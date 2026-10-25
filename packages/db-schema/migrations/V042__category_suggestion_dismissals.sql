-- V042: Remember the category suggestions the user turned down.
--
-- Uncategorized transactions get a suggested category computed on demand from
-- the already categorized ones (lib/category-suggestions). Nothing is applied
-- on its own; this table keeps only the "Ignorar" answers of the Categorías
-- page, so a dismissed suggestion does not come back on the next load.
--
-- A dismissal is keyed by the normalised description (lowercase, no accents,
-- no tokens holding digits, punctuation collapsed; see normalizeDescription)
-- and the suggested category. The suggestion reappears only when it changes
-- to another category. Deleting the category deletes its dismissals.
--
-- New table only, no backfill. Safe on a fresh database, and idempotent.

CREATE TABLE IF NOT EXISTS dismissed_category_suggestions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  normalized_description TEXT NOT NULL,
  category_id UUID NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
  dismissed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_dismissed_category_suggestions
    UNIQUE (normalized_description, category_id)
);

-- The unique constraint's index serves lookups by description; this one
-- serves the cascade when a category is deleted.
CREATE INDEX IF NOT EXISTS idx_dismissed_category_suggestions_category
  ON dismissed_category_suggestions (category_id);

COMMENT ON TABLE dismissed_category_suggestions IS
    'Category suggestions the user dismissed: normalised description and the '
    'suggested category. A different suggested category shows again.';
