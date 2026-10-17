-- V030: Seed the Skipo institution.
--
-- The scraper writer creates a missing institution on its first write with
-- name = slug and kind = 'other', so this row only gives Skipo its display
-- name, kind and site. The upsert also fixes a row the writer may already
-- have created. No products, balances or transactions are seeded: those
-- come from scrapes.
INSERT INTO institutions (slug, name, kind, country, url)
VALUES ('skipo', 'Skipo', 'exchange', 'CL', 'https://skipo.com')
ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name, kind = EXCLUDED.kind,
  country = EXCLUDED.country, url = EXCLUDED.url, updated_at = now();
