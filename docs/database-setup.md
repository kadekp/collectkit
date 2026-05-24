# Database Setup

This document contains the SQL script to set up the database tables and mock data for the worker.

`docs/schema.sql` is the authoritative version (auto-applied by docker-compose
on first boot of the `postgres` service). The SQL inlined below is duplicated
here for human reading.

## Prerequisites

- PostgreSQL (any modern version)
- Access to run SQL queries

## Tables Overview

| Table | Purpose |
|-------|---------|
| `borrowers` | Customer profiles with billing info |
| `loans` | Historical loan records per borrower |
| `ptp` | Promise to Pay commitments |
| `chat_sessions` | Conversation state tracking |
| `chat_history` | Message log for LLM context |
| `scheduled_tasks` | Scheduled payment verification tasks |

---

## SQL Setup Script

### 1. Create Tables

```sql
-- ============================================================
-- BUSINESS TABLES
-- ============================================================

-- Borrower profiles
CREATE TABLE IF NOT EXISTS borrowers (
    phone_number TEXT PRIMARY KEY,
    customer_number TEXT NOT NULL,
    customer_name TEXT NOT NULL,
    due_date TEXT NOT NULL,
    days_late INT DEFAULT 0,
    billing_amount FLOAT DEFAULT 0,
    status TEXT DEFAULT 'UPCOMING',  -- one of: UPCOMING, OVERDUE, PAID
    label TEXT DEFAULT NULL,
    registration_date TEXT NOT NULL        -- Format: YYYY-MM-DD (e.g., 2025-09-27)
);

-- Index for efficient analytics queries on label
CREATE INDEX IF NOT EXISTS idx_borrowers_label ON borrowers(label);

-- Loan records (history of transactions)
CREATE TABLE IF NOT EXISTS loans (
    id SERIAL PRIMARY KEY,
    phone_number TEXT NOT NULL REFERENCES borrowers(phone_number),
    loan_amount FLOAT NOT NULL,
    loan_date TEXT NOT NULL,
    loan_admin_fee FLOAT DEFAULT 0
);

-- Promise to Pay records
CREATE TABLE IF NOT EXISTS ptp (
    id SERIAL PRIMARY KEY,
    phone_number TEXT NOT NULL,
    promise_amount FLOAT NOT NULL,
    promise_date DATE NOT NULL,
    status TEXT DEFAULT 'PENDING',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- SESSION TABLES
-- ============================================================

-- Conversation state per user
CREATE TABLE IF NOT EXISTS chat_sessions (
    phone_number TEXT PRIMARY KEY,
    last_message_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status TEXT DEFAULT 'idle',
    last_followup_at DATE DEFAULT NULL,   -- Tracks last 9 AM follow-up sent date
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Index for efficient morning follow-up query
CREATE INDEX IF NOT EXISTS idx_chat_sessions_followup 
ON chat_sessions (last_followup_at);

-- Message history for LLM context
CREATE TABLE IF NOT EXISTS chat_history (
    id SERIAL PRIMARY KEY,
    phone_number TEXT NOT NULL,
    sender TEXT NOT NULL,
    message_content TEXT NOT NULL,
    image_data TEXT,                      -- Base64-encoded image (nullable)
    is_processed BOOLEAN DEFAULT FALSE,
    is_auto_reply BOOLEAN DEFAULT FALSE,  -- WhatsApp Business auto-reply flag
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- SCHEDULED TASKS TABLE
-- ============================================================

-- Scheduled tasks (payment verification)
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id SERIAL PRIMARY KEY,
    phone_number TEXT NOT NULL,
    customer_number TEXT NOT NULL,
    task_type TEXT DEFAULT 'payment_check',
    scheduled_at TIMESTAMP NOT NULL,
    status TEXT DEFAULT 'pending',
    result TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Index for efficient polling of due tasks
CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_due
ON scheduled_tasks (status, scheduled_at)
WHERE status = 'pending';

-- Index for efficient unprocessed message polling
CREATE INDEX IF NOT EXISTS idx_chat_history_unprocessed
ON chat_history (phone_number, sender, is_processed)
WHERE is_processed = FALSE;
```

### 2. Insert Mock Data

