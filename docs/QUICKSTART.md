# Quickstart

Goal: clone the repo, run the bot locally, send a test message, see it
reply — in about 15 minutes. No real WhatsApp account required.

## Prerequisites

- Docker (with Compose v2)
- An [OpenRouter](https://openrouter.ai/) API key (or any
  OpenAI-compatible endpoint)

That's it. The default config uses the **null messaging adapter** so you
don't need a Mimin / Twilio / Telegram account to try the loop end-to-end.

## 1. Clone and configure

```bash
git clone <your-fork-url>
cd <repo>

cp .env.example .env
```

Open `.env` and set, at minimum:

```bash
OPENROUTER_API_KEY=sk-or-...
MESSAGING_PROVIDER=null
```

Everything else can stay at the shipped defaults — locale is English/USD,
timezone UTC, prompts are the generic English bundle.

## 2. Start the stack

```bash
docker compose up --build
```

Three containers come up:

- `postgres` — schema is auto-applied from `docs/schema.sql` on first boot
- `receiver` — FastAPI app on `http://localhost:8000`
- `worker` — the bot brain, with health check on `http://localhost:8080/live`

You should see worker logs like `Background task thread started` and
`Polling for sessions needing reply`.

## 3. Seed a test customer

In a second terminal:

```bash
docker compose exec worker python3 scripts/add_test_employees.py
```

This inserts three synthetic borrowers (Alex Carter at `15555550101`,
Bailey Morgan at `15555550102`, Casey Reed at `15555550103`).

## 4. Send an incoming message

```bash
curl -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -d '{
        "phone_number": "15555550101",
        "content": "Hi, when is my bill due?",
        "sender_name": "Alex Carter"
      }'
```

The receiver writes the message to `chat_history` and marks the session
`needs_reply`. The worker picks it up within 2 seconds, debounces for 4
seconds, then calls the LLM.

## 5. Watch it reply

Watch the worker logs:

```bash
docker compose logs -f worker
```

You'll see entries like:

```
LLM request started ...
LLM response received ...
Null adapter — text would be sent {"event": "null_send_text", ...}
```

Or inspect the reply directly in the DB:

```bash
docker compose exec postgres psql -U bot -d bot \
  -c "SELECT sender, message_content FROM chat_history WHERE phone_number = '15555550101' ORDER BY id;"
```

You should see your incoming message followed by a `'bot'` row with the
generated reply.

## 6. Try a different locale

Stop the stack (`Ctrl-C`), then in `.env` swap in your target locale —
for example Spanish (Spain):

```bash
PROMPT_DIR=prompts/es                 # you'll create this dir; copy from prompts/en/
PERSONA_CONFIG=config/persona.yaml    # your filled-in persona
LOCALE=es_ES
CURRENCY_CODE=EUR
TIMEZONE=Europe/Madrid
```

Copy `prompts/en/` to `prompts/es/`, translate the strings inside, then
restart the stack and send the same curl. The bot now replies in
Spanish with EUR formatting and Madrid-local timestamps. See
[ADAPTING.md](ADAPTING.md) for the full customization guide.

## Next steps

- [PROVIDERS.md](PROVIDERS.md) — wire up a real messaging provider
  (Mimin / Twilio / Telegram).
- [ADAPTING.md](ADAPTING.md) — customize the bot for your own business.
- [ARCHITECTURE.md](ARCHITECTURE.md) — module map and request flow.
