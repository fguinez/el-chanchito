-- Split scraper_runs.scraper_name into method + institution.
--   method:      how we scrape          ('email' | 'fintself' | 'http_api' | 'open_banking')
--   institution: what we scrape          ('mach' | 'mercadopago' | 'tenpo' | 'banchile' | ...)

ALTER TABLE scraper_runs ADD COLUMN method      TEXT;
ALTER TABLE scraper_runs ADD COLUMN institution TEXT;

-- Backfill from the old scraper_name values.
UPDATE scraper_runs SET method = 'http_api',     institution = 'fintual'   WHERE scraper_name = 'fintual_api';
UPDATE scraper_runs SET method = 'http_api',     institution = 'buda'      WHERE scraper_name = 'buda_api';
UPDATE scraper_runs SET method = 'fintself',     institution = 'banchile'  WHERE scraper_name = 'fintself_banchile';
UPDATE scraper_runs SET method = 'open_banking', institution = 'bci_lider' WHERE scraper_name = 'bci_lider';
-- Legacy composite rows: the single 'email_parser' scraper covered MACH + MercadoPago + Tenpo
-- so we cannot attribute historical rows to a specific institution.
UPDATE scraper_runs SET method = 'email',        institution = '_legacy_composite' WHERE scraper_name = 'email_parser';

-- Anything else (unexpected): flag it explicitly instead of violating NOT NULL silently.
UPDATE scraper_runs SET method = 'unknown', institution = COALESCE(scraper_name, 'unknown')
  WHERE method IS NULL;

ALTER TABLE scraper_runs ALTER COLUMN method      SET NOT NULL;
ALTER TABLE scraper_runs ALTER COLUMN institution SET NOT NULL;

ALTER TABLE scraper_runs DROP COLUMN scraper_name;

DROP INDEX IF EXISTS idx_scraper_runs_name_date;
CREATE INDEX idx_scraper_runs_method_institution_date
  ON scraper_runs(method, institution, started_at DESC);
