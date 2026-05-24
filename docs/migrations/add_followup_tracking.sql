-- Migration: Add last_followup_at to chat_sessions
-- Purpose: Prevent duplicate 9 AM follow-up messages on worker restart
-- Date: 2025-12-30

-- Add last_followup_at column to track when follow-up was sent to each borrower
ALTER TABLE chat_sessions 
ADD COLUMN IF NOT EXISTS last_followup_at DATE DEFAULT NULL;

-- Index for efficient filtering during morning follow-up query
CREATE INDEX IF NOT EXISTS idx_chat_sessions_followup 
ON chat_sessions (last_followup_at);

-- Verify the column was added
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_name = 'chat_sessions'
ORDER BY ordinal_position;
