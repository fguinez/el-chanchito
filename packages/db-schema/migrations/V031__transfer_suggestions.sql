-- V031: Remember what the transfer suggestions learned (issue #95).
--
-- The dashboard suggests scraped rows that look like internal transfers (a
-- debit and a credit of the same amount on two products a few days apart, or
-- a debit naming a product without a transaction feed). Suggestions are
-- computed on demand from transactions; these two tables keep only the
-- user's answers to them.
--
--   dismissed_transfer_suggestions
--     A suggestion the user turned down, so it does not come back on the next
--     load. source_transaction_id is the outgoing row (amount < 0);
--     target_transaction_id the incoming row of a pair, or NULL for a
--     one-sided suggestion (the target product has no row to link). One row
--     per (source, target), the NULL target included (NULLS NOT DISTINCT).
--     Deleting either transaction deletes the dismissal.
--
--   confirmed_transfer_patterns
--     A confirmed suggestion's shape: the source and target products and the
--     normalised prefix of the source row's description (lowercase, no
--     accents, digits or punctuation; see lib/transfer-suggestions). Later
--     rows matching a pattern rank first. Deleting a row removes the boost;
--     deleting either product deletes its patterns.
--
-- New tables only, no backfill. Safe on a fresh database, and idempotent.

CREATE TABLE IF NOT EXISTS dismissed_transfer_suggestions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source_transaction_id UUID NOT NULL
    REFERENCES transactions(id) ON DELETE CASCADE,
  target_transaction_id UUID
    REFERENCES transactions(id) ON DELETE CASCADE,
  dismissed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_dismissed_transfer_suggestions_pair
    UNIQUE NULLS NOT DISTINCT (source_transaction_id, target_transaction_id)
);

-- The unique constraint's index serves lookups by source; this one serves
-- the cascade when a target row is deleted.
CREATE INDEX IF NOT EXISTS idx_dismissed_transfer_suggestions_target
  ON dismissed_transfer_suggestions (target_transaction_id);

CREATE TABLE IF NOT EXISTS confirmed_transfer_patterns (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source_product_id UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  target_product_id UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  description_prefix TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_confirmed_transfer_patterns
    UNIQUE (source_product_id, target_product_id, description_prefix)
);

CREATE INDEX IF NOT EXISTS idx_confirmed_transfer_patterns_target
  ON confirmed_transfer_patterns (target_product_id);

COMMENT ON TABLE dismissed_transfer_suggestions IS
    'Transfer suggestions the user dismissed; target NULL = a one-sided '
    'suggestion for that source row.';
COMMENT ON TABLE confirmed_transfer_patterns IS
    'Source product, target product and normalised description prefix of '
    'confirmed transfer suggestions; matching rows rank first.';
