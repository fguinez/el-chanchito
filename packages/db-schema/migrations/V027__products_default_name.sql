-- V027: Store each product's default name next to its display name.
--
-- The scraper writer names a new product "Institution - kind" (plus the
-- currency when it is not CLP), or takes the name the scraper supplies (a
-- deposit number, a card brand). Until now that default lived only in the
-- INSERT, so once the user renamed a product from the dashboard the original
-- was gone and there was nothing to reset to.
--
--   default_name   the name the scrapers would give the product today. The
--                  writer refreshes it on every scrape; the dashboard never
--                  edits it.
--   name           what the dashboard shows. While it equals default_name the
--                  product "uses its default" and the writer keeps the two in
--                  step; once the user renames it, the writer leaves it alone.
--                  Resetting from the dashboard copies default_name back.
--
-- BACKFILL: default_name = name for every existing row, i.e. every stored
-- name is treated as the default (no product has been renamed through the
-- dashboard yet). Some legacy names do not match what the writer would
-- generate today (pre-V009 rows were named "<slug> - <account_type>", and
-- older non-CLP rows lack the " (USD)" suffix); the next named scrape may
-- normalize those, and displayProductName already shows such generic names
-- as the kind label.
--
-- The BEFORE INSERT trigger fills a missing default_name from name, so an
-- inserter that predates this column (a scraper process started before the
-- migration ran) keeps working instead of failing the NOT NULL.
--
-- Safe on a fresh database, and idempotent.

ALTER TABLE products ADD COLUMN IF NOT EXISTS default_name TEXT;

UPDATE products SET default_name = name WHERE default_name IS NULL;

ALTER TABLE products ALTER COLUMN default_name SET NOT NULL;

CREATE OR REPLACE FUNCTION fill_product_default_name()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.default_name IS NULL THEN
    NEW.default_name := NEW.name;
  END IF;
  RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER trg_products_default_name
  BEFORE INSERT ON products
  FOR EACH ROW
  EXECUTE FUNCTION fill_product_default_name();

COMMENT ON COLUMN products.default_name IS
    'The name the scrapers give this product (refreshed on every scrape). '
    'name equals it until the user renames the product.';
