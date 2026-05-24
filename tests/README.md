# Tests

A small suite of pure-function smoke tests that runs without a database
or a live LLM. Designed to catch import-time breakage and obvious
regressions in helpers like locale formatting and guardrail rules.

These tests **gate merges to `main`** — CI (`.github/workflows/ci.yml`)
runs them on every push and PR with no soft-fail. Keep them green.

## Running

```bash
pip install -r requirements.txt
pip install pytest
pytest tests/ -q
```

## What's here

- `test_smoke.py` — imports each module in `src/` and exercises a handful
  of pure helpers (i18n formatting, timezone helpers, guardrail keyword
  matching).

## What's not here

End-to-end tests against a live PostgreSQL + LLM stack are intentionally
out of scope for the public template — they depend too tightly on the
specific data and persona of the deployment that runs them. Build them
in your own fork once you've filled in `config/persona.yaml`, your
prompt bundle, and your messaging provider credentials.
