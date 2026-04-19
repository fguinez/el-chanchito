-- Latest balance per account, updated by scrapers
CREATE TABLE account_balances (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id UUID NOT NULL REFERENCES accounts(id),
  balance INTEGER NOT NULL,          -- CLP
  as_of TIMESTAMPTZ NOT NULL DEFAULT now(),
  source TEXT NOT NULL DEFAULT 'scraper',
  UNIQUE(account_id)                 -- one row per account, always the latest
);

CREATE INDEX idx_account_balances_account ON account_balances(account_id);
