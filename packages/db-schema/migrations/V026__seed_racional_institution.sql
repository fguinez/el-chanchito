-- V026: Seed Racional, the investment app read by the new `racional` scraper.
--
-- The writer creates a missing institution on its first write with the slug as
-- its name and kind 'other', so the scraper works without this row, but the
-- dashboard would show the raw slug. The upsert also corrects a row the writer
-- may already have created that way. Racional is an investment app (an order
-- router and investment adviser, not an asset manager), so it is a fintech.
INSERT INTO institutions (slug, name, kind, country, url)
VALUES ('racional', 'Racional', 'fintech', 'CL', 'https://racional.cl')
ON CONFLICT (slug) DO UPDATE SET
  name = EXCLUDED.name,
  kind = EXCLUDED.kind,
  country = EXCLUDED.country,
  url = EXCLUDED.url,
  updated_at = now();
