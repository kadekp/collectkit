"""
Telegram Bot API adapter.

Telegram identifies recipients by numeric `chat_id`, not phone number.
For the bot's existing data model (which is keyed on phone numbers) this
adapter expects `phone_number` to actually be the customer's Telegram
chat_id (as a string). Setting up that mapping is the responsibility of
your receiver service — typically you'd store `phone_number=<chat_id>` in
the borrowers table when a user starts the bot.

Configure via env vars:
- TELEGRAM_BOT_TOKEN     (required — from BotFather)
- TELEGRAM_PARSE_MODE    "MarkdownV2", "HTML", or "" (default "")
- TELEGRAM_DISABLED=true (no-op send — useful in tests)
"""

from __future__ import annotations

import os
import time
from typing import Optional

import requests

from ..config import get_config
from ..logging_config import get_logger
from .base import CampaignResult, MessagingAdapter, SendResult

logger = get_logger(__name__)


class TelegramAdapter(MessagingAdapter):
    """Telegram Bot API provider."""

    name = "telegram"

    def __init__(self) -> None:
        self._token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._parse_mode = os.getenv("TELEGRAM_PARSE_MODE", "")

    def _api_url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self._token}/{method}"

    # ----- send_text --------------------------------------------------------

    def send_text(self, phone_number: str, message: str) -> SendResult:
        if os.getenv("TELEGRAM_DISABLED", "").lower() == "true":
            logger.info("Telegram disabled — text send skipped", extra={
                "event": "telegram_disabled", "chat_id": phone_number,
            })
            return SendResult(success=True, message_id=None, error=None)

        if not self._token:
            logger.error("Telegram bot token not configured", extra={
                "event": "telegram_config_error",
            })
            return SendResult(
                success=False, message_id=None, error="TELEGRAM_BOT_TOKEN not configured"
            )

        payload = {"chat_id": phone_number, "text": message}
        if self._parse_mode:
            payload["parse_mode"] = self._parse_mode

        try:
            response = requests.post(self._api_url("sendMessage"), json=payload, timeout=30)
            response.raise_for_status()
            data = response.json()
            mid = str(data.get("result", {}).get("message_id", ""))
            logger.info("Message sent (Telegram)", extra={
                "event": "telegram_sent", "chat_id": phone_number, "telegram_message_id": mid,
            })
            return SendResult(success=True, message_id=mid, error=None)
        except requests.exceptions.Timeout:
            return SendResult(success=False, message_id=None, error="Request timeout")
        except requests.exceptions.HTTPError as e:
            logger.error("Telegram HTTP error", extra={
                "event": "telegram_http_error",
                "chat_id": phone_number,
                "status_code": e.response.status_code,
                "response": e.response.text[:500],
            })
            return SendResult(
                success=False, message_id=None, error=f"HTTP {e.response.status_code}"
            )
        except requests.exceptions.RequestException as e:
            logger.error("Telegram request error", extra={
                "event": "telegram_request_error", "chat_id": phone_number, "error": str(e),
            })
            return SendResult(success=False, message_id=None, error=str(e))

    # ----- send_campaign ----------------------------------------------------

    def send_campaign(
        self, template_name: str, recipients: list[dict], *, dry_run: bool = False
    ) -> Optional[CampaignResult]:
        if not recipients:
            return None
        if dry_run:
            logger.info("Telegram campaign dry-run", extra={
                "event": "campaign_dry_run",
                "template": template_name,
                "recipients": len(recipients),
            })
            return CampaignResult(
                dry_run=True, sent_count=len(recipients), failed_count=0, campaign_id=None
            )

        cfg = get_config()
        reminders = cfg.reminders or {}
        body_template: Optional[str] = None
        for kind, spec in reminders.items():
            if (spec or {}).get("campaign_template") == template_name:
                body_template = spec.get("chat_message")
                break

        if not body_template:
            logger.error("No reminders.yaml entry matches template", extra={
                "event": "telegram_campaign_no_template", "template": template_name,
            })
            return None

        started = time.time()
        sent = failed = 0
        for r in recipients:
            placeholders = {
                **cfg.template_vars(),
                "name": r.get("name", ""),
                "amount": r.get("amount", ""),
                "amount_plain": r.get("amount", ""),
                "promise_date": r.get("promise-date", ""),
                "payment_link": r.get("payment-link", ""),
            }
            try:
                body = body_template.format(**placeholders).strip()
            except KeyError as missing:
                logger.warning(
                    "Skipping recipient — template placeholder missing",
                    extra={
                        "event": "telegram_campaign_template_error",
                        "template": template_name,
                        "missing": str(missing),
                    },
                )
                failed += 1
                continue
            result = self.send_text(r["phone"], body)
            if result["success"]:
                sent += 1
            else:
                failed += 1

        logger.info("Telegram campaign completed", extra={
            "event": "campaign_sent",
            "template": template_name,
            "sent_count": sent,
            "failed_count": failed,
            "duration_seconds": round(time.time() - started, 2),
        })
        return CampaignResult(
            campaign_id=None, sent_count=sent, failed_count=failed, dry_run=False
        )
