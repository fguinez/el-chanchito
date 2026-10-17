-- V035: Reusable description aliases, resolved at READ time (issue #103).
--
-- An alias relabels a transaction keyed on its scraped `description`. Unlike
-- category rules, which materialize their decision onto the row at insert time
-- (V021), aliases are NOT stored on the transaction: `alias_for_transaction`
-- runs when a list is read, so editing or deleting an alias updates every list
-- immediately, with no backfill and no "apply" step. The scraped `description`
-- is never overwritten; the alias only changes what is shown.
--
-- Matching is case-insensitive:
--   exact     = trimmed whole-string equality (btrim + lower on both sides).
--   substring = literal `strpos` (so `%` and `_` match themselves, unlike LIKE).
-- Ties are broken by priority desc, then the oldest alias, then its id, so the
-- order is deterministic.
--
-- The function already takes `txn_amount` and `txn_currency` so Phase 3 can be
-- UI-only. They are UNUSED here: Phase 2 matches on the description alone.
-- Phase 3 (issue #103) adds amount/currency columns and filtering, and orders
-- conditional (amount/currency-bound) aliases ahead of unconditional ones.

CREATE TABLE description_aliases (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  match_description TEXT NOT NULL,      -- scraped description to match (case-insensitive)
  match_type        TEXT NOT NULL DEFAULT 'exact'
                      CHECK (match_type IN ('exact', 'substring')),
  alias             TEXT NOT NULL,      -- label shown in place of the scraped text
  priority          INTEGER NOT NULL DEFAULT 0, -- higher = checked first
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_description_aliases_priority ON description_aliases(priority DESC);

CREATE OR REPLACE FUNCTION alias_for_transaction(
  txn_description TEXT,
  txn_amount NUMERIC,
  txn_currency TEXT
)
RETURNS TEXT
LANGUAGE sql
STABLE
AS $$
  SELECT a.alias
  FROM description_aliases a
  WHERE btrim(a.match_description) <> ''
    AND (
      (a.match_type = 'exact'
        AND btrim(lower(a.match_description)) = btrim(lower(txn_description)))
      OR (a.match_type = 'substring'
        AND strpos(lower(txn_description), lower(a.match_description)) > 0)
    )
  ORDER BY a.priority DESC, a.created_at ASC, a.id ASC
  LIMIT 1
$$;

COMMENT ON FUNCTION alias_for_transaction(TEXT, NUMERIC, TEXT) IS
    'Alias of the highest-priority description_aliases row matching the '
    'description (case-insensitive: exact = trimmed whole-string equality, '
    'substring = literal strpos; ties: oldest alias, then lowest id). NULL when '
    'none matches. Resolved at read time, so an edit shows up everywhere with '
    'no backfill. txn_amount/txn_currency are reserved for Phase 3 and ignored.';
