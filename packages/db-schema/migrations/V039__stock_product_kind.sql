-- V039: Re-kind Fintual stock positions from `investment` to `stock`.
--
-- The product-model registry now has a `stock` kind (family "Acciones") with
-- its own payloads: ticker/description/tags moved to StockAttributes and the
-- per-unit figures (units, unit_price, average_cost, ...) to StockMetrics.
-- The Fintual scraper emits every stock position (external_ref
-- "stock:<asset id>", USD) as `stock`; its goals stay `investment`.
--
-- Data only, apart from two constraints: products_kind_check (V009) widens to
-- admit the new kind, and products_stock_ref_kind_check keeps a stock ref off
-- `investment` from now on. Every payload the writer stores carries its kind
-- as a "kind" key (products.attributes, products.metrics,
-- product_snapshots.metrics), and products_attributes_kind_check (V011)
-- requires attributes->>'kind' to match the column, so the column and the
-- payloads move in the same UPDATE. The snapshots follow, so a stock's history
-- parses with the new discriminator. A payload without a "kind" key (NULL
-- metrics, a legacy '{}' snapshot) is left as it is.
-- No other table stores a product kind: transactions, prices, rates and
-- monitor refs point at products by id.
--
-- The identity index (account_id, kind, currency, external_ref) cannot clash:
-- no `stock` row existed before this migration. Scoped by the "stock:" ref
-- prefix (only Fintual's scraper writes it), the same predicate the guard
-- checks, so the guard can always be added; safe on a fresh database, and
-- idempotent.
--
-- ROLLOUT: stop the scrapers, run this migration, then restart the scrapers
-- and the web on the new code. A scraper still on the old code would write
-- its positions as (`investment`, "stock:<asset id>"), which the identity
-- index sees as new products: products_stock_ref_kind_check rejects them, so
-- each lands in the run's failed products instead of a duplicate. The old
-- web build cannot render the new kind either (KIND_INFO has no "stock"
-- entry there, and the institutions page throws on it).

ALTER TABLE products DROP CONSTRAINT IF EXISTS products_kind_check;
ALTER TABLE products ADD CONSTRAINT products_kind_check CHECK (kind IN (
  'checking', 'savings', 'vista', 'wallet', 'term_deposit',
  'credit_card', 'debit_card', 'prepaid_card',
  'line_of_credit', 'loan', 'mortgage',
  'investment', 'stock', 'crypto', 'other'
));

UPDATE products p
SET kind = 'stock',
    attributes = CASE
      WHEN p.attributes ? 'kind'
        THEN jsonb_set(p.attributes, '{kind}', '"stock"')
      ELSE p.attributes
    END,
    metrics = CASE
      WHEN p.metrics ? 'kind'
        THEN jsonb_set(p.metrics, '{kind}', '"stock"')
      ELSE p.metrics
    END,
    updated_at = now()
WHERE p.kind = 'investment'
  AND p.external_ref LIKE 'stock:%';

UPDATE product_snapshots s
SET metrics = jsonb_set(s.metrics, '{kind}', '"stock"')
FROM products p
WHERE s.product_id = p.id
  AND p.kind = 'stock'
  AND p.external_ref LIKE 'stock:%'
  AND s.metrics->>'kind' = 'investment';

-- A stock ref is never an `investment` again.
ALTER TABLE products DROP CONSTRAINT IF EXISTS products_stock_ref_kind_check;
ALTER TABLE products ADD CONSTRAINT products_stock_ref_kind_check
  CHECK (NOT (kind = 'investment' AND external_ref LIKE 'stock:%'));
