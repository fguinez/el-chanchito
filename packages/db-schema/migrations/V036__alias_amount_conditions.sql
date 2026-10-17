-- V036: Amount-range conditions for description aliases, scoped by currency
-- (issue #103, Phase 3).
--
-- Phase 2 (V035) matched an alias on the scraped description alone. Phase 3 adds
-- an optional, inclusive amount condition on the SIGNED amount, scoped to one
-- currency:
--
--   * An amount condition is an inclusive range [amount_min, amount_max] on the
--     SIGNED amount. An exact value Y is amount_min = amount_max = Y; a range
--     W..Z is amount_min = W, amount_max = Z; an open end leaves that bound NULL
--     (amount_min only means ">= min", amount_max only means "<= max").
--   * Bounds are currency-specific: a bound only ever applies to rows in its own
--     currency, so a CLP bound never matches a USD row. An amount-bounded alias
--     must therefore carry a currency (enforced by chk_..._amount_currency).
--   * "amount is Y OR between W and Z" is NOT a boolean DSL: model it as TWO
--     alias rows sharing the same `alias` text, one per disjunct.
--   * Amount- or currency-bound ("conditional") aliases are ordered AHEAD of
--     unconditional ones, so a specific amount/currency rule wins over a
--     catch-all on the same description even when the catch-all has a higher
--     priority. A currency-only alias (no bounds) counts as conditional.
--
-- Safe on a fresh database once V035 has run, and idempotent.

ALTER TABLE description_aliases
  ADD COLUMN IF NOT EXISTS amount_min NUMERIC(18, 2),
  ADD COLUMN IF NOT EXISTS amount_max NUMERIC(18, 2),
  ADD COLUMN IF NOT EXISTS currency   TEXT;

ALTER TABLE description_aliases
  DROP CONSTRAINT IF EXISTS chk_description_aliases_amount_order,
  DROP CONSTRAINT IF EXISTS chk_description_aliases_amount_currency;

ALTER TABLE description_aliases
  ADD CONSTRAINT chk_description_aliases_amount_order
    CHECK (amount_min IS NULL OR amount_max IS NULL OR amount_min <= amount_max),
  ADD CONSTRAINT chk_description_aliases_amount_currency
    CHECK ((amount_min IS NULL AND amount_max IS NULL) OR currency IS NOT NULL);

COMMENT ON COLUMN description_aliases.amount_min IS
    'Inclusive lower bound on the SIGNED transaction amount (NULL = no lower '
    'bound). Applies only to rows whose currency equals `currency`.';
COMMENT ON COLUMN description_aliases.amount_max IS
    'Inclusive upper bound on the SIGNED transaction amount (NULL = no upper '
    'bound). For an exact value Y, amount_min = amount_max = Y.';
COMMENT ON COLUMN description_aliases.currency IS
    'Currency that scopes the amount bounds (products.currency, e.g. CLP/USD). '
    'Required whenever a bound is set, so a CLP bound never matches a USD row.';

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
    AND (a.amount_min IS NULL OR txn_amount >= a.amount_min)
    AND (a.amount_max IS NULL OR txn_amount <= a.amount_max)
    AND (a.currency IS NULL OR a.currency = txn_currency)
  ORDER BY
    (a.amount_min IS NOT NULL OR a.amount_max IS NOT NULL OR a.currency IS NOT NULL) DESC,
    a.priority DESC,
    a.created_at ASC,
    a.id ASC
  LIMIT 1
$$;

COMMENT ON FUNCTION alias_for_transaction(TEXT, NUMERIC, TEXT) IS
    'Alias of the highest-priority description_aliases row matching the '
    'description (case-insensitive: exact = trimmed whole-string equality, '
    'substring = literal strpos) AND the optional amount condition: '
    'txn_amount within [amount_min, amount_max] (inclusive, either bound NULL '
    'for open) and currency = txn_currency (when a currency is set). '
    'Amount- or currency-bound ("conditional") aliases are ordered AHEAD of '
    'unconditional ones; then priority desc, oldest alias, then lowest id. NULL when none '
    'matches. Resolved at read time, so an edit shows up everywhere with no '
    'backfill.';
