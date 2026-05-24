"""
Messaging adapter interface.

A `MessagingAdapter` is responsible for delivering messages to customers
on whichever channel a deployment uses (WhatsApp via Mimin or Twilio,
Telegram, etc.). All adapters expose the same two methods so the rest of
the bot is provider-agnostic.

Add a new adapter by:
  1. Subclassing `MessagingAdapter` and implementing `send_text` and
     `send_campaign`.
  2. Registering it in `src/messaging/__init__.py` under a string key.
  3. Setting `MESSAGING_PROVIDER=<your-key>` in the environment.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional, TypedDict


class SendResult(TypedDict):
    """Return value of MessagingAdapter.send_text()."""
    success: bool
    message_id: Optional[str]
    error: Optional[str]


class CampaignResult(TypedDict, total=False):
    """Return value of MessagingAdapter.send_campaign()."""
    campaign_id: Optional[str]
    sent_count: int
    failed_count: int
    dry_run: bool


class MessagingAdapter(ABC):
    """Abstract interface for sending messages to customers."""

    #: Short string identifying the provider in logs / metrics.
    name: str = "base"

    @abstractmethod
    def send_text(self, phone_number: str, message: str) -> SendResult:
        """Send a single text message to one recipient.

        Args:
            phone_number: Recipient in the channel's address format. For
                WhatsApp this is digits-only, international format (e.g.
                ``"15555550101"``). For Telegram this is the numeric chat_id
                as a string.
            message: Message body (plain text, may contain emoji).

        Returns:
            A SendResult dict. ``success=False`` should be returned (not
            raised) for retryable provider errors; ``error`` carries a short
            description for logging.
        """

    @abstractmethod
    def send_campaign(
        self,
        template_name: str,
        recipients: list[dict],
        *,
        dry_run: bool = False,
    ) -> Optional[CampaignResult]:
        """Send a templated bulk message to many recipients.

        Args:
            template_name: Provider-specific template identifier (must be
                pre-registered with the provider for WhatsApp/Twilio; for
                Telegram and Null adapters this is treated as a label).
            recipients: List of recipient dicts. Each dict MUST include at
                least ``phone`` and ``name``; remaining keys are template
                variables. For Mimin these are passed straight through; for
                Twilio/Telegram they're substituted into the campaign body.
            dry_run: When True, the adapter logs what it would send and
                returns a result with ``dry_run=True`` instead of sending.

        Returns:
            A CampaignResult, or None on hard provider failure.
        """
