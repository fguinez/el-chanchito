-- V041: Category rules can match a whole description, not only a substring.
--
-- `match_type` 'contains' (the default, and every rule before this migration)
-- keeps the V021 semantics: the keyword is a case-insensitive literal
-- substring of the description. 'exact' matches only when the trimmed
-- description equals the trimmed keyword, ignoring case. The transaction
-- detail panel creates exact rules ("aplicar a todas las transacciones con
-- esta descripción"), so a rule made from a short description such as a
-- single merchant word does not swallow every longer description containing
-- it.
--
-- An exact match wins over any contains match; within each group the highest
-- priority wins, then the oldest rule, then the lowest id. At most one exact
-- rule per keyword (trimmed, case-insensitive), so the panel's reuse and the
-- trigger always agree on which one applies. Idempotent.

ALTER TABLE category_rules
  ADD COLUMN IF NOT EXISTS match_type TEXT NOT NULL DEFAULT 'contains';

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'category_rules_match_type_check'
  ) THEN
    ALTER TABLE category_rules
      ADD CONSTRAINT category_rules_match_type_check
      CHECK (match_type IN ('contains', 'exact'));
  END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_category_rules_exact_keyword
  ON category_rules (lower(btrim(keyword)))
  WHERE match_type = 'exact';

CREATE OR REPLACE FUNCTION category_for_description(txn_description TEXT)
RETURNS UUID
LANGUAGE sql
STABLE
AS $$
  SELECT r.category_id
  FROM category_rules r
  WHERE btrim(r.keyword) <> ''
    AND CASE r.match_type
      WHEN 'exact' THEN lower(btrim(txn_description)) = lower(btrim(r.keyword))
      ELSE strpos(lower(txn_description), lower(r.keyword)) > 0
    END
  ORDER BY (r.match_type = 'exact') DESC, r.priority DESC, r.created_at ASC,
    r.id ASC
  LIMIT 1
$$;

COMMENT ON FUNCTION category_for_description(TEXT) IS
    'Category of the best category_rules match for the description: exact '
    'rules (trimmed, case-insensitive equality) before contains rules '
    '(case-insensitive literal substring); then highest priority, oldest rule, '
    'lowest id. NULL when no rule matches.';
