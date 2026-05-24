"""
Central configuration loader.

Reads:
- Environment variables (TIMEZONE, LOCALE, CURRENCY_CODE, PROMPT_DIR, ...)
- Persona file at PERSONA_CONFIG (default: config/persona.yaml,
  falls back to config/persona.example.yaml)
- YAML files inside PROMPT_DIR (default: prompts/en/)

Exposes a single cached `get_config()` function so callers don't repeatedly
hit disk. Reload via `reload_config()` (useful in tests).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent


def _resolve(p: str | Path) -> Path:
    """Resolve a path relative to the repo root if it isn't absolute."""
    p = Path(p)
    return p if p.is_absolute() else REPO_ROOT / p


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file. Returns {} if the file doesn't exist."""
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected mapping at top level of {path}, got {type(data).__name__}")
    return data


def _env_int_list(name: str, default: list[int]) -> list[int]:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return [int(x.strip()) for x in raw.split(",") if x.strip()]
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    """Resolved runtime configuration."""

    # Locale / timezone / currency
    timezone: ZoneInfo
    timezone_name: str
    locale: str
    currency_code: str

    # Persona (bot identity, company, product, business rules)
    persona: dict[str, Any]

    # Prompt bundle (system_prompt.md + yaml siblings)
    prompt_dir: Path
    followups: dict[str, Any]
    guardrails: dict[str, Any]
    strategies: dict[str, Any]
    reminders: dict[str, Any]

    # Scheduled job hours (local time)
    ptp_check_hour: int
    bulk_sync_hours: list[int]
    followup_hour: int
    ptp_reminder_hour: int

    # Convenience accessors
    @property
    def bot_name(self) -> str:
        return self.persona.get("bot_name", "Bot")

    @property
    def company_name(self) -> str:
        return self.persona.get("company_name", "your company")

    @property
    def product_name(self) -> str:
        return self.persona.get("product_name", "our product")

    @property
    def support_handoff_channel(self) -> str:
        return self.persona.get("support_handoff_channel", "our support team")

    def template_vars(self) -> dict[str, Any]:
        """Flat dict of persona values for use in `str.format()` templates."""
        flat: dict[str, Any] = {
            "bot_name": self.bot_name,
            "company_name": self.company_name,
            "product_name": self.product_name,
            "support_handoff_channel": self.support_handoff_channel,
            "currency_code": self.currency_code,
            "locale": self.locale,
        }
        rules = self.persona.get("business_rules") or {}
        if isinstance(rules, dict):
            for k, v in rules.items():
                flat[f"rule_{k}"] = v
        return flat


def _load_persona() -> dict[str, Any]:
    explicit = os.getenv("PERSONA_CONFIG")
    candidates: list[Path] = []
    if explicit:
        candidates.append(_resolve(explicit))
    candidates.extend(
        [
            _resolve("config/persona.yaml"),
            _resolve("config/persona.example.yaml"),
        ]
    )
    for path in candidates:
        if path.exists():
            return _load_yaml(path)
    return {}


def _load_prompt_bundle(prompt_dir: Path) -> tuple[dict, dict, dict, dict]:
    """Load YAML siblings of system_prompt.md from a prompt directory."""
    return (
        _load_yaml(prompt_dir / "followups.yaml"),
        _load_yaml(prompt_dir / "guardrails.yaml"),
        _load_yaml(prompt_dir / "strategies.yaml"),
        _load_yaml(prompt_dir / "reminders.yaml"),
    )


def _build_config() -> Config:
    timezone_name = os.getenv("TIMEZONE", "UTC")
    try:
        timezone = ZoneInfo(timezone_name)
    except Exception:
        timezone = ZoneInfo("UTC")
        timezone_name = "UTC"

    locale = os.getenv("LOCALE", "en_US")
    currency_code = os.getenv("CURRENCY_CODE", "USD")

    persona = _load_persona()

    prompt_dir = _resolve(os.getenv("PROMPT_DIR", "prompts/en"))
    followups, guardrails, strategies, reminders = _load_prompt_bundle(prompt_dir)

    return Config(
        timezone=timezone,
        timezone_name=timezone_name,
        locale=locale,
        currency_code=currency_code,
        persona=persona,
        prompt_dir=prompt_dir,
        followups=followups,
        guardrails=guardrails,
        strategies=strategies,
        reminders=reminders,
        ptp_check_hour=_env_int("PTP_CHECK_HOUR", 23),
        bulk_sync_hours=_env_int_list("BULK_SYNC_HOURS", [7, 19]),
        followup_hour=_env_int("FOLLOWUP_HOUR", 9),
        ptp_reminder_hour=_env_int("PTP_REMINDER_HOUR", 9),
    )


@lru_cache(maxsize=1)
def get_config() -> Config:
    """Return the cached singleton config."""
    return _build_config()


def reload_config() -> Config:
    """Force a reload from disk + env (useful in tests)."""
    get_config.cache_clear()
    return get_config()
