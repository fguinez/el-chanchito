-- V025: An optional location on transactions.
--
-- Some sources say where a movement happened: Banco de Chile's card legs
-- carry the merchant's city and country on every purchase row, and a user may
-- want to record a place by hand. Every column below is nullable and
-- independent of the others: a source that reports only a city fills only
-- `location_city`, and a NULL always reads as "not reported".
--
--   location_name       merchant or place name (e.g. "Tienda de Ejemplo"),
--                       when it differs from the description
--   location_city       city, as the source spells it
--   location_country    country as a readable NAME, as the source spells it
--                       (e.g. "Chile"). Banco de Chile reports names, never
--                       ISO codes (its `codigoPais` is always null), so names
--                       are what we store; a future source that only gives an
--                       ISO 3166 code stores the code as-is rather than
--                       inventing a translation table.
--   location_latitude   WGS84 degrees, 6 decimals (~0.1 m)
--   location_longitude  WGS84 degrees, 6 decimals; both-or-neither with the
--                       latitude
--
-- Text is stored as the source gives it (trimmed, blank = NULL), with no case
-- folding, the same way descriptions are.
--
-- `location_is_manual` marks a location the user set or cleared through the
-- dashboard. The scraper writer fills a NULL location column on a row it
-- already stored (so rows imported before this migration gain their location
-- on the next scrape) but never overwrites a non-NULL one, and never touches
-- any location column of a row whose flag is set, so a user's edit, including
-- clearing a wrong location, survives every later scrape.
--
-- NO BACKFILL: nothing stored so far carries a location; the next scrape fills
-- the rows still inside each source's window.
--
-- Safe on a fresh database, and idempotent.

ALTER TABLE transactions
    ADD COLUMN IF NOT EXISTS location_name TEXT,
    ADD COLUMN IF NOT EXISTS location_city TEXT,
    ADD COLUMN IF NOT EXISTS location_country TEXT,
    ADD COLUMN IF NOT EXISTS location_latitude NUMERIC(9, 6),
    ADD COLUMN IF NOT EXISTS location_longitude NUMERIC(9, 6),
    ADD COLUMN IF NOT EXISTS location_is_manual BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE transactions
    DROP CONSTRAINT IF EXISTS chk_transactions_location_latitude,
    DROP CONSTRAINT IF EXISTS chk_transactions_location_longitude,
    DROP CONSTRAINT IF EXISTS chk_transactions_location_coordinates_pair;

ALTER TABLE transactions
    ADD CONSTRAINT chk_transactions_location_latitude
        CHECK (location_latitude BETWEEN -90 AND 90),
    ADD CONSTRAINT chk_transactions_location_longitude
        CHECK (location_longitude BETWEEN -180 AND 180),
    ADD CONSTRAINT chk_transactions_location_coordinates_pair
        CHECK ((location_latitude IS NULL) = (location_longitude IS NULL));

COMMENT ON COLUMN transactions.location_name IS
    'Merchant or place name where the transaction happened. NULL when not reported.';
COMMENT ON COLUMN transactions.location_city IS
    'City where the transaction happened, as the source spells it. NULL when not reported.';
COMMENT ON COLUMN transactions.location_country IS
    'Country name (or the source''s ISO code when that is all it gives). NULL when not reported.';
COMMENT ON COLUMN transactions.location_latitude IS
    'WGS84 latitude in degrees; set together with location_longitude or not at all.';
COMMENT ON COLUMN transactions.location_longitude IS
    'WGS84 longitude in degrees; set together with location_latitude or not at all.';
COMMENT ON COLUMN transactions.location_is_manual IS
    'True once the user set or cleared the location; scrapers then never touch it.';
