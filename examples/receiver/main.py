"""
Reference receiver — minimal FastAPI app demonstrating the worker's
expected inbound contract.

The worker (in ../../src/worker.py) polls the `chat_history` and
`chat_sessions` tables. This receiver writes to those tables when a
webhook arrives, so the worker can pick the message up and reply.

This is intentionally minimal:
- Single `POST /webhook` endpoint accepting a flat JSON payload.
- Optional shared-secret header check.
- No provider-specific signature verification (Twilio, Mimin, Telegram
  each have their own — see PROVIDERS.md and add the verification you
  need for production).

Production receivers should also:
- Validate the provider's webhook signature.
- Rate-limit per phone number.
- Deduplicate by provider message ID.
- Log to a real audit table (`whatsapp_messages` in the upstream schema).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Optional

import psycopg
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

app = FastAPI(title="collectkit reference receiver", version="0.1")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATABASE_URL = os.getenv("DATABASE_URL")
WEBHOOK_SHARED_SECRET = os.getenv("WEBHOOK_SHARED_SECRET")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is required")


@contextmanager
def db_cursor():
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            yield cur
        conn.commit()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class IncomingMessage(BaseModel):
    """Inbound webhook payload.

    Fields:
        phone_number: Customer's phone number (digits only) or the
            channel-specific identifier (e.g. Telegram chat_id).
        content: Plain-text message body.
        image_url: Optional image attachment URL (the receiver downloads
            and base64-encodes; the worker analyses via vision LLM).
        sender_name: Optional name reported by the channel.
        is_auto_reply: Set True for provider-generated auto-replies that
            shouldn't trigger a bot response.
    """
    phone_number: str
    content: str
    image_url: Optional[str] = None
    sender_name: Optional[str] = None
    is_auto_reply: bool = False


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/live")
def health():
    return {"status": "ok"}


@app.post("/webhook")
def webhook(
    msg: IncomingMessage,
    x_shared_secret: Optional[str] = Header(default=None, alias="X-Shared-Secret"),
):
    if WEBHOOK_SHARED_SECRET and x_shared_secret != WEBHOOK_SHARED_SECRET:
        raise HTTPException(status_code=401, detail="Invalid shared secret")

    with db_cursor() as cur:
        # 1. Append the message to chat_history (worker reads from here)
        cur.execute(
            """
            INSERT INTO chat_history (phone_number, sender, message_content,
                                      is_processed, is_auto_reply, image_data)
            VALUES (%s, 'user', %s, FALSE, %s, NULL)
            """,
            (msg.phone_number, msg.content, msg.is_auto_reply),
        )
        # 2. Mark the session as needing a reply (worker polls for this)
        cur.execute(
            """
            INSERT INTO chat_sessions (phone_number, last_message_at, status)
            VALUES (%s, NOW(), 'needs_reply')
            ON CONFLICT (phone_number) DO UPDATE
              SET last_message_at = NOW(),
                  status = 'needs_reply'
            """,
            (msg.phone_number,),
        )

    return {"status": "queued"}
