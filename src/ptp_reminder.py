"""
PTP (Promise to Pay) reminders — sends daily WhatsApp campaign messages
for due-today / due-tomorrow / missed PTPs and logs them to chat_history.

Templates live in `prompts/<locale>/reminders.yaml`. Payment-link generation
is overridable via the PAYMENT_LINK_BASE environment variable.
"""

from __future__ import annotations

import os
import time
from datetime import date

import newrelic.agent

from .config import get_config
from .database_pg import (
    get_missed_ptp_yesterday,
    get_ptp_due_today,
    get_ptp_due_tomorrow,
    log_ptp_reminders,
    log_ptp_to_chat_history,
)
from .i18n import format_amount_no_currency, format_currency, format_date_long
from .logging_config import get_logger
from .messaging import get_messaging_adapter


def send_campaign(template_name: str, recipients: list[dict]):
    """Thin wrapper over the configured messaging adapter."""
    return get_messaging_adapter().send_campaign(template_name, recipients)

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Payment-link generator
# ---------------------------------------------------------------------------

def generate_payment_link(customer_number: str) -> str:
    """Build a payment link from the configured base URL.

    Set `PAYMENT_LINK_BASE` to the prefix; the customer number is appended.
    Defaults to a placeholder that callers will obviously need to override.
    """
    base = os.getenv(
        "PAYMENT_LINK_BASE",
        "https://example.com/payment-url",
    ).rstrip("/")
    return f"{base}/{customer_number}"


# ---------------------------------------------------------------------------
# Reminder bundle accessors
# ---------------------------------------------------------------------------

def _reminder(kind: str) -> dict:
    cfg = get_config()
    return (cfg.reminders or {}).get(kind, {}) or {}


def _campaign_template(kind: str, default: str) -> str:
    return _reminder(kind).get("campaign_template") or default


def _chat_message_template(kind: str) -> str:
    return _reminder(kind).get("chat_message") or ""


# ---------------------------------------------------------------------------
# Recipient + message builders
# ---------------------------------------------------------------------------

def _build_recipients(rows: list[dict]) -> list[dict]:
    return [
        {
            "phone": row["phone_number"],
            "name": row["customer_name"],
            "amount": format_currency(row["promise_amount"]),
            "payment-link": generate_payment_link(row["customer_number"]),
        }
        for row in rows
    ]


def _build_recipients_missed(rows: list[dict]) -> list[dict]:
    return [
        {
            "phone": row["phone_number"],
            "name": row["customer_name"],
            "amount": format_amount_no_currency(
                row.get("billing_amount") or row["promise_amount"]
            ),
            "promise-date": format_date_long(row["promise_date"]),
            "payment-link": generate_payment_link(row["customer_number"]),
        }
        for row in rows
    ]


def _placeholders_for(row: dict) -> dict:
    cfg = get_config()
    amount_value = row.get("billing_amount") or row.get("promise_amount", 0)
    return {
        **cfg.template_vars(),
        "name": row["customer_name"],
        "amount": format_currency(row.get("promise_amount", amount_value)),
        "amount_plain": format_amount_no_currency(amount_value),
        "promise_date": format_date_long(row["promise_date"]) if row.get("promise_date") else "",
        "payment_link": generate_payment_link(row["customer_number"]),
    }


def _log_chat_history(rows: list[dict], template: str) -> None:
    if not template:
        return
    entries = []
    for row in rows:
        try:
            message = template.format(**_placeholders_for(row)).strip()
        except KeyError as missing:
            logger.warning(
                "Reminder template references undefined placeholder",
                extra={"event": "reminder_template_error", "missing": str(missing)},
            )
            continue
        entries.append((row["phone_number"], message))
    if entries:
        log_ptp_to_chat_history(entries)


def _process_reminder_type(
    kind: str,
    rows: list[dict],
    template_name: str,
    recipients: list[dict],
    today: date,
) -> None:
    if not rows:
        return

    result = send_campaign(template_name, recipients)
    if not result:
        return

    campaign_id = result.get("campaign_id")
    log_ptp_reminders(rows, template_name, campaign_id, today)
    _log_chat_history(rows, _chat_message_template(kind))


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

@newrelic.agent.background_task()
def process_ptp_reminders() -> None:
    """Query, send, and log all three reminder types. Runs once per day."""
    start_time = time.time()
    logger.info("PTP reminder processing started", extra={"event": "ptp_reminder_start"})

    today = date.today()

    due_today_template = _campaign_template("due_today", "bot_ptp_due")
    dminus_1_template = _campaign_template("dminus_1", "bot_ptp_dminus_1")
    missed_template = _campaign_template("missed", "bot_ptp_missed")

    # 1. PTPs due today
    due_today = get_ptp_due_today(due_today_template)
    logger.info(f"Found {len(due_today)} PTP(s) due today", extra={
        "event": "ptp_reminder_query", "type": "due_today", "count": len(due_today),
    })
    _process_reminder_type(
        "due_today", due_today, due_today_template, _build_recipients(due_today), today,
    )

    # 2. PTPs due tomorrow (D-1)
    due_tomorrow = get_ptp_due_tomorrow(dminus_1_template)
    logger.info(f"Found {len(due_tomorrow)} PTP(s) due tomorrow", extra={
        "event": "ptp_reminder_query", "type": "due_tomorrow", "count": len(due_tomorrow),
    })
    _process_reminder_type(
        "dminus_1", due_tomorrow, dminus_1_template, _build_recipients(due_tomorrow), today,
    )

    # 3. Missed PTPs from yesterday
    missed_yesterday = get_missed_ptp_yesterday(missed_template)
    logger.info(f"Found {len(missed_yesterday)} missed PTP(s) from yesterday", extra={
        "event": "ptp_reminder_query", "type": "missed_yesterday", "count": len(missed_yesterday),
    })
    _process_reminder_type(
        "missed", missed_yesterday, missed_template, _build_recipients_missed(missed_yesterday), today,
    )

    elapsed = time.time() - start_time
    logger.info("PTP reminder processing completed", extra={
        "event": "ptp_reminder_complete",
        "due_today": len(due_today),
        "due_tomorrow": len(due_tomorrow),
        "missed_yesterday": len(missed_yesterday),
        "duration_seconds": round(elapsed, 2),
    })
