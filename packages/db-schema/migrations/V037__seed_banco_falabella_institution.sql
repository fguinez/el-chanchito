-- V037: Seed Banco Falabella, the bank read by the new `banco_falabella` scraper.
--
-- The writer creates a missing institution on its first write with the slug as
-- its name and kind 'other', so the scraper works without this row, but the
-- dashboard would show the raw slug. The upsert also corrects a row the writer
-- may already have created that way.
INSERT INTO institutions (slug, name, kind, country, url)
VALUES ('banco_falabella', 'Banco Falabella', 'bank', 'CL', 'https://www.bancofalabella.cl')
ON CONFLICT (slug) DO UPDATE SET
  name = EXCLUDED.name,
  kind = EXCLUDED.kind,
  country = EXCLUDED.country,
  url = EXCLUDED.url,
  updated_at = now();
