"""
Pure-function smoke tests.

These run without a database or a live LLM. They mostly catch import-time
breakage and obvious regressions in locale-formatting / template-rendering
helpers.
"""

import os

# Minimal env so config loads without complaining.
os.environ.setdefault("DATABASE_URL", "postgresql://localhost/x")
os.environ.setdefault("OPENROUTER_API_KEY", "sk-test")

from src.config import reload_config  # noqa: E402
from src.followup_messages import (  # noqa: E402
    get_followup_message,
    get_payment_confirmed_message,
)
from src.i18n import format_currency, format_date_iso  # noqa: E402
from src.timezone_utils import now_local, today_local  # noqa: E402


def setup_module(_module):
    reload_config()


def test_timezone_helpers_return_aware_datetime():
    n = now_local()
    assert n.tzinfo is not None
    assert today_local() == n.date()


def test_format_currency_uses_default_usd():
    out = format_currency(1234.56)
    assert "1" in out and "234" in out  # locale-grouped


def test_format_date_iso():
    from datetime import date
    assert format_date_iso(date(2026, 5, 24)) == "2026-05-24"


def test_payment_confirmed_message_substitutes_persona():
    msg = get_payment_confirmed_message()
    assert "payment" in msg.lower()
    assert "{product_name}" not in msg  # placeholder substituted


def test_followup_message_renders_without_error():
    msg = get_followup_message("Alex Carter", days_late=2, billing_amount=100.00)
    assert "Alex" in msg


def test_no_legacy_wib_aliases():
    import src.timezone_utils as tz
    assert not hasattr(tz, "today_wib")
    assert not hasattr(tz, "now_wib")


def test_validate_env_rejects_missing_prompt_dir(tmp_path, monkeypatch):
    """validate_env() must fail fast when PROMPT_DIR/system_prompt.md is absent.

    Prior behavior crashed only at first message, which is hours/days late in
    daemon mode.
    """
    import pytest
    from src.startup import validate_env

    monkeypatch.setenv("PROMPT_DIR", str(tmp_path))  # empty dir — no system_prompt.md
    with pytest.raises(FileNotFoundError, match="system_prompt.md"):
        validate_env()


def test_validate_env_accepts_default_prompt_dir(monkeypatch):
    """The shipped prompts/en bundle must satisfy validation."""
    from src.startup import validate_env

    monkeypatch.delenv("PROMPT_DIR", raising=False)
    validate_env()  # must not raise