```sql
-- ============================================================
-- MOCKUP BORROWERS (5 users with varied statuses)
-- ============================================================

INSERT INTO borrowers (phone_number, customer_number, customer_name, due_date, days_late, billing_amount, status, label, registration_date) VALUES
('15555550100', '5009000100', 'Alex Carter',    '2025-12-07',  10, 206.00, 'OVERDUE',  NULL,  '2025-01-10'),
('15555550101', '5009000101', 'Bailey Morgan',  '2025-11-07',  45, 356.00, 'OVERDUE', 'TEST', '2025-03-15'),
('15555550102', '5009000102', 'Casey Reed',     '2026-01-07', -10, 103.00, 'UPCOMING','TEST', '2025-04-20'),
('15555550103', '5009000103', 'Dakota Lee',     '2025-12-07',   0,   0.00, 'PAID',    NULL,  '2025-02-10'),
('15555550104', '5009000104', 'Emerson Quinn',  '2025-10-07',  75, 503.00, 'OVERDUE', 'TEST', '2025-05-25')
ON CONFLICT (phone_number) DO NOTHING;

-- ============================================================
-- MOCKUP LOANS (1-2 loans per borrower)
-- ============================================================

INSERT INTO loans (phone_number, loan_amount, loan_date, loan_admin_fee) VALUES
('15555550100', 150.00, '2025-11-20', 3.00),
('15555550100',  50.00, '2025-11-25', 3.00),
('15555550101', 200.00, '2025-10-10', 3.00),
('15555550101', 150.00, '2025-10-15', 3.00),
('15555550102', 100.00, '2025-12-28', 3.00),
('15555550103', 250.00, '2025-11-05', 3.00),
('15555550103', 100.00, '2025-11-10', 3.00),
('15555550104', 500.00, '2025-09-15', 3.00);
```

---

## Mock Data Summary

| # | Name | Phone | Customer # | Status | Days Late | Billing | Loans | Registration | Bot Tone |
|---|------|-------|------------|--------|-----------|---------|-------|--------------|----------|
| 1 | **Alex Carter**  | 15555550100 | 5009000100 | OVERDUE  |  10 | 206.00 | 2 | 2025-01-10 | Concerned partner |
| 2 | Bailey Morgan    | 15555550101 | 5009000101 | OVERDUE  |  45 | 356.00 | 2 | 2025-03-15 | Firm but kind |
| 3 | Casey Reed       | 15555550102 | 5009000102 | UPCOMING | -10 | 103.00 | 1 | 2025-04-20 | Friendly reminder |
| 4 | Dakota Lee       | 15555550103 | 5009000103 | PAID     |   0 |   0.00 | 2 | 2025-02-10 | Appreciative |
| 5 | Emerson Quinn    | 15555550104 | 5009000104 | OVERDUE  |  75 | 503.00 | 1 | 2025-05-25 | Formal, serious |

*Billing amount = total loans + admin fees (varies per transaction). Currency follows your `CURRENCY_CODE` env var.*

---

## Status Values

| Status | Meaning | Bot Behavior |
|--------|---------|--------------|
| `UPCOMING` | Bill upcoming (days_late is negative) | Friendly reminder |
| `OVERDUE` | Overdue (days_late is positive) | Graduated empathy based on days |
| `PAID` | Paid | Appreciative, thankful |

---

## Verification Queries

```sql
-- Check all tables exist
SELECT table_name FROM information_schema.tables
WHERE table_schema = 'public' ORDER BY table_name;

-- Check borrowers
SELECT customer_name, phone_number, status, days_late, billing_amount
FROM borrowers ORDER BY customer_number;

-- Check loans with borrower names
SELECT b.customer_name, l.loan_amount, l.loan_date
FROM loans l
JOIN borrowers b ON l.phone_number = b.phone_number
ORDER BY b.customer_number, l.loan_date;

-- Count records
SELECT 'borrowers' as table_name, COUNT(*) as count FROM borrowers
UNION ALL
SELECT 'loans', COUNT(*) FROM loans
UNION ALL
SELECT 'ptp', COUNT(*) FROM ptp
UNION ALL
SELECT 'scheduled_tasks', COUNT(*) FROM scheduled_tasks;
```

---

## Notes

1. **Phone number format**: Use international format without `+` (e.g., `15555550100`)

2. **LangGraph tables**: Automatically created by `PostgresSaver.setup()` when the worker runs

3. **Data sync**: In production, borrower data should be synced from your core lending / banking system

4. **Customer number format**: Use numeric-only strings (e.g., `5009000100`). No alphabetic characters.
