-- Migration: Add label column to borrowers table
-- Purpose: Add analytics label for borrower classification (e.g., "TEST" users)
-- Date: 2025-12-29

-- Add label column to borrowers table (nullable for backward compatibility)
ALTER TABLE borrowers ADD COLUMN IF NOT EXISTS label TEXT DEFAULT NULL;

-- Create index on label for efficient analytics queries
CREATE INDEX IF NOT EXISTS idx_borrowers_label ON borrowers(label);

-- Verify the column was added
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'borrowers' AND column_name = 'label';
