-- Migration: Add loan_admin_fee column to loans table
-- Purpose: Store per-loan admin fees (tiered, not flat)
-- Date: 2026-01-28

-- Add loan_admin_fee column to loans table
ALTER TABLE loans ADD COLUMN IF NOT EXISTS loan_admin_fee FLOAT DEFAULT 0;

-- (No backfill in the public template. If you ran an older schema with a
-- flat admin fee, add your own UPDATE here for your historical value.)

-- Verify the column was added
SELECT column_name, data_type, column_default
FROM information_schema.columns
WHERE table_name = 'loans' AND column_name = 'loan_admin_fee';
