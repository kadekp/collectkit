-- Migration: Add bot_exclusions table
-- Purpose: Phone numbers the bot should NOT handle (harsh complaints, employees, etc.)
-- Date: 2026-02-01

CREATE TABLE IF NOT EXISTS bot_exclusions (
    phone_number TEXT PRIMARY KEY,
    reason TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Verify
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'bot_exclusions';
