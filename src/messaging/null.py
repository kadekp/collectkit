"""
Null messaging adapter — logs what would be sent but doesn't talk to any
provider. Useful for local dev, tests, and dry-running production data.

Selected via `MESSAGING_PROVIDER=null`.
"""

from __future__ import annotations

import random
from typing import Optional

from ..logging_config import get_logger
from .base import CampaignResult, MessagingAdapter, SendResult

logger = get_logger(__name__)


def _fake_id() -> str:
    return f"null-{random.randint(100_000_000, 999_999_999)}"


class NullAdapter(MessagingAdapter):
    """No-op adapter. All sends succeed; nothing leaves the process."""

    name = "null"

    def send_text(self, phone_number: str, message: str) -> SendResult:
        mid = _fake_id()
        logger.info("Null adapter — text would be sent", extra={
            "event": "null_send_text",
            "phone_number": phone_number,
            "message_preview": message[:120],
            "null_message_id": mid,
        })
        return SendResult(success=True, message_id=mid, error=None)

    def send_campaign(
        self, template_name: str, recipients: list[dict], *, dry_run: bool = False
    ) -> Optional[CampaignResult]:
        logger.info("Null adapter — campaign would be sent", extra={
            "event": "null_send_campaign",
            "template": template_name,
            "recipients": len(recipients),
            "dry_run": dry_run,
        })
        return CampaignResult(
            campaign_id=_fake_id(),
            sent_count=len(recipients),
            failed_count=0,
            dry_run=dry_run,
        )
