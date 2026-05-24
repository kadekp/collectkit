"""
Messaging adapter factory.

The bot picks an adapter at runtime based on the `MESSAGING_PROVIDER`
environment variable. Add a new provider by registering it in `_REGISTRY`.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Callable

from .base import CampaignResult, MessagingAdapter, SendResult


def _build_mimin() -> MessagingAdapter:
    from .mimin import MiminAdapter
    return MiminAdapter()


def _build_twilio() -> MessagingAdapter:
    from .twilio import TwilioAdapter
    return TwilioAdapter()


def _build_telegram() -> MessagingAdapter:
    from .telegram import TelegramAdapter
    return TelegramAdapter()


def _build_null() -> MessagingAdapter:
    from .null import NullAdapter
    return NullAdapter()


_REGISTRY: dict[str, Callable[[], MessagingAdapter]] = {
    "mimin": _build_mimin,
    "twilio": _build_twilio,
    "telegram": _build_telegram,
    "null": _build_null,
}


@lru_cache(maxsize=1)
def get_messaging_adapter() -> MessagingAdapter:
    """Return the cached adapter for the configured provider."""
    provider = os.getenv("MESSAGING_PROVIDER", "mimin").lower().strip()
    if provider not in _REGISTRY:
        raise ValueError(
            f"Unknown MESSAGING_PROVIDER={provider!r}. "
            f"Supported: {sorted(_REGISTRY)}"
        )
    return _REGISTRY[provider]()


def reset_messaging_adapter() -> None:
    """Drop the cached adapter (useful in tests when env vars change)."""
    get_messaging_adapter.cache_clear()


__all__ = [
    "MessagingAdapter",
    "SendResult",
    "CampaignResult",
    "get_messaging_adapter",
    "reset_messaging_adapter",
]
