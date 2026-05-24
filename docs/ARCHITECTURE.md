# Architecture

## Two services, one database

```
   Customer
      │  (1) WhatsApp / SMS / Telegram message
      ▼
┌─────────────┐    (2) webhook
│  Messaging  │ ──────────────────────►  ┌──────────────┐
│  provider   │                          │  Receiver    │  examples/receiver/
└─────────────┘                          │  (FastAPI)   │
      ▲                                  └──────┬───────┘
      │ (5) send_text reply                     │ (3) INSERT chat_history
      │                                          │     UPSERT chat_sessions
      │                                          ▼
      │                                  ┌──────────────┐
      │                                  │  PostgreSQL  │
      │                                  └──────┬───────┘
      │                                          │
      │                                          │ (4) polls every 2s
      │                                          ▼
      │                                  ┌──────────────┐
      └──────────────────────────────────┤   Worker     │  src/
                                         │  (LangGraph) │
                                         └──────────────┘
```

1. Customer sends a message via WhatsApp / Telegram / SMS.
2. The provider POSTs a webhook to **the receiver**.
3. The receiver writes the message to `chat_history` and flips the
   customer's `chat_sessions.status` to `needs_reply`.
4. The **worker** polls `chat_sessions` every 2 seconds. After a 4-second
   debounce window of silence, it aggregates unprocessed messages,
   invokes the LangGraph agent, and gets a reply.
5. The worker sends the reply through the configured **messaging adapter**.

This split exists because the worker needs to run as a long-lived
background process (it also does scheduled jobs like morning reminders)
while the receiver needs to accept short-lived HTTP requests at provider
speed. They share state through PostgreSQL, never talk directly.

## Module map (`src/`)

```
src/
├── worker.py                main entry: polling loop + 5 scheduled jobs
├── agent.py                 LangGraph ReAct graph + system-prompt builder
├── database_pg.py           PostgreSQL data layer (raw SQL + pydantic models)
├── config.py                env + persona + prompt bundle loader (cached singleton)
├── i18n.py                  babel-powered currency / date formatting
├── timezone_utils.py        now_local / today_local (uses TIMEZONE env var)
├── guardrails.py            input/output validation (regex + keyword lists)
├── followup_messages.py     morning-followup template selector
├── ptp_reminder.py          PTP campaign orchestrator (due / D-1 / missed)
├── payment_checker.py       polls a payment URL; "paid" / "needs_human"
├── image_analyzer.py        vision LLM for payment-proof screenshots
├── health.py                /live HTTP server (port 8080)
├── logging_config.py        structured JSON logs
├── metrics.py               in-process counter store
│
├── messaging/               pluggable provider adapters (Tier 3)
│   ├── base.py                MessagingAdapter ABC
│   ├── mimin.py               Mimin.io
│   ├── twilio.py              Twilio WhatsApp / SMS
│   ├── telegram.py            Telegram Bot API
│   └── null.py                no-op (default for local dev)
│
└── prompts/                 (removed in v0.2; see prompts/ at repo root)
```

## Configuration layers

The bot resolves runtime behavior from three layers, top to bottom:

1. **Environment variables** (`.env`)
   - Credentials, hosts, hour schedules, locale / timezone / currency,
     and which prompt bundle and messaging provider to load.
2. **Persona file** (`config/persona.yaml`, falls back to
   `config/persona.example.yaml`)
   - Bot name, company, product, support handoff label, business
     rules (max loan, payment methods, late-fee policy).
3. **Prompt bundle** (`prompts/<locale>/` selected by `PROMPT_DIR`)
   - `system_prompt.md`, `followups.yaml`, `guardrails.yaml`,
     `strategies.yaml`, `reminders.yaml`.

Every customer-facing string the bot can produce lives in layers 2 + 3.
Adopting the bot for a new business is — in the typical case — editing a
YAML file and a Markdown file. No Python changes required.

## The LangGraph agent

The agent is a vanilla ReAct loop with one twist: the system prompt is
rebuilt for each conversation turn so it reflects the current customer's
state (days late, outstanding amount, account history). The graph has
three nodes:

```
START → loader → agent → (has_tool_calls?) → tools ──┐
                   ▲                                  │
                   └──────────────────────────────────┘
                                                     │
                                                     ▼ (no tool calls)
                                                    END
```

- `loader` — fetches the borrower record from PostgreSQL.
- `agent` — builds the system prompt, calls the LLM with the tool list
  bound, returns the response.
- `tools` — executes any tool calls the LLM emitted and feeds results
  back to the agent for the next turn.

Tools available to the agent:
- `record_promise_to_pay(amount, date)` — writes a PTP row
- `get_ptp_history()` — reads back the customer's PTP history
- `request_human_handoff(reason)` — flags the session for human takeover
- `schedule_payment_verification()` — queues a job to recheck payment
  status in 1 hour (only attached when the function is wired in)

## Scheduled jobs (background thread in `worker.py`)

| Schedule           | Job                                  | Purpose                                       |
|--------------------|--------------------------------------|-----------------------------------------------|
| every 60 s         | `process_scheduled_tasks`            | Run any due `scheduled_tasks` (payment checks) |
| `PTP_CHECK_HOUR`   | `process_ptp_expiry`                 | Mark missed PTPs as MISSED                    |
| `PTP_CHECK_HOUR`   | `process_paid_borrower_cleanup`      | Clear stale memory for paid borrowers         |
| `BULK_SYNC_HOURS`  | `process_bulk_borrower_sync`         | Refresh `days_late` for all borrowers         |
| `FOLLOWUP_HOUR`    | `process_morning_followup` (Mon–Sat) | Send the 9 AM follow-up to overdue borrowers  |
| `PTP_REMINDER_HOUR`| `process_ptp_reminders`              | Send due / D-1 / missed PTP campaigns         |

All hours are in the configured local timezone (`TIMEZONE`). All hour
values are env-var-overridable.

## Glossary

- **PTP** — Promise to Pay. A customer's commitment to pay a specific
  amount on a specific future date.
- **DPD** — Days Past Due. How long a bill has been overdue (positive)
  or how many days remain until the due date (negative).
- **`borrowers.status` enum** — `UPCOMING` (bill not yet due),
  `OVERDUE` (past due date, unpaid), `PAID` (settled). Presentation
  strings come from `prompts/<locale>/strategies.yaml`.
- **ReAct** — Reasoning + Acting: the LLM agent pattern where the model
  alternates between thinking and calling tools.
