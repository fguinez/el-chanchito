-- V034: Layer a one-time custom description over the scraped one.
--
-- A scraped transaction has a single `description`: the text the bank sent.
-- #101 let the detail panel edit it, which overwrites the only copy of that
-- original. This adds `custom_description`, a per-row override the user owns, so
-- the scraped text stays intact: for a scraped row (one with an `external_id`)
-- `description` keeps the immutable bank original and the display is
-- `COALESCE(custom_description, description)`. A manual row (no `external_id`)
-- keeps editing `description` directly, so it leaves `custom_description` NULL.
--
-- The override is for DISPLAY only: category rules and (later) alias matching
-- still key on the raw scraped `description`.
--
-- Safe on a fresh database, and idempotent.

ALTER TABLE transactions
    ADD COLUMN IF NOT EXISTS custom_description TEXT;

COMMENT ON COLUMN transactions.custom_description IS
    'One-time per-row description override the user set. For a scraped row '
    '(one with an external_id) `description` stays the immutable scraped '
    'original and the display is COALESCE(custom_description, description); a '
    'manual row (no external_id) edits description directly and leaves this '
    'NULL. Display only: matching still keys on the raw `description`.';
