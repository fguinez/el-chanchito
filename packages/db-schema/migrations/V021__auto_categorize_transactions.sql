-- V021: Categorize new transactions from category_rules as they are inserted
-- (issue #14).
--
-- Matching lives in the database, not in each writer, so every path that
-- inserts a transaction (the scrapers' writer, CSV import, manual entry through
-- the API, any future ingest) gets the same behavior without code of its own.
-- `category_for_description` is the single source of matching truth: the
-- BEFORE INSERT trigger below calls it for new rows, and the dashboard's
-- "Categorizar ahora" backfill (PUT /api/categories) calls it for rows that are
-- still uncategorized.
--
-- A rule matches when its keyword is a case-insensitive LITERAL substring of
-- the description (strpos, not LIKE/ILIKE, so `%` and `_` match themselves).
-- Blank keywords never match. The highest priority wins; ties go to the oldest
-- rule, then the lowest id, so the result is deterministic.
--
-- Rules only fill in missing categories: a row that arrives with a category, or
-- flagged as manually categorized, is left alone. Editing or deleting a rule
-- never undoes a category it already assigned.
--
-- No backfill here: existing rows are categorized from the dashboard. Safe on
-- a fresh database, and idempotent.

CREATE OR REPLACE FUNCTION category_for_description(txn_description TEXT)
RETURNS UUID
LANGUAGE sql
STABLE
AS $$
  SELECT r.category_id
  FROM category_rules r
  WHERE btrim(r.keyword) <> ''
    AND strpos(lower(txn_description), lower(r.keyword)) > 0
  ORDER BY r.priority DESC, r.created_at ASC, r.id ASC
  LIMIT 1
$$;

COMMENT ON FUNCTION category_for_description(TEXT) IS
    'Category of the highest-priority category_rules keyword found in the '
    'description (case-insensitive literal substring; ties: oldest rule, then '
    'lowest id). NULL when no rule matches.';

CREATE OR REPLACE FUNCTION categorize_new_transaction()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.category_id IS NULL AND NOT NEW.is_manually_categorized THEN
    NEW.category_id := category_for_description(NEW.description);
  END IF;
  RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER trg_transactions_categorize
  BEFORE INSERT ON transactions
  FOR EACH ROW
  EXECUTE FUNCTION categorize_new_transaction();
