"""
Morning follow-up message generation.

Tier definitions live in `prompts/<locale>/followups.yaml` (selected via
PROMPT_DIR). See that file for the supported placeholders.
"""

from __future__ import annotations

from typing import Any

from .config import get_config
from .i18n import format_currency


def _select_tier(tiers: list[dict[str, Any]], days_late: int) -> dict[str, Any]:
    """Find the first tier whose max_days_late >= days_late.
    A tier with `max_days_late: null` is the catch-all."""
    for tier in tiers:
        cap = tier.get("max_days_late")
        if cap is None:
            return tier
        if days_late <= int(cap):
            return tier
    return tiers[-1] if tiers else {"message": ""}


def get_followup_message(
    customer_name: str,
    days_late: int,
    billing_amount: float,
) -> str:
    """Generate a follow-up message for a late borrower."""
    cfg = get_config()
    bundle = cfg.followups or {}
    tiers = bundle.get("tiers") or []
    default_first_name = bundle.get("default_first_name") or "there"

    # Extract first name (or fall back to the locale-appropriate default)
    first_name = default_first_name
    if customer_name and customer_name.strip():
        parts = customer_name.strip().split()
        if parts:
            first_name = parts[0]

    tier = _select_tier(tiers, days_late)
    template = tier.get("message") or ""

    placeholders = {
        **cfg.template_vars(),
        "first_name": first_name,
        "customer_name": customer_name or default_first_name,
        "days_late": days_late,
        "amount": format_currency(billing_amount),
    }

    try:
        return template.format(**placeholders).strip()
    except KeyError as missing:
        # If a template uses an unknown placeholder, fail loudly with context
        raise KeyError(
            f"Follow-up tier '{tier.get('label')}' references undefined "
            f"placeholder {missing}. Available: {sorted(placeholders.keys())}"
        ) from missing


_DEFAULT_PAYMENT_CONFIRMED = (
    "Hi! We've confirmed your payment — thank you! 🎉\n"
    "Your {product_name} account is active again."
)


def get_payment_confirmed_message() -> str:
    """Generate the message sent when a borrower's payment is confirmed.

    Pulls the template from `prompts/<locale>/followups.yaml` under the
    `payment_confirmed` key, with persona placeholders substituted in.
    """
    cfg = get_config()
    template = (cfg.followups or {}).get("payment_confirmed") or _DEFAULT_PAYMENT_CONFIRMED
    try:
        return template.format(**cfg.template_vars()).strip()
    except KeyError as missing:
        raise KeyError(
            f"payment_confirmed template references undefined placeholder {missing}. "
            f"Available: {sorted(cfg.template_vars().keys())}"
        ) from missing
