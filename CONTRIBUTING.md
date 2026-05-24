# Contributing

Thanks for your interest in improving this project. This document covers the
basics; ask if anything's unclear.

## Reporting bugs

Open a GitHub issue. Please include:
- What you expected to happen
- What actually happened
- Steps to reproduce (minimal example if possible)
- Your environment: Python version, OS, relevant `.env` values (redacted)

For **security issues**, do NOT open a public issue. See [SECURITY.md](SECURITY.md).

## Proposing changes

1. Fork the repo and create a branch off `main`.
2. Make your change with a focused commit message.
3. Run the linter and tests locally (see below).
4. Open a PR. Reference any related issue.

## Development setup

```bash
git clone <your-fork>
cd <repo>

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Fill in at least DATABASE_URL and OPENROUTER_API_KEY
```

## Running locally

```bash
python3 -m src.worker
```

The worker exposes `http://localhost:8080/live` for health checks.

## Style

- Keep it simple. Prefer clear code over clever code.
- Match the existing style (no formatter is enforced yet; consistency wins).
- Comments only when the **why** isn't obvious from the code.
- Don't add features beyond the scope of the issue you're fixing.

## What we're looking for

The project is mid-generalization (see the roadmap in [README.md](README.md)).
High-value contributions right now:

- **v0.2 work**: parameterizing locale / timezone / currency / persona /
  business rules out of hardcoded constants.
- **v1.0 work**: messaging adapters (Twilio, Telegram, 360dialog) behind a
  common interface.
- **Documentation**: especially a polished quickstart and "adapt this for
  your business" guide.
- **Tests**: a locale-agnostic unit test suite.

## What we're not looking for (yet)

- Large refactors that change architectural direction without prior discussion.
- New external dependencies that aren't well-justified.
- Cosmetic-only changes to working code.
