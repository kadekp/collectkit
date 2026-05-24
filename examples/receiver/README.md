# Reference receiver

A minimal FastAPI app demonstrating the worker's inbound webhook contract.

## Why a separate service?

The worker is a long-running poller — it doesn't accept HTTP connections
from your messaging provider directly. Provider webhooks (Mimin, Twilio,
Telegram) need a small HTTP server in front that drops messages into the
shared database. This is that server.

This reference implementation is intentionally simple so forkers have
a starting point.

## Webhook contract

```http
POST /webhook
Content-Type: application/json
X-Shared-Secret: <optional shared secret>

{
  "phone_number": "15555550101",
  "content": "Hi, when is my bill due?",
  "image_url": null,
  "sender_name": "Alex Carter",
  "is_auto_reply": false
}
```

Response: `{"status": "queued"}` (HTTP 200).

On success the receiver writes one row to `chat_history` (sender=`'user'`,
`is_processed=FALSE`) and upserts the customer's `chat_sessions` row with
`status='needs_reply'`. The worker polls these tables and replies through
the configured `MESSAGING_PROVIDER` adapter.

## Translating from provider payloads

Each provider sends its own webhook format. The receiver above accepts a
normalized payload to keep the example readable. In production, add a thin
provider-specific parser at the top of the handler — for example, for
Twilio you'd read `From` / `Body` from the URL-encoded form body and map
to the normalized shape.

## Running

```bash
cd examples/receiver
pip install -r requirements.txt

DATABASE_URL=postgresql://user:password@localhost:5432/dbname \
  uvicorn main:app --reload --port 8000
```

Or via the repo's root `docker-compose.yml`:

```bash
docker compose up receiver
```

Set `WEBHOOK_SHARED_SECRET` to require an `X-Shared-Secret` header on every
request — recommended for any deployment exposed to the internet.

## What this receiver does NOT do

- Verify provider-specific webhook signatures (Twilio's `X-Twilio-Signature`,
  Mimin's HMAC, etc.) — **add this before going to production**.
- Download / store images (`image_url` is accepted but ignored by this
  reference; the worker expects base64-encoded `image_data` in
  `chat_history` for vision-LLM analysis).
- Rate-limit per phone number.
- Deduplicate by provider message ID.

These are deliberate omissions to keep the reference small. Add the
provider-specific bits you need before going to production.
