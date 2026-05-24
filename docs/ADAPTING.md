# Adapting the bot for your business

Most of what makes the bot feel like *your* product lives in config and
prompts, not code. This doc walks through the four things you typically
change when forking.

## 1. Pick a locale + currency + timezone

In `.env`:

```bash
TIMEZONE=America/New_York   # any IANA name
LOCALE=en_US                # any babel locale (en_US, id_ID, fr_FR, ...)
CURRENCY_CODE=USD           # any ISO 4217 code
```

These drive `format_currency` (`$500.00` vs `Rp 500.000,00`),
`format_date_long` (`January 27, 2026` vs `27 Januari 2026`), and the
scheduled-job hours (when the worker thinks 9 AM is). No code changes.

## 2. Write a persona

Copy `config/persona.example.yaml` to `config/persona.yaml` (it's
gitignored) and edit:

```yaml
bot_name: Aiden
company_name: Riverstone Credit
product_name: Riverstone Card
support_handoff_channel: "the Riverstone support team"

business_rules:
  max_loan_amount: 5000
  due_date_day_of_month: 15
  payment_methods:
    - ACH transfer
    - Debit card
    - Apple Pay
  allows_partial_payment: true
  allows_extension: true
  late_fee_policy: "Late payments may be reported to TransUnion and Experian."
```

Every key here becomes available in your prompt template as a
placeholder. `bot_name` → `{bot_name}`, `payment_methods` →
`{rule_payment_methods}`, etc.

## 3. Customize the prompt bundle

The shipped `prompts/en/` is a reasonable English baseline. To deviate:

```bash
cp -r prompts/en prompts/en-riverstone
```

Then edit:

- `prompts/en-riverstone/system_prompt.md` — the master prompt; tone,
  rules, escalation criteria
- `prompts/en-riverstone/strategies.yaml` — what the bot says at each
  days-late tier (the most product-specific content)
- `prompts/en-riverstone/followups.yaml` — the morning follow-up
  messages
- `prompts/en-riverstone/guardrails.yaml` — injection patterns and
  on-topic keywords your bot should recognize
- `prompts/en-riverstone/reminders.yaml` — PTP campaign messages
  (due today / due tomorrow / missed)

Point the worker at your bundle:

```bash
PROMPT_DIR=prompts/en-riverstone
```

## 4. Plug in payment-link generation

Your business has its own way of giving customers a link to pay. Set:

```bash
PAYMENT_LINK_BASE=https://pay.riverstone.example.com/url
```

The bot appends `/<customer_number>` to that base for every link. If you
need richer logic (signed URLs, per-customer tokens), edit
`generate_payment_link` in `src/ptp_reminder.py` — it's a 6-line
function.

For payment-status verification (the "did they pay?" check), set:

```bash
PAYMENT_PAID_INDICATOR="paid"     # substring that appears in the payment
                                  # page when the bill is settled
```

The `payment_checker.py` module fetches `PAYMENT_LINK_BASE/{customer}`
and looks for that substring. Swap it for a real API call by editing
`check_payment_status` if your provider exposes one.

## 5. Pick a messaging provider

See [PROVIDERS.md](PROVIDERS.md). Switching between Mimin, Twilio,
Telegram, or your own adapter is a one-env-var change
(`MESSAGING_PROVIDER=...`).

---

## What you (still) need to write yourself

These haven't been generalized:

- **Customer-data loader** — `src/database_pg.fetch_borrower_data` reads
  a fixed schema (see `docs/schema.sql`). If your borrower data lives
  somewhere else (a different table, a microservice, a CRM), pass your
  own `fetch_borrower_fn` to `build_graph()`.
- **The webhook receiver** — `examples/receiver/main.py` accepts a
  normalized JSON payload. To accept your provider's native webhook
  format, add a small parser at the top of the handler that maps the
  provider fields to the `IncomingMessage` model.

If you build either of these for a popular setup, PRs welcome.
