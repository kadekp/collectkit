"""
Timezone helpers driven by the TIMEZONE environment variable.

`now_local()` / `today_local()` are the canonical entry points.
"""

from datetime import date, datetime

from .config import get_config
from .i18n import format_date_iso  # re-exported


def now_local() -> datetime:
    """Current datetime in the configured local timezone."""
    return datetime.now(get_config().timezone)


def today_local() -> date:
    """Current date in the configured local timezone."""
    return now_local().date()


__all__ = [
    "now_local",
    "today_local",
    "format_date_iso",
]
