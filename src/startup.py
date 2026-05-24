"""
Startup validation helpers.

Kept in a tiny standalone module (rather than inside `worker.py`) so the
checks can run without dragging in the worker's heavyweight imports
(newrelic, langsmith, langgraph) — useful for tests and for tooling that
wants to validate a config without booting the worker.
"""

from __future__ import annotations

import os
from pathlib import Path


_REQUIRED_ENV = ("DATABASE_URL", "OPENROUTER_API_KEY")


def validate_env() -> None:
    """Validate required env vars and the prompt bundle at startup.

    Fails loudly here rather than crashing on the first inbound message —
    that delay can be hours in daemon mode.
    """
    from .config import REPO_ROOT

    missing = [var for var in _REQUIRED_ENV if not os.getenv(var)]
    if missing:
        raise ValueError(
            f"Missing required environment variables: {', '.join(missing)}"
        )

    # Only system_prompt.md is mandatory; YAML siblings fall back to empty
    # dicts in src.config._load_yaml.
    prompt_dir = Path(os.getenv("PROMPT_DIR", "prompts/en"))
    if not prompt_dir.is_absolute():
        prompt_dir = REPO_ROOT / prompt_dir
    if not (prompt_dir / "system_prompt.md").exists():
        raise FileNotFoundError(
            f"PROMPT_DIR={prompt_dir} is missing system_prompt.md. "
            f"Copy prompts/en/ as a starting point and translate from there."
        )
