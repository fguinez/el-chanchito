-- V009: Restructure into users / institutions / accounts / products.
--
-- The old `accounts` table actually held products ("banchile - checking");
-- the platform relationship lived in a free-text `institution` column.
-- New model:
--   users         -> who owns things (schema.org/Person)
--   institutions  -> the platform: bank/fintech/exchange (schema.org/FinancialService)
--   accounts      -> a user's enrollment at one institution
--   products      -> the money-holding elements (schema.org/FinancialProduct)
--   product_balances -> balance history (a row per value change, not latest-only)
-- All existing UUIDs are preserved: old accounts rows become products rows,
-- so every transactions/internal_transfers FK survives the rename.

-- 1. users -------------------------------------------------------------------
CREATE TABLE users (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL,
  email TEXT UNIQUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO users (id, name)
VALUES ('00000000-0000-0000-0000-000000000001', 'Demo User');

-- 2. institutions ------------------------------------------------------------
CREATE TABLE institutions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  slug TEXT NOT NULL UNIQUE,          -- stable key the scrapers resolve by
  name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'other'
    CHECK (kind IN ('bank', 'fintech', 'exchange', 'asset_manager', 'other')),
  country TEXT,
  url TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO institutions (slug, name, kind, country, url) VALUES
  ('banchile',    'Banco de Chile', 'bank',          'CL', 'https://portales.bancochile.cl'),
  ('bci',         'Bci',            'bank',          'CL', 'https://www.bci.cl'),
  ('bci_lider',   'Bci / Líder',    'bank',          'CL', 'https://www.bci.cl'),
  ('mercadopago', 'Mercado Pago',   'fintech',       'CL', 'https://www.mercadopago.cl'),
  ('mach',        'MACH',           'fintech',       'CL', 'https://somosmach.com'),
  ('tenpo',       'Tenpo',          'fintech',       'CL', 'https://www.tenpo.cl'),
  ('fintual',     'Fintual',        'asset_manager', 'CL', 'https://fintual.cl'),
  ('buda',        'Buda',           'exchange',      'CL', 'https://www.buda.com'),
  ('manual',      'Manual',         'other',         NULL, NULL);

-- Anything present in data but not seeded above (e.g. csv_import) lands as 'other'
INSERT INTO institutions (slug, name, kind)
SELECT DISTINCT a.institution, a.institution, 'other'
FROM accounts a
WHERE a.institution NOT IN (SELECT slug FROM institutions);

-- 3. old accounts become products ---------------------------------------------
ALTER TABLE accounts RENAME TO products;
ALTER TABLE products RENAME COLUMN account_type TO kind;
UPDATE products SET kind = 'wallet' WHERE kind = 'prepaid';

-- 4. new accounts: one enrollment per (user, institution) found in data --------
CREATE TABLE accounts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id),
  institution_id UUID NOT NULL REFERENCES institutions(id),
  name TEXT NOT NULL DEFAULT 'Personal',
  is_active BOOLEAN NOT NULL DEFAULT true,
  display_order INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, institution_id, name)
);

CREATE INDEX idx_accounts_user ON accounts(user_id);
CREATE INDEX idx_accounts_institution ON accounts(institution_id);

INSERT INTO accounts (user_id, institution_id, name)
SELECT DISTINCT
  '00000000-0000-0000-0000-000000000001'::uuid,
  i.id,
  'Personal'
FROM products p
JOIN institutions i ON i.slug = p.institution;

ALTER TABLE products ADD COLUMN account_id UUID REFERENCES accounts(id);

UPDATE products p
SET account_id = a.id
FROM institutions i
JOIN accounts a ON a.institution_id = i.id
WHERE i.slug = p.institution;

ALTER TABLE products ALTER COLUMN account_id SET NOT NULL;
ALTER TABLE products DROP COLUMN institution;

ALTER TABLE products
  ADD COLUMN parent_product_id UUID REFERENCES products(id),  -- debit card -> checking, línea -> cta. cte.
  ADD COLUMN external_ref TEXT,                               -- masked number / goal id at the institution
  ADD COLUMN credit_limit BIGINT,                             -- credit_card / line_of_credit (cupo)
  ADD COLUMN details JSONB NOT NULL DEFAULT '{}'::jsonb,      -- kind-specific attributes
  ADD COLUMN current_balance NUMERIC(20, 8),                  -- denormalized latest; history in product_balances
  ADD COLUMN balance_as_of TIMESTAMPTZ;                       -- last time a scraper confirmed the balance

ALTER TABLE products ADD CONSTRAINT products_kind_check CHECK (kind IN (
  'checking', 'savings', 'vista', 'wallet', 'term_deposit',
  'credit_card', 'debit_card', 'prepaid_card',
  'line_of_credit', 'loan', 'mortgage',
  'investment', 'crypto', 'other'
));

CREATE INDEX idx_products_account ON products(account_id);

