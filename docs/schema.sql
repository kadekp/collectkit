-- Bot database schema.
-- Apply with: psql "$DATABASE_URL" -f docs/schema.sql
-- (Auto-applied at first boot of the `postgres` service in docker-compose.yml.)

-- ============================================================
-- BUSINESS TABLES
-- ============================================================

CREATE TABLE IF NOT EXISTS borrowers (
    phone_number TEXT PRIMARY KEY,
    customer_number TEXT NOT NULL,
    customer_name TEXT NOT NULL,
    due_date TEXT NOT NULL,
    days_late INT DEFAULT 0,
    billing_amount FLOAT DEFAULT 0,
    status TEXT DEFAULT 'UPCOMING',  -- one of: UPCOMING, OVERDUE, PAID
    label TEXT DEFAULT NULL,
    registration_date TEXT NOT NULL        -- Format: YYYY-MM-DD
);

CREATE INDEX IF NOT EXISTS idx_borrowers_label ON borrowers(label);

CREATE TABLE IF NOT EXISTS loans (
    id SERIAL PRIMARY KEY,
    phone_number TEXT NOT NULL REFERENCES borrowers(phone_number),
    loan_amount FLOAT NOT NULL,
    loan_date TEXT NOT NULL,
    loan_admin_fee FLOAT DEFAULT 0
);

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

CREATE TABLE IF NOT EXISTS chat_sessions (
    phone_number TEXT PRIMARY KEY,
    last_message_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status TEXT DEFAULT 'idle',
    last_followup_at DATE DEFAULT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_chat_sessions_followup
ON chat_sessions (last_followup_at);

CREATE TABLE IF NOT EXISTS chat_history (
    id SERIAL PRIMARY KEY,
    phone_number TEXT NOT NULL,
    sender TEXT NOT NULL,
    message_content TEXT NOT NULL,
    image_data TEXT,
    is_processed BOOLEAN DEFAULT FALSE,
    is_auto_reply BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_chat_history_unprocessed
ON chat_history (phone_number, sender, is_processed)
WHERE is_processed = FALSE;

-- ============================================================
-- SCHEDULED TASKS
-- ============================================================

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

CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_due
ON scheduled_tasks (status, scheduled_at)
WHERE status = 'pending';

-- ============================================================
-- MISC TABLES referenced by src/database_pg.py
-- ============================================================

CREATE TABLE IF NOT EXISTS bot_exclusions (
    phone_number TEXT PRIMARY KEY,
    reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS whatsapp_messages (
    id SERIAL PRIMARY KEY,
    phone_number TEXT NOT NULL,
    direction TEXT NOT NULL,
    message_content TEXT,
    raw_payload JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ptp_reminders (
    id SERIAL PRIMARY KEY,
    phone_number TEXT NOT NULL,
    template_name TEXT NOT NULL,
    campaign_id TEXT,
    reminder_date DATE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Note: LangGraph checkpoint tables are created automatically at
-- worker startup via PostgresSaver.setup().
