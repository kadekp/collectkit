# Security Policy

## Reporting a vulnerability

If you discover a security vulnerability, **please do not open a public
GitHub issue**. Instead:

1. Open a private security advisory on GitHub:
   `Security → Report a vulnerability` on the repository page.
2. Or email the maintainers directly (see repo metadata).

Please include:
- A description of the issue
- Steps to reproduce or a proof-of-concept
- The version / commit you found it on
- Any suggested mitigation

We will acknowledge receipt within a few days and aim to provide a
remediation plan within two weeks for confirmed vulnerabilities.

## Scope

In scope:
- The worker code in `src/`
- Operational scripts in `scripts/`
- Default configurations in `.env.example`, `Dockerfile`, `docker-compose.yml`
- Documentation that could lead users to insecure configurations

Out of scope:
- Vulnerabilities in upstream dependencies (report to the project itself)
- Issues that require physical access or already-compromised credentials

## Operating securely

A few non-obvious things to be aware of when running this bot:

- **Database credentials**: store them only in environment variables.
  Never commit `.env` files. Rotate the database password if you suspect
  it has leaked.
- **LLM API keys**: the bot sends customer messages to a third-party LLM
  (OpenRouter by default). Make sure your provider's terms allow your
  use case and that customers are appropriately informed.
- **Prompt injection**: the bot includes basic guardrails, but treat all
  incoming messages as adversarial. Don't pipe LLM output into other
  privileged systems without validation.
- **Webhook authentication**: the receiver service must validate that
  webhooks actually come from your messaging provider. The reference
  receiver (coming in v1.0) will demonstrate this.
