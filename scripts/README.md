# Scripts

Utility scripts for operating the bot.

## Included

- `add_test_employees.py` — seed synthetic test borrowers + loans into the
  database. Useful for trying the bot end-to-end against your own DB. All
  names and phone numbers are synthetic; edit them before seeding production.
- `cleanup_stale_memory.py` — purge LangGraph checkpoint rows for borrowers
  that no longer need bot memory (e.g. after a billing cycle resets).
- `manual_payment_check.py` — manually trigger a payment-status check for a
  single phone number. Useful for debugging the payment-verification flow.

## Adding your own

If you need additional one-off tooling for your own deployment (bulk
data migration, label backfills, incident-response cleanup), write small
scripts against your own schema and check them into your fork.
