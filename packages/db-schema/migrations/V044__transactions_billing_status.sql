-- V044: Whether a card movement has been billed (facturado), and on which
-- statement.
--
-- A credit card's movements sit "por facturar" until the statement that closes
-- after them bills them. The institutions show the two states apart (separate
-- legs, tabs or views), so a scraper can say which one a movement was seen in:
--
--   billing_status   'unbilled' (por facturar) or 'billed' (facturado).
--                    NULL means the source did not say: manual rows, products
--                    without a billing cycle, rows imported before this
--                    migration, and scrapers that expose nothing.
--   statement_date   closing date of the statement that billed the movement,
--                    when the source reports it. Only a billed row has one,
--                    and a billed row may still have none (a source that only
--                    names the "last billed period" view gives no date).
--
-- The columns are kind-agnostic, but only credit cards fill them today. The
-- latest statement's own figures (closing, next closing and due dates, billed
-- amount, minimum payment) live in the card's metrics, not here.
--
-- MONOTONIC: the scraper writer inserts both as given and, on a row it already
-- stored (or re-keys in place), only moves the status forward. 'unbilled' only
-- fills a NULL status, 'billed' replaces NULL or 'unbilled', and nothing ever
-- turns 'billed' back into 'unbilled' or NULL. A NULL statement_date is filled,
-- a stored one is never overwritten, so an installment purchase keeps the
-- statement it was first billed on. Neither column is ever part of a dedup key,
-- and neither is editable from the dashboard.
--
-- NO BACKFILL: nothing stored says which rows were billed; the next scrape
-- fills the rows still inside each source's window.
--
-- Safe on a fresh database, and idempotent.

ALTER TABLE transactions
    ADD COLUMN IF NOT EXISTS billing_status TEXT,
    ADD COLUMN IF NOT EXISTS statement_date DATE;

ALTER TABLE transactions
    DROP CONSTRAINT IF EXISTS chk_transactions_billing_status,
    DROP CONSTRAINT IF EXISTS chk_transactions_statement_date_billed;

ALTER TABLE transactions
    ADD CONSTRAINT chk_transactions_billing_status
        CHECK (billing_status IN ('unbilled', 'billed')),
    -- IS NOT DISTINCT FROM, not =: with a NULL status `=` yields NULL, which a
    -- CHECK lets through, so a dated row with no status would slip in.
    ADD CONSTRAINT chk_transactions_statement_date_billed
        CHECK (statement_date IS NULL OR billing_status IS NOT DISTINCT FROM 'billed');

COMMENT ON COLUMN transactions.billing_status IS
    'unbilled (por facturar) or billed (facturado), as the source reports it; NULL when not reported. Never moves back from billed.';
COMMENT ON COLUMN transactions.statement_date IS
    'Closing date of the statement that billed the transaction; only on billed rows, NULL when not reported. Never overwritten once set.';
