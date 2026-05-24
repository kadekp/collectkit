# Example conversations

Three short dialogues showing how the default bot ("Maya", for the
fictional "Acme Pay" product) handles common situations. Every reply
follows the rules in [`prompts/en/system_prompt.md`](../prompts/en/system_prompt.md)
and the strategies in [`prompts/en/strategies.yaml`](../prompts/en/strategies.yaml).

All three customers below match the synthetic borrowers seeded by
[`scripts/add_test_employees.py`](../scripts/add_test_employees.py), so you can
replay any of these flows locally after running the [Quickstart](QUICKSTART.md).

> Persona, tone, business rules, and language are all driven by
> [`config/persona.example.yaml`](../config/persona.example.yaml) and the YAML
> files in [`prompts/en/`](../prompts/en/). Change those, not the Python.

---

## 1. Promise to Pay — the happy path

**Customer:** Alex Carter · `+1 555 555 0101` · bill of **$1,070.00** due in 5 days · status `UPCOMING`

> **Alex** — Hey, when is my bill due again?

> **Maya** — Hi Alex! Your Acme Pay bill of **$1,070.00** is due on
> **Feb 7, 2026** — that's 5 days from now. Want me to send payment
> instructions?

> **Alex** — Not today, money's tight. I can pay on the 9th, payday.

> **Maya** — Totally understand. I've recorded a Promise to Pay for
> **$1,070.00 on Feb 9, 2026**. I'll check back with you then. Thanks
> Alex!

<sub>**Under the hood:** Maya called `record_promise_to_pay` (see
[`src/agent.py`](../src/agent.py)). The empathetic, low-pressure tone comes
from the `UPCOMING` strategy in
[`prompts/en/strategies.yaml`](../prompts/en/strategies.yaml).</sub>

---

## 2. "I already paid" — verify with a screenshot

**Customer:** Bailey Morgan · `+1 555 555 0102` · bill of **$1,392.00** · status `UPCOMING`

> **Bailey** — already paid this morning, all good 👍

> **Maya** — Thanks Bailey! Could you share the payment receipt or
> a screenshot? I'll get it verified on our side.

> **Bailey** — *\[sends a screenshot of a bank transfer confirmation]*

> **Maya** — Got it — verification is scheduled. I'll follow up the
> moment it's confirmed. Thanks for sending the proof so quickly!

<sub>**Under the hood:** Maya called `schedule_payment_verification` instead
of `record_promise_to_pay`, because the customer claimed payment had
already happened. The screenshot is handled by
[`src/image_analyzer.py`](../src/image_analyzer.py), which runs the image
through a vision LLM before the verification job in
[`src/payment_checker.py`](../src/payment_checker.py) confirms the
transaction against the back office.</sub>

---

## 3. Frustrated customer — graceful human handoff

**Customer:** Casey Reed · `+1 555 555 0103` · bill of **$847.00** · status `UPCOMING`

> **Casey** — This is the third time I'm asking. Just give me a real
> person.

> **Maya** — I hear you, Casey — sorry for the frustration. I'm
> connecting you with our human support team right now. Someone will
> reach out shortly.

<sub>**Under the hood:** Maya called `request_human_handoff` with the reason
`"customer repeatedly requested a human agent"` (see
[`src/agent.py`](../src/agent.py)). The session is flagged so the worker
stops auto-replying — your CS team takes over from here through whichever
channel you wire in. Maya stays polite and never argues; that behavior
lives in the **Tone** and **Guardrails** sections of
[`prompts/en/system_prompt.md`](../prompts/en/system_prompt.md).</sub>

---

## Reproduce these locally

After running [Quickstart](QUICKSTART.md), send Alex's opening line:

```bash
curl -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -d '{"phone_number":"15555550101","content":"Hey, when is my bill due again?","sender_name":"Alex Carter"}'
```

Then tail the worker logs (`docker compose logs -f worker`) or inspect
`chat_history` in Postgres to see Maya's reply.

To change Maya's name, company, currency, or tone — see
[ADAPTING.md](ADAPTING.md).
