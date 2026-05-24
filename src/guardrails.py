"""
Lightweight input/output guardrails.

Pattern lists and on-topic keywords are loaded from
`prompts/<locale>/guardrails.yaml` via `src.config`.
"""

from __future__ import annotations

import re
from functools import lru_cache

from .config import get_config
from .logging_config import get_logger

logger = get_logger(__name__)


# Sensible English fallbacks if the YAML is empty.
_DEFAULT_INJECTION_PATTERNS: list[str] = [
    r"(?i)ignore\s+(all\s+)?(previous|above|prior)\s+(instructions?|prompts?|rules?)",
    r"(?i)disregard\s+(all\s+)?(previous|above|prior)",
    r"(?i)forget\s+(all\s+)?(previous|everything|your)",
    r"(?i)you\s+are\s+now\s+(a|an|the)",
    r"(?i)pretend\s+(to\s+be|you\s+are)",
    r"(?i)new\s+instructions?\s*:",
    r"(?i)^system\s*:",
    r"(?i)override\s+(your|the|all)",
    r"(?i)jailbreak",
]
_DEFAULT_ON_TOPIC_KEYWORDS: list[str] = [
    "bill",
    "payment",
    "pay",
    "due",
    "balance",
    "account",
    "support",
    "help",
    "thanks",
]
_DEFAULT_MIN_CHECK_LENGTH = 150


@lru_cache(maxsize=1)
def _compiled_patterns() -> list[re.Pattern]:
    bundle = get_config().guardrails or {}
    patterns = bundle.get("injection_patterns") or _DEFAULT_INJECTION_PATTERNS
    return [re.compile(p) for p in patterns]


@lru_cache(maxsize=1)
def _on_topic_keywords() -> list[str]:
    bundle = get_config().guardrails or {}
    kws = bundle.get("on_topic_keywords") or _DEFAULT_ON_TOPIC_KEYWORDS
    return [k.lower() for k in kws]


def _min_check_length() -> int:
    bundle = get_config().guardrails or {}
    return int(bundle.get("min_check_length", _DEFAULT_MIN_CHECK_LENGTH))


def check_prompt_injection(text: str) -> tuple[bool, str]:
    """Return (is_safe, reason). is_safe=True means no injection detected."""
    for pattern in _compiled_patterns():
        match = pattern.search(text)
        if match:
            logger.warning(
                "Prompt injection detected",
                extra={
                    "event": "guardrail_prompt_injection",
                    "matched": match.group(),
                },
            )
            return False, "prompt_injection"
    return True, ""


def check_output_on_topic(text: str) -> tuple[bool, str]:
    """Return (is_safe, reason). Short replies (< min_check_length) always pass."""
    if len(text) < _min_check_length():
        return True, ""

    text_lower = text.lower()
    for keyword in _on_topic_keywords():
        if keyword in text_lower:
            return True, ""

    logger.warning("Off-topic response detected", extra={"event": "guardrail_off_topic"})
    return False, "off_topic"


def validate_input(text: str) -> tuple[bool, str]:
    """Validate user input. Returns (passed, guardrail_name)."""
    return check_prompt_injection(text)


def validate_output(text: str) -> tuple[bool, str]:
    """Validate bot output. Returns (passed, guardrail_name)."""
    return check_output_on_topic(text)
