-- V015: User-defined monitors (issue #41).
--
-- A monitor is one stored equation: a left arithmetic expression over product
-- values, compared against one or more threshold expressions
-- ({severity, comparator, expression}; `alert` required, `warning` optional).
-- Nothing is precomputed: the API evaluates on read against current products
-- and replays history from product_snapshots. Expressions are persisted in
-- uuid-ref form (@{product_uuid:field}) so product renames never break them;
-- the API round-trips them to the display form (institution:product:field).
--   currency -> monitor currency; currency-denominated refs convert into it
--   display  -> chart type (line/stat) + whether the margin is highlighted
-- No extra indexes: the table is tiny (personal dashboard) and always read
-- whole.

CREATE TABLE monitors (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL,
  description TEXT,
  currency TEXT NOT NULL DEFAULT 'CLP',
  expression TEXT NOT NULL,
  thresholds JSONB NOT NULL,
  display JSONB NOT NULL DEFAULT '{"chart": "line", "show_margin": true}',
  is_active BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
