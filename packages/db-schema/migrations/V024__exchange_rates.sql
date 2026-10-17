-- V024: Exchange rates that institutions apply, read by the scrapers.
--
-- Institutions convert with their own rates, sometimes a different one per
-- product: Banco de Chile bills its USD credit card in pesos at the "Tipo de
-- cambio" its international card payment page prints, which is neither the
-- market reference lib/rates fetches nor the observed dollar. A row here is the
-- latest such rate a scraper read, so the dashboard can value that product the
-- way the institution does instead of at the provider's reference.
--
-- Scope: `product_id` NULL means the rate applies to every product of the
-- institution in `base_currency`; a product-scoped row also carries its
-- product's `institution_id`. When the dashboard converts an amount of a
-- product it prefers the product row, then the institution row, then the
-- provider rate (apps/web/src/lib/rates.ts). One row per scope and currency
-- pair: a re-scrape updates it in place, so this holds the current value
-- only, not a history.
--
-- `rate` is quote units per 1 base unit (CLP per USD). `observed_at` is the
-- last scrape that read it, which is what the staleness warning compares with
-- today; `reported_as_of` is the validity date the institution prints, when
-- it prints one. `source` is the Spanish label of where it was read, shown in
-- the UI.
--
-- Written only by the scrapers' `db.writer.upsert_exchange_rate`, which never
-- creates an institution or a product to hang a rate on. Safe on a fresh
-- database, and idempotent.

CREATE TABLE IF NOT EXISTS exchange_rates (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  institution_id UUID NOT NULL REFERENCES institutions(id) ON DELETE CASCADE,
  product_id UUID REFERENCES products(id) ON DELETE CASCADE,  -- NULL = institution-wide
  base_currency TEXT NOT NULL,
  quote_currency TEXT NOT NULL DEFAULT 'CLP',
  rate NUMERIC(20, 8) NOT NULL CHECK (rate > 0),              -- quote units per 1 base
  source TEXT NOT NULL,                                       -- Spanish UI label of where it was read
  reported_as_of DATE,                                        -- validity date printed by the institution
  observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),             -- last scrape that read this value
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (base_currency <> quote_currency)
);

-- The nil UUID stands in for "institution-wide" so the two scopes share one
-- index; the writer's ON CONFLICT names this exact expression.
CREATE UNIQUE INDEX IF NOT EXISTS exchange_rates_scope_pair_idx
  ON exchange_rates (
    institution_id,
    COALESCE(product_id, '00000000-0000-0000-0000-000000000000'::uuid),
    base_currency,
    quote_currency
  );
