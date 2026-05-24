# Messaging providers

The bot talks to customers through a pluggable **messaging adapter**. Pick
which one by setting `MESSAGING_PROVIDER` in `.env`.

| Provider   | `MESSAGING_PROVIDER` | Best for                                |
|------------|----------------------|-----------------------------------------|
| Null       | `null`               | Local dev, tests, dry runs              |
| Mimin      | `mimin`              | Hosted WhatsApp Business (omnichannel)  |
| Twilio     | `twilio`             | New deployments needing WhatsApp or SMS |
| Telegram   | `telegram`           | Internal tools, low-barrier launches    |

All adapters implement the same two-method interface
(`send_text`, `send_campaign`) defined in
[`src/messaging/base.py`](../src/messaging/base.py).

---

## Null adapter

Logs what would be sent but doesn't talk to any provider. **This is the
default in `docker-compose.yml`** so you can run the bot end-to-end with
zero provider accounts.

```bash
MESSAGING_PROVIDER=null
```

No further config needed. Outbound "sends" show up in worker logs under
`event: null_send_text`.

---

## Mimin.io adapter

A hosted WhatsApp Business API gateway via Mimin's omnichannel chat
and campaign APIs.

```bash
MESSAGING_PROVIDER=mimin
MIMIN_API_TOKEN=<bearer-token>
MIMIN_STORE=<your-store-name>
MIMIN_ACCOUNT_ID=<numeric-account-id>
MIMIN_INBOX_ID=<numeric-inbox-id>
MIMIN_CAMPAIGN_URL=<campaign-base-url>
MIMIN_CAMPAIGN_API_KEY=<campaign-key>
WHATSAPP_SENDER=<digits-only sender number>
```

To dry-run without hitting the API:

```bash
MIMIN_DISABLED=true
```

---

## Twilio adapter

Uses Twilio's REST Messages API. Works for both WhatsApp Business (via
the sandbox or an approved sender) and plain SMS.

```bash
MESSAGING_PROVIDER=twilio
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_FROM=whatsapp:+14155238886   # sandbox; or +15558675309 for SMS
TWILIO_CHANNEL=whatsapp             # or "sms"
```

`send_campaign` doesn't use Twilio Content Templates — instead it
resolves each recipient's body from your `prompts/<locale>/reminders.yaml`
and sends them via the same `Messages.create` call. This keeps the
campaign content under your control without round-tripping through
Twilio's template approval flow. (You can add real template support by
forking `src/messaging/twilio.py` — search for "TODO".)

To dry-run:

```bash
TWILIO_DISABLED=true
```

---

## Telegram adapter

Cheapest channel to set up: create a bot with
[@BotFather](https://t.me/BotFather), copy the token, you're done.

```bash
MESSAGING_PROVIDER=telegram
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_PARSE_MODE=                 # blank, "MarkdownV2", or "HTML"
```

Telegram identifies recipients by numeric `chat_id`, not phone number.
Since the bot's data model is keyed on `phone_number`, the convention is
to **store the customer's Telegram `chat_id` in the `phone_number` column**.
Your receiver service is responsible for that mapping — typically by
storing the `chat_id` from the first `/start` message.

To dry-run:

```bash
TELEGRAM_DISABLED=true
```

---

## Adding your own adapter

1. Create `src/messaging/<your_provider>.py` with a class inheriting
   from `MessagingAdapter` and implementing `send_text` and
   `send_campaign`.
2. Register it in [`src/messaging/__init__.py`](../src/messaging/__init__.py):
   ```python
   def _build_yours() -> MessagingAdapter:
       from .yours import YoursAdapter
       return YoursAdapter()

   _REGISTRY: dict[str, ...] = {
       ...,
       "yours": _build_yours,
   }
   ```
3. Document the required env vars in your provider's docstring and add
   them to `.env.example`.
4. Set `MESSAGING_PROVIDER=yours` and you're done — no callers in `src/`
   need to change.

The base interface is small on purpose. If your provider needs more
features (media attachments, location messages, etc.), add optional
methods to your adapter and call them directly from the worker if you
need them.
