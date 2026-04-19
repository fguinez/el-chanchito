-- Accounts: one per financial account
CREATE TABLE accounts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL,
  institution TEXT NOT NULL,       -- 'banchile' | 'mercadopago' | 'fintual' | 'bci' | 'mach' | 'tenpo' | 'buda'
  account_type TEXT NOT NULL,      -- 'checking' | 'credit_card' | 'investment' | 'prepaid' | 'crypto'
  currency TEXT NOT NULL DEFAULT 'CLP',
  is_active BOOLEAN NOT NULL DEFAULT true,
  display_order INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Categories for transactions
CREATE TABLE categories (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL,
  parent_id UUID REFERENCES categories(id),
  color TEXT,
  icon TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Transactions from scrapers + manual entry
CREATE TABLE transactions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id UUID NOT NULL REFERENCES accounts(id),
  description TEXT NOT NULL,
  amount INTEGER NOT NULL,                  -- CLP, negative=expense, positive=income
  transaction_date DATE NOT NULL,
  category_id UUID REFERENCES categories(id),
  scheduled_month DATE,                     -- first day of month, for budget SUMIFS logic
  source TEXT NOT NULL DEFAULT 'manual',    -- 'manual' | 'scraper_banchile' | 'scraper_fintual' | 'csv_import' | 'email_parser'
  external_id TEXT,                         -- dedup key from scraper
  is_internal_transfer BOOLEAN NOT NULL DEFAULT false,
  is_manually_categorized BOOLEAN NOT NULL DEFAULT false,
  notes TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(account_id, external_id)
);

CREATE INDEX idx_transactions_account_date ON transactions(account_id, transaction_date);
CREATE INDEX idx_transactions_scheduled_month ON transactions(scheduled_month);
CREATE INDEX idx_transactions_category ON transactions(category_id);
