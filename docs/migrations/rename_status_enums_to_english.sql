-- Migration: Rename borrowers.status enum values to English
-- Purpose: Generalize the public template; old values were Indonesian.
-- Date: 2026-05-24
--
-- Mapping:
--   AKAN_JATUH_TEMPO -> UPCOMING
--   TERLAMBAT        -> OVERDUE
--   TERBAYAR         -> PAID
--
-- Idempotent: re-running is a no-op once rows are migrated.

UPDATE borrowers SET status = 'UPCOMING' WHERE status = 'AKAN_JATUH_TEMPO';
UPDATE borrowers SET status = 'OVERDUE'  WHERE status = 'TERLAMBAT';
UPDATE borrowers SET status = 'PAID'     WHERE status = 'TERBAYAR';

-- Verify
SELECT status, COUNT(*) FROM borrowers GROUP BY status ORDER BY status;
