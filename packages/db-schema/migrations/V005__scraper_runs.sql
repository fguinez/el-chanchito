-- Scraper run log for monitoring
CREATE TABLE scraper_runs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  scraper_name TEXT NOT NULL,                     -- 'fintself_banchile' | 'fintual_api' | 'buda_api' | 'email_parser' | 'bci_lider'
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  status TEXT NOT NULL DEFAULT 'running',          -- 'running' | 'success' | 'error' | 'partial'
  transactions_imported INTEGER DEFAULT 0,
  error_message TEXT
);

CREATE INDEX idx_scraper_runs_name_date ON scraper_runs(scraper_name, started_at DESC);
