-- V045: Tell a scraper's soft delete apart from the user's.
--
-- Since V033 a deleted imported row keeps its key and a `deleted_at`, and the
-- writer's lookups deliberately still match it, so a movement the user deleted
-- is never imported again. The scrapers now soft-delete too: BanChile's
-- vanished-row cleanup removes pending card rows the bank stopped listing (a
-- stale authorization, a charge re-dated or re-described under a new key).
-- Unlike the user's, that delete is a guess the bank can contradict later, so
-- the writer must be able to tell the two apart:
--
--   deleted_by   'scraper' when a scraper's cleanup set `deleted_at`;
--                NULL for a visible row and for a row the user deleted
--                (every delete before this migration was the user's).
--
-- A scraper-deleted row is never adopted onto another movement's key (it
-- could hide a real charge), and is restored (both columns cleared) when a
-- scrape lists its key again. A user-deleted row stays deleted either way.
-- Scraper logic only: every dashboard read keeps filtering `deleted_at IS
-- NULL`, and the dashboard's own delete only ever touches visible rows.
--
-- No backfill. Safe on a fresh database, and idempotent.

ALTER TABLE transactions
    ADD COLUMN IF NOT EXISTS deleted_by TEXT;

ALTER TABLE transactions
    DROP CONSTRAINT IF EXISTS chk_transactions_deleted_by;

ALTER TABLE transactions
    ADD CONSTRAINT chk_transactions_deleted_by
        CHECK (deleted_by IS NULL OR (deleted_by = 'scraper' AND deleted_at IS NOT NULL));

COMMENT ON COLUMN transactions.deleted_by IS
    'scraper when a scraper cleanup soft-deleted the row (restorable by a later scrape); NULL when visible or deleted by the user.';
