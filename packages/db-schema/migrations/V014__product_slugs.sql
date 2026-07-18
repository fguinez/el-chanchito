-- V014: Persisted product slugs (issue #42).
--
-- Products gain a `slug`: a URL-safe identifier generated once at creation and
-- never changed by renames (product editing is issue #17). Both creation paths
-- (the Python scraper writer and the web resolver) slugify the product name and
-- dedup against every slug of the institution, including inactive products, so
-- retired products keep their slugs reserved. Institution-scoped uniqueness is
-- enforced in app code; the unique index on (account_id, slug) added below is
-- the narrower DB-level backstop.
--
-- Backfill: slugify(name) with institution-scoped dedup suffixes (-2, -3, ...)
-- ordered by creation, so older products keep the bare slug. translate() folds
-- the Spanish accent set plus the ordinal indicators (º, ª), matching what the
-- app code's NFKD slugify produces on these inputs. No extensions (no
-- unaccent) so the migration runs on the stock postgres:16-alpine image.

ALTER TABLE products ADD COLUMN slug TEXT;

WITH ranked AS (
  SELECT p.id,
         base.slug AS base_slug,
         row_number() OVER (
           PARTITION BY a.institution_id, base.slug
           ORDER BY p.created_at, p.id
         ) AS rn
  FROM products p
  JOIN accounts a ON p.account_id = a.id
  CROSS JOIN LATERAL (
    SELECT COALESCE(
      NULLIF(
        trim(BOTH '-' FROM regexp_replace(
          translate(lower(p.name),
                    'áàâäãéèêëíìîïóòôöõúùûüñçºª',
                    'aaaaaeeeeiiiiooooouuuuncoa'),
          '[^a-z0-9]+', '-', 'g')),
        ''),
      replace(p.kind, '_', '-')
    ) AS slug
  ) AS base
)
UPDATE products p
SET slug = CASE WHEN r.rn = 1 THEN r.base_slug
                ELSE r.base_slug || '-' || r.rn END
FROM ranked r
WHERE p.id = r.id;

-- Defensive (V011 pattern): a literal name like "X 2" can collide with a
-- generated "x-2"; disambiguate later-created rows with their full id so the
-- unique index build cannot fail on unseen data.
UPDATE products p
SET slug = p.slug || '-' || p.id::text
FROM accounts pa
WHERE p.account_id = pa.id
  AND EXISTS (
    SELECT 1
    FROM products q
    JOIN accounts qa ON q.account_id = qa.id
    WHERE qa.institution_id = pa.institution_id
      AND q.id <> p.id
      AND q.slug = p.slug
      AND (q.created_at, q.id) < (p.created_at, p.id)
  );

ALTER TABLE products ALTER COLUMN slug SET NOT NULL;
CREATE UNIQUE INDEX uq_products_account_slug ON products (account_id, slug);
