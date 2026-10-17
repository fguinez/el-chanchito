-- V023: Let transaction amounts hold cents (issue #79).
--
-- `transactions.amount` has been `integer` since V001 because every source
-- imported so far books whole CLP. Foreign-currency products do not: a USD card
-- or checking movement such as USD 1.234,56 has cents, and storing it as an
-- integer would either round them away or force every writer to pick an
-- arbitrary scale. The column becomes `numeric(18, 2)`: exact (no float
-- drift), two decimals (the minor unit of every fiat currency we hold), and 16
-- integer digits, far above any amount a personal account will ever see.
-- `amount` stays in the currency of the product it belongs to
-- (`products.currency`); nothing is converted here.
--
-- Existing values are unchanged: `integer` to `numeric` is an exact cast, so a
-- stored -999999 becomes -999999.00 and compares equal to the integer the
-- scrapers keep sending for CLP movements. The writer's adoption lookup
-- (`amount = %s`, see V018) therefore keeps matching every stored row.
--
-- Nothing else depends on the column's type: no view or function reads it, the
-- category trigger only reads the description, and the one index that covers
-- it (idx_transactions_product_date_amount, V018) is rebuilt by the ALTER
-- itself. The rewrite takes an exclusive lock for its (short) duration.
--
-- Note for code that formats the column as text: `amount::text` now carries
-- the scale ("-999999.00"), unlike the integer it was. V017's one-off
-- fingerprint backfill ran against the integer column and is unaffected; any
-- future SQL that hashes an amount must format it explicitly.
--
-- Safe on a fresh database, and idempotent (re-running the ALTER is a no-op on
-- the values).

ALTER TABLE transactions
    ALTER COLUMN amount TYPE NUMERIC(18, 2);

COMMENT ON COLUMN transactions.amount IS
    'Signed amount in the product''s currency (products.currency): negative = '
    'expense, positive = income. NUMERIC(18, 2) since V023 so foreign-currency '
    'movements keep their cents; CLP amounts are whole numbers.';
