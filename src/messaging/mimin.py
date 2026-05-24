"""
Mimin.io adapter.

Two distinct APIs:
- Omnichannel chat send: individual outgoing messages (1:1 chat replies)
- Campaign send: batched template messages

Configure via env vars (all placeholders — set to your Mimin tenant values):
- MIMIN_API_TOKEN          (required)
- MIMIN_STORE              (appended to the chat URL; default "your-store")
- MIMIN_ACCOUNT_ID         (default "0" — set to your numeric account id)
- MIMIN_INBOX_ID           (default "0" — set to your numeric inbox id)
- MIMIN_CAMPAIGN_URL       (default "https://mimin-campaign.example.com")
- MIMIN_CAMPAIGN_API_KEY   (required for campaigns)
- WHATSAPP_SENDER          (default "15555550199" — set to your sender number)
- MIMIN_DISABLED=true      (no-op send_text — useful in tests)
"""

from __future__ import annotations

import os
import random
import time
from typing import Optional

import requests

from ..logging_config import get_logger
from .base import CampaignResult, MessagingAdapter, SendResult

logger = get_logger(__name__)


def _generate_message_id() -> str:
    return str(random.randint(1_000_000_000_000, 9_999_999_999_999))


class MiminAdapter(MessagingAdapter):
    """Mimin.io WhatsApp provider."""

    name = "mimin"

    def __init__(self) -> None:
        store = os.getenv("MIMIN_STORE", "your-store")
        self._chat_url = (
            f"https://mimin-api.mimin.io/whatsapp-api/api/v1/omnichannel/store/{store}"
        )
        self._account_id = os.getenv("MIMIN_ACCOUNT_ID", "0")
        self._inbox_id = os.getenv("MIMIN_INBOX_ID", "0")
        self._campaign_url = os.getenv(
            "MIMIN_CAMPAIGN_URL", "https://mimin-campaign.example.com"
        ).rstrip("/")
        self._whatsapp_sender = os.getenv("WHATSAPP_SENDER", "15555550199")

    # ----- send_text --------------------------------------------------------

    def send_text(self, phone_number: str, message: str) -> SendResult:
        if os.getenv("MIMIN_DISABLED", "").lower() == "true":
            mid = _generate_message_id()
            logger.info("Mimin disabled — text send skipped", extra={
                "event": "mimin_disabled",
                "phone_number": phone_number,
                "mimin_id": mid,
            })
            return SendResult(success=True, message_id=mid, error=None)

        token = os.getenv("MIMIN_API_TOKEN")
        if not token:
            logger.error("MIMIN_API_TOKEN not configured", extra={"event": "mimin_config_error"})
            return SendResult(success=False, message_id=None, error="MIMIN_API_TOKEN not configured")

        mid = _generate_message_id()
        payload = {
            "id": int(mid),
            "account": {"id": self._account_id},
            "inbox": {"id": self._inbox_id},
            "content": message,
            "message_type": "outgoing",
            "event": "message_created",
            "conversation": {
                "meta": {"sender": {"name": "", "phone_number": phone_number}}
            },
            "content_attributes": {"in_reply_to": None},
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        try:
            response = requests.post(self._chat_url, json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            logger.info("WhatsApp message sent (Mimin)", extra={
                "event": "mimin_sent",
                "phone_number": phone_number,
                "mimin_id": mid,
            })
            return SendResult(success=True, message_id=mid, error=None)

        except requests.exceptions.Timeout:
            logger.error("Mimin chat timeout", extra={
                "event": "mimin_timeout", "phone_number": phone_number, "mimin_id": mid,
            })
            return SendResult(success=False, message_id=mid, error="Request timeout")

        except requests.exceptions.HTTPError as e:
            logger.error("Mimin chat HTTP error", extra={
                "event": "mimin_http_error",
                "phone_number": phone_number,
                "mimin_id": mid,
                "status_code": e.response.status_code,
            })
            return SendResult(
                success=False, message_id=mid, error=f"HTTP {e.response.status_code}"
            )

        except requests.exceptions.RequestException as e:
            logger.error("Mimin chat request error", extra={
                "event": "mimin_request_error",
                "phone_number": phone_number,
                "mimin_id": mid,
                "error": str(e),
            })
            return SendResult(success=False, message_id=mid, error=str(e))

    # ----- send_campaign ----------------------------------------------------

    def send_campaign(
        self, template_name: str, recipients: list[dict], *, dry_run: bool = False
    ) -> Optional[CampaignResult]:
        if not recipients:
            logger.info("No recipients — skipping campaign", extra={
                "event": "campaign_skip", "template": template_name,
            })
            return None

        if dry_run:
            logger.info("Campaign dry-run", extra={
                "event": "campaign_dry_run",
                "template": template_name,
                "recipients": len(recipients),
            })
            return CampaignResult(
                dry_run=True, sent_count=len(recipients), failed_count=0, campaign_id=None
            )

        api_key = os.getenv("MIMIN_CAMPAIGN_API_KEY")
        if not api_key:
            logger.error("MIMIN_CAMPAIGN_API_KEY not set", extra={
                "event": "campaign_config_error", "template": template_name,
            })
            return None

        payload = {
            "template_name": template_name,
            "sender": self._whatsapp_sender,
            "recipients": recipients,
        }
        url = f"{self._campaign_url}/api/send"
        started = time.time()
        try:
            response = requests.post(
                url,
                headers={"X-API-Key": api_key, "Content-Type": "application/json"},
                json=payload,
                timeout=30,
            )
            elapsed = time.time() - started
            if response.ok:
                body = response.json()
                logger.info("Campaign sent (Mimin)", extra={
                    "event": "campaign_sent",
                    "template": template_name,
                    "recipients": len(recipients),
                    "campaign_id": body.get("campaign_id"),
                    "duration_seconds": round(elapsed, 2),
                })
                return CampaignResult(
                    campaign_id=body.get("campaign_id"),
                    sent_count=len(recipients),
                    failed_count=0,
                    dry_run=False,
                )
            logger.error("Mimin campaign API error", extra={
                "event": "campaign_api_error",
                "template": template_name,
                "status_code": response.status_code,
                "response": response.text[:500],
                "duration_seconds": round(elapsed, 2),
            })
            return None
        except requests.exceptions.RequestException as e:
            logger.error("Mimin campaign request failed", extra={
                "event": "campaign_request_error",
                "template": template_name,
                "error": str(e),
                "duration_seconds": round(time.time() - started, 2),
            })
            return None
