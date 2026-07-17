-- V011: Rebuild products around typed per-kind payloads + product_snapshots.
--
-- The product-model registry (packages/product-model) now defines, per kind,
-- two JSONB payload families that replace the never-written `details` column
-- and the promoted `credit_limit`:
--   attributes -> slow-changing identity/config (last4, maturity_date, ...);
--                 lives on products, shallow-merged on write
--   metrics    -> per-scrape observation (balance, available, limit, owed,
--                 nav, units, ...); history in product_snapshots, latest
--                 denormalized on products.metrics
-- product_balances becomes product_snapshots (same rows, plus the metrics
-- payload) and products gains a real identity key so the writer can upsert
-- with ON CONFLICT instead of SELECT-then-INSERT.

-- 1. products: typed payload columns ------------------------------------------
ALTER TABLE products
  ADD COLUMN attributes JSONB NOT NULL DEFAULT '{}',
  ADD COLUMN metrics JSONB;

-- Seed revolving metrics from the old columns (current_balance held the
-- available cupo, credit_limit the total). This keeps the *current* net worth
-- (/api/institutions, which reads products.metrics) from losing card / línea
-- debt before the first re-scrape; the wealth *history* series is a different
-- story — pre-V011 snapshot rows intentionally carry empty metrics (see the
-- INSERT below) and the series self-heals as re-scraped observations accumulate.
UPDATE products
SET metrics = jsonb_strip_nulls(
  jsonb_build_object(
    'kind', kind,
    'available', current_balance,
    'limit', credit_limit
  )
)
WHERE kind IN ('credit_card', 'line_of_credit')
  AND current_balance IS NOT NULL;

ALTER TABLE products DROP COLUMN details;
ALTER TABLE products DROP COLUMN credit_limit;

-- 2. product identity: (account, kind, currency, external_ref) -----------------
-- Defensive dedupe before the unique index: suffix the external_ref of any
-- later-created duplicate so the index build can't fail on unseen data.
UPDATE products p
SET external_ref = COALESCE(p.external_ref, '') || 'dup-' || p.id
WHERE EXISTS (
  SELECT 1 FROM products q
  WHERE q.account_id = p.account_id
    AND q.kind = p.kind
    AND q.currency = p.currency
    AND COALESCE(q.external_ref, '') = COALESCE(p.external_ref, '')
    AND (q.created_at, q.id) < (p.created_at, p.id)
);

CREATE UNIQUE INDEX uq_products_identity
  ON products (account_id, kind, currency, COALESCE(external_ref, ''));

-- The only per-kind guard: a payload written under the wrong kind is a bug.
ALTER TABLE products ADD CONSTRAINT products_attributes_kind_check
  CHECK (attributes->>'kind' IS NULL OR attributes->>'kind' = kind);

-- 3. product_balances -> product_snapshots (adds the metrics payload) ----------
CREATE TABLE product_snapshots (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  product_id UUID NOT NULL REFERENCES products(id),
  balance NUMERIC(20, 8) NOT NULL,            -- headline (metrics.headline())
  metrics JSONB NOT NULL DEFAULT '{}',        -- full observation at as_of
  as_of TIMESTAMPTZ NOT NULL DEFAULT now(),
  source TEXT NOT NULL DEFAULT 'scraper',
  UNIQUE (product_id, as_of)
);

-- Old rows carry over (UUIDs preserved) with an empty metrics payload: they
-- predate typed observations, and the wealth API only needs balance/as_of.
INSERT INTO product_snapshots (id, product_id, balance, metrics, as_of, source)
SELECT id, product_id, balance, '{}'::jsonb, as_of, source
FROM product_balances;

DROP TABLE product_balances;

CREATE INDEX idx_product_snapshots_product ON product_snapshots(product_id);
