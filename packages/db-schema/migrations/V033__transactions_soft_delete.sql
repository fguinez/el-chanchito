-- V033: Soft-delete imported transactions so a scrape cannot bring them back.
--
-- A hard delete throws away the only thing that keeps a movement from being
-- imported again: `upsert_transactions` looks each movement up by
-- (product_id, external_id) and inserts with ON CONFLICT DO NOTHING, so once
-- the row is gone the next scrape (or a re-import of the same CSV) finds
-- nothing under that key and inserts it again.
--
-- So a row with an `external_id` (scraper or CSV import) is soft-deleted: it
-- gets a `deleted_at` and stays, still holding its key. The writer's lookups
-- keep matching it and skip the movement with no writer change at all, and
-- BanChile's adoption path (issue #57) can still re-key a deleted legacy row
-- onto the bank's operation id. A tombstone table of deleted keys would miss
-- that case: the new key matches no tombstone and the movement comes back.
-- A row without an `external_id` (manual entry) is hard-deleted instead, since
-- nothing can re-import it.
--
-- Every dashboard read filters `deleted_at IS NULL`; the writer's lookups
-- deliberately do not. Restoring a deleted row is not offered yet.
--
-- Safe on a fresh database, and idempotent.

ALTER TABLE transactions
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

COMMENT ON COLUMN transactions.deleted_at IS
    'When the user deleted this imported transaction; NULL while visible. '
    'The row keeps its (product_id, external_id) key so scrapers and CSV '
    're-imports skip it instead of importing it again.';
