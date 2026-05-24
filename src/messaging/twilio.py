"""
Twilio adapter (WhatsApp Business or SMS).

Uses Twilio's REST Messages API directly so the `twilio` Python SDK isn't
a required dependency for users on other providers.

Configure via env vars:
- TWILIO_ACCOUNT_SID    (required)
- TWILIO_AUTH_TOKEN     (required)
- TWILIO_FROM           (required — e.g. "whatsapp:+14155238886" for the
                         WhatsApp sandbox, or "+15558675309" for SMS)
- TWILIO_CHANNEL        "whatsapp" (default) or "sms"
- TWILIO_DISABLED=true  (no-op send — useful in tests)

`send_campaign` simply loops over recipients and substitutes their
template variables into a chat_message body resolved from
prompts/<locale>/reminders.yaml. Twilio Content Templates can be used
instead by supplying TWILIO_CONTENT_SID_<TEMPLATE_NAME> env vars (see
PROVIDERS.md for the contract).
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

TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"


def _format_to(phone_number: str, channel: str) -> str:
    """Convert a raw phone number to Twilio's expected `to` field."""
    cleaned = phone_number if phone_number.startswith("+") else f"+{phone_number}"
    if channel == "whatsapp" and not cleaned.startswith("whatsapp:"):
        return f"whatsapp:{cleaned}"
    return cleaned


class TwilioAdapter(MessagingAdapter):
    """Twilio Messages API provider."""

    name = "twilio"

    def __init__(self) -> None:
        self._account_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
        self._auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
        self._from = os.getenv("TWILIO_FROM", "")
        self._channel = os.getenv("TWILIO_CHANNEL", "whatsapp").lower()
        if self._channel not in ("whatsapp", "sms"):
            raise ValueError(f"TWILIO_CHANNEL must be 'whatsapp' or 'sms', got {self._channel!r}")

    # ----- send_text --------------------------------------------------------

    def send_text(self, phone_number: str, message: str) -> SendResult:
        if os.getenv("TWILIO_DISABLED", "").lower() == "true":
            logger.info("Twilio disabled — text send skipped", extra={
                "event": "twilio_disabled", "phone_number": phone_number,
            })
            return SendResult(success=True, message_id=None, error=None)

        if not self._account_sid or not self._auth_token or not self._from:
            logger.error("Twilio not configured", extra={"event": "twilio_config_error"})
            return SendResult(
                success=False,
                message_id=None,
                error="TWILIO_ACCOUNT_SID/AUTH_TOKEN/FROM not configured",
            )

        url = f"{TWILIO_API_BASE}/Accounts/{self._account_sid}/Messages.json"
        data = {
            "From": self._from,
            "To": _format_to(phone_number, self._channel),
            "Body": message,
        }
        try:
            response = requests.post(
                url,
                data=data,
                auth=(self._account_sid, self._auth_token),
                timeout=30,
            )
            response.raise_for_status()
            sid = response.json().get("sid")
            logger.info("Message sent (Twilio)", extra={
                "event": "twilio_sent",
                "phone_number": phone_number,
                "twilio_sid": sid,
                "channel": self._channel,
            })
            return SendResult(success=True, message_id=sid, error=None)
        except requests.exceptions.Timeout:
            logger.error("Twilio timeout", extra={
                "event": "twilio_timeout", "phone_number": phone_number,
            })
            return SendResult(success=False, message_id=None, error="Request timeout")
        except requests.exceptions.HTTPError as e:
            logger.error("Twilio HTTP error", extra={
                "event": "twilio_http_error",
                "phone_number": phone_number,
                "status_code": e.response.status_code,
                "response": e.response.text[:500],
            })
            return SendResult(
                success=False, message_id=None, error=f"HTTP {e.response.status_code}"
            )
        except requests.exceptions.RequestException as e:
            logger.error("Twilio request error", extra={
                "event": "twilio_request_error", "phone_number": phone_number, "error": str(e),
            })
            return SendResult(success=False, message_id=None, error=str(e))

    # ----- send_campaign ----------------------------------------------------

    def send_campaign(
        self, template_name: str, recipients: list[dict], *, dry_run: bool = False
    ) -> Optional[CampaignResult]:
        if not recipients:
            return None

        if dry_run:
            logger.info("Twilio campaign dry-run", extra={
                "event": "campaign_dry_run",
                "template": template_name,
                "recipients": len(recipients),
            })
            return CampaignResult(
                dry_run=True, sent_count=len(recipients), failed_count=0, campaign_id=None
            )

        # Resolve a chat_message body from reminders.yaml so we don't need
        # provider-side templates to start.
        cfg = get_config()
        reminders = cfg.reminders or {}
        body_template: Optional[str] = None
        for kind, spec in reminders.items():
            if (spec or {}).get("campaign_template") == template_name:
                body_template = spec.get("chat_message")
                break

        if not body_template:
            logger.error("No reminders.yaml entry matches template", extra={
                "event": "twilio_campaign_no_template",
                "template": template_name,
            })
            return None

        started = time.time()
        sent = 0
        failed = 0
        for r in recipients:
            try:
                body = body_template.format(**_recipient_to_placeholders(r, cfg)).strip()
            except KeyError as missing:
                logger.warning(
                    "Skipping recipient — template placeholder missing",
                    extra={
                        "event": "twilio_campaign_template_error",
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

        logger.info("Twilio campaign completed", extra={
            "event": "campaign_sent",
            "template": template_name,
            "sent_count": sent,
            "failed_count": failed,
            "duration_seconds": round(time.time() - started, 2),
        })
        return CampaignResult(
            campaign_id=None, sent_count=sent, failed_count=failed, dry_run=False
        )


def _recipient_to_placeholders(recipient: dict, cfg) -> dict:
    """Map a recipient dict (with Mimin-style hyphenated keys) to template placeholders."""
    return {
        **cfg.template_vars(),
        "name": recipient.get("name", ""),
        "amount": recipient.get("amount", ""),
        "amount_plain": recipient.get("amount", ""),
        "promise_date": recipient.get("promise-date", ""),
        "payment_link": recipient.get("payment-link", ""),
    }
