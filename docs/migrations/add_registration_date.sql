-- Migration: Add registration_date column to borrowers table
-- Purpose: Track when each borrower registered for the product
-- Date: 2026-01-01

-- Add registration_date column to borrowers table
-- Format: YYYY-MM-DD (e.g., 2025-09-27)
ALTER TABLE borrowers ADD COLUMN IF NOT EXISTS registration_date TEXT;

-- Verify the column was added
SELECT column_name, data_type 
FROM information_schema.columns 
WHERE table_name = 'borrowers' AND column_name = 'registration_date';