-- 5. account_balances -> product_balances (history, not latest-only) ----------
ALTER TABLE account_balances RENAME TO product_balances;
ALTER TABLE product_balances RENAME COLUMN account_id TO product_id;
ALTER TABLE product_balances DROP CONSTRAINT account_balances_account_id_key;
ALTER TABLE product_balances ALTER COLUMN balance TYPE NUMERIC(20, 8);
ALTER TABLE product_balances
  ADD CONSTRAINT product_balances_product_id_as_of_key UNIQUE (product_id, as_of);
ALTER INDEX idx_account_balances_account RENAME TO idx_product_balances_product;

-- Denormalize the current latest into products
UPDATE products p
SET current_balance = b.balance, balance_as_of = b.as_of
FROM product_balances b
WHERE b.product_id = p.id;

-- 6. FK column renames (constraints follow the columns automatically) ----------
ALTER TABLE transactions RENAME COLUMN account_id TO product_id;
ALTER INDEX idx_transactions_account_date RENAME TO idx_transactions_product_date;
ALTER TABLE internal_transfers RENAME COLUMN from_account_id TO from_product_id;
ALTER TABLE internal_transfers RENAME COLUMN to_account_id TO to_product_id;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'transactions_account_id_external_id_key') THEN
    ALTER TABLE transactions
      RENAME CONSTRAINT transactions_account_id_external_id_key
      TO transactions_product_id_external_id_key;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'transactions_account_id_fkey') THEN
    ALTER TABLE transactions
      RENAME CONSTRAINT transactions_account_id_fkey TO transactions_product_id_fkey;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'account_balances_account_id_fkey') THEN
    ALTER TABLE product_balances
      RENAME CONSTRAINT account_balances_account_id_fkey TO product_balances_product_id_fkey;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'internal_transfers_from_account_id_fkey') THEN
    ALTER TABLE internal_transfers
      RENAME CONSTRAINT internal_transfers_from_account_id_fkey TO internal_transfers_from_product_id_fkey;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'internal_transfers_to_account_id_fkey') THEN
    ALTER TABLE internal_transfers
      RENAME CONSTRAINT internal_transfers_to_account_id_fkey TO internal_transfers_to_product_id_fkey;
  END IF;
END $$;

-- 7. Backfill wealth history from decomposable wealth_snapshots columns --------
-- (wealth_snapshots stays as legacy totals for pre-migration dates + manual entries;
--  the wealth API now derives the series from product_balances.)

-- Banco de Chile savings never existed as a product; create it if snapshots used it
INSERT INTO products (account_id, name, kind, currency)
SELECT a.id, 'Cuenta de Ahorro Banco de Chile', 'savings', 'CLP'
FROM accounts a
JOIN institutions i ON a.institution_id = i.id
WHERE i.slug = 'banchile'
  AND EXISTS (SELECT 1 FROM wealth_snapshots WHERE banchile_savings IS NOT NULL)
  AND NOT EXISTS (
    SELECT 1 FROM products p WHERE p.account_id = a.id AND p.kind = 'savings'
  );

INSERT INTO product_balances (product_id, balance, as_of, source)
SELECT p.id, w.fintual_balance, w.snapshot_date::timestamptz, 'wealth_snapshot'
FROM wealth_snapshots w
CROSS JOIN LATERAL (
  SELECT p.id FROM products p
  JOIN accounts a ON p.account_id = a.id
  JOIN institutions i ON a.institution_id = i.id
  WHERE i.slug = 'fintual' AND p.kind = 'investment'
  ORDER BY p.created_at LIMIT 1
) p
WHERE w.fintual_balance IS NOT NULL
ON CONFLICT (product_id, as_of) DO NOTHING;

INSERT INTO product_balances (product_id, balance, as_of, source)
SELECT p.id, w.mercadopago_balance, w.snapshot_date::timestamptz, 'wealth_snapshot'
FROM wealth_snapshots w
CROSS JOIN LATERAL (
  SELECT p.id FROM products p
  JOIN accounts a ON p.account_id = a.id
  JOIN institutions i ON a.institution_id = i.id
  WHERE i.slug = 'mercadopago' AND p.kind = 'wallet'
  ORDER BY p.created_at LIMIT 1
) p
WHERE w.mercadopago_balance IS NOT NULL
ON CONFLICT (product_id, as_of) DO NOTHING;

INSERT INTO product_balances (product_id, balance, as_of, source)
SELECT p.id, w.banchile_savings, w.snapshot_date::timestamptz, 'wealth_snapshot'
FROM wealth_snapshots w
CROSS JOIN LATERAL (
  SELECT p.id FROM products p
  JOIN accounts a ON p.account_id = a.id
  JOIN institutions i ON a.institution_id = i.id
  WHERE i.slug = 'banchile' AND p.kind = 'savings'
  ORDER BY p.created_at LIMIT 1
) p
WHERE w.banchile_savings IS NOT NULL
ON CONFLICT (product_id, as_of) DO NOTHING;

-- Refresh denormalized balances for products that only gained history in step 7
UPDATE products p
SET current_balance = b.balance, balance_as_of = b.as_of
FROM (
  SELECT DISTINCT ON (product_id) product_id, balance, as_of
  FROM product_balances
  ORDER BY product_id, as_of DESC
) b
WHERE b.product_id = p.id
  AND (p.balance_as_of IS NULL OR b.as_of > p.balance_as_of);
