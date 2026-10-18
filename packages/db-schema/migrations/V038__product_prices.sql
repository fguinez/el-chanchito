-- V038: Daily closing prices of a product's unit, read by the scrapers.
--
-- A stock position's balance history (product_snapshots) only moves when the
-- scraper runs, and mixes the price with the shares held. This keeps the
-- market's own series: one closing price per product and trading day, so the
-- product page can chart the price itself. Fintual's scraper fills it for
-- every stock position still held, with the last year of daily closes on each
-- run.
--
-- `price_date` is the trading day. Fintual stamps a daily bar at New York
-- midnight (`T04:00:00Z` under US daylight time, `T05:00:00Z` under standard
-- time), so its UTC calendar date is the day the bar closed. This is the
-- exception to the repo's rule of taking a timestamp's date in
-- America/Santiago (Chilean movements): converting the bar to Santiago time
-- would move it to the day before. `price` is per unit in `currency`, the
-- product's own currency when written.
--
-- One row per product and day: a re-scrape updates a changed price in place
-- (and leaves an unchanged one alone, `updated_at` included) and never prunes,
-- so the series grows as long as the product is held.
--
-- Written only by the scrapers' `db.writer.upsert_price_history`, which never
-- creates an institution or a product to hang a price on. Safe on a fresh
-- database, and idempotent.

CREATE TABLE IF NOT EXISTS product_prices (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  product_id UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  price_date DATE NOT NULL,                       -- trading day (UTC date of the bar)
  price NUMERIC(20, 8) NOT NULL CHECK (price > 0),  -- closing price per unit
  currency TEXT NOT NULL,                         -- the product's currency when written
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_product_prices_product_date UNIQUE (product_id, price_date)
);
