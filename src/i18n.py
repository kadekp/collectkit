"""
Locale-aware formatting helpers (powered by Babel).

Currency, date, and number formatting all read locale and currency code
from `src.config`, so callers don't need to thread them through.
"""

from __future__ import annotations

from datetime import date, datetime

from babel.dates import format_date as _babel_format_date
from babel.numbers import format_currency as _babel_format_currency

from .config import get_config


def format_currency(amount: float, *, currency_code: str | None = None) -> str:
    """Format an amount in the configured (or overridden) currency."""
    cfg = get_config()
    code = currency_code or cfg.currency_code
    return _babel_format_currency(amount, code, locale=cfg.locale)


def format_amount_no_currency(amount: float) -> str:
    """Format an amount with locale-appropriate digit grouping but no currency symbol."""
    from babel.numbers import format_decimal

    cfg = get_config()
    return format_decimal(amount, locale=cfg.locale, format="#,##0")


def format_date_long(d: date | str) -> str:
    """Format a date in the configured locale (long form, e.g. 'January 27, 2026')."""
    cfg = get_config()
    if isinstance(d, str):
        try:
            d = datetime.strptime(d, "%Y-%m-%d").date()
        except ValueError:
            return d
    return _babel_format_date(d, format="long", locale=cfg.locale)


def format_date_iso(d: date) -> str:
    """Format date as YYYY-MM-DD (locale-independent)."""
    return d.isoformat()
