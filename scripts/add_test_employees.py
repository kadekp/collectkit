#!/usr/bin/env python3
"""
Seed synthetic test borrowers into the borrowers & loans tables.

Inserts test borrowers with label='TEST' and realistic-looking data.
Idempotent — safe to run multiple times (uses ON CONFLICT for borrowers,
deletes+reinserts for loans).

All names and phone numbers below are synthetic. Replace with your own
test fixtures before seeding a real database.

Usage:
    python3 scripts/add_test_employees.py --dry-run    # Show what would be inserted
    python3 scripts/add_test_employees.py              # Insert to DB
"""

import os
import sys
import argparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from dotenv import load_dotenv
import psycopg
from psycopg.rows import dict_row

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    print("❌ DATABASE_URL not set")
    sys.exit(1)

# Test employees data (status=UPCOMING — bill due in the future)
BORROWERS = [
    {
        "phone_number": "15555550101",
        "customer_number": "5009000001",
        "customer_name": "Alex Carter",
        "due_date": "2026-02-07",
        "days_late": -5,
        "billing_amount": 107000,
        "status": "UPCOMING",
        "label": "TEST",
        "registration_date": "2025-07-15",
    },
    {
        "phone_number": "15555550102",
        "customer_number": "5009000002",
        "customer_name": "Bailey Morgan",
        "due_date": "2026-02-07",
        "days_late": -5,
        "billing_amount": 139200,
        "status": "UPCOMING",
        "label": "TEST",
        "registration_date": "2025-08-20",
    },
    {
        "phone_number": "15555550103",
        "customer_number": "5009000003",
        "customer_name": "Casey Reed",
        "due_date": "2026-02-07",
        "days_late": -5,
        "billing_amount": 84700,
        "status": "UPCOMING",
        "label": "TEST",
        "registration_date": "2025-06-10",
    },
]

# Loans: billing_amount = sum(loan_amount + loan_admin_fee) per borrower
LOANS = [
    # Alex: 100000 + 7000 = 107000
    {"phone_number": "15555550101", "loan_amount": 100000, "loan_date": "2026-01-07", "loan_admin_fee": 7000},
    # Bailey: (80000 + 5600) + (50000 + 3600) = 139200
    {"phone_number": "15555550102", "loan_amount": 80000, "loan_date": "2026-01-07", "loan_admin_fee": 5600},
    {"phone_number": "15555550102", "loan_amount": 50000, "loan_date": "2026-01-14", "loan_admin_fee": 3600},
    # Casey: 80000 + 4700 = 84700
    {"phone_number": "15555550103", "loan_amount": 80000, "loan_date": "2026-01-07", "loan_admin_fee": 4700},
]


def main():
    parser = argparse.ArgumentParser(description="Add test employees to borrowers table")
    parser.add_argument('--dry-run', action='store_true', help="Show data without inserting")
    args = parser.parse_args()

    print("=" * 60)
    print("ADD TEST EMPLOYEES")
    print("=" * 60)

    # Show what will be inserted
    print(f"\n📋 Borrowers ({len(BORROWERS)}):")
    for b in BORROWERS:
        print(f"   {b['phone_number']} | {b['customer_name']} | {b['billing_amount']:,} | {b['status']} | label={b['label']}")

    print(f"\n📋 Loans ({len(LOANS)}):")
    for l in LOANS:
        print(f"   {l['phone_number']} | {l['loan_amount']:,} + {l['loan_admin_fee']:,} fee | {l['loan_date']}")

    if args.dry_run:
        print(f"\n🔍 DRY RUN — no database changes made")
        return

    # Insert to DB
    print(f"\n💾 Inserting to database...")
    phones = [b['phone_number'] for b in BORROWERS]

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            # Upsert borrowers
            for b in BORROWERS:
                cur.execute("""
                    INSERT INTO borrowers (phone_number, customer_number, customer_name,
                                           due_date, days_late, billing_amount, status, label, registration_date)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (phone_number) DO UPDATE SET
                        customer_number = EXCLUDED.customer_number,
                        customer_name = EXCLUDED.customer_name,
                        due_date = EXCLUDED.due_date,
                        days_late = EXCLUDED.days_late,
                        billing_amount = EXCLUDED.billing_amount,
                        status = EXCLUDED.status,
                        label = EXCLUDED.label,
                        registration_date = EXCLUDED.registration_date
                """, (
                    b['phone_number'], b['customer_number'], b['customer_name'],
                    b['due_date'], b['days_late'], b['billing_amount'],
                    b['status'], b['label'], b['registration_date']
                ))

            # Delete existing loans for these phones, then reinsert
            cur.execute("DELETE FROM loans WHERE phone_number = ANY(%s)", (phones,))

            for l in LOANS:
                cur.execute("""
                    INSERT INTO loans (phone_number, loan_amount, loan_date, loan_admin_fee)
                    VALUES (%s, %s, %s, %s)
                """, (l['phone_number'], l['loan_amount'], l['loan_date'], l['loan_admin_fee']))

            conn.commit()

    print(f"   ✅ Upserted {len(BORROWERS)} borrowers")
    print(f"   ✅ Inserted {len(LOANS)} loans")
    print(f"\n✅ Done!")


if __name__ == "__main__":
    main()
