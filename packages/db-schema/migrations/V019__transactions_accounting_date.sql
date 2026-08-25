-- V019: Record the institution's posting date next to the date a transaction
-- occurred.
--
-- Banco de Chile reports both for every checking movement: `fecha`, when the
-- movement happened (down to the second), and `fechaContable`, when the bank
-- posted it. They are different dates for most of a typical window, and until
-- now we stored only one of them, so a genuinely useful distinction was being
-- collapsed.
--
-- `transaction_date` means what its name says: the date the transaction
-- occurred. That is what the dashboard's charts and tables are built on, and
-- since #57 it is what the BanChile scraper writes there. This migration adds a
-- nullable `accounting_date` beside it for the posting date. Nullable is the
-- point: no other source reports a second date, and a NULL must read as
-- "not reported", never as missing data. A movement that has only a posting
-- date carries it in BOTH columns; an occurrence date is never invented.
--
-- Neither column is ever part of a dedup key. `external_id` is derived from the
-- bank's own operation ids (see V018), and keying on a date is precisely the
-- bug #57 removed.
--
-- NO BACKFILL, on purpose. The BanChile rows already stored were written by
-- fintself from the portal's `fechaContable` column, so they hold the POSTING
-- date in `transaction_date`, and the date each of them actually occurred is
-- not derivable from anything we have. Rows whose movement is still inside the
-- bank's movements window are corrected by the next scrape: `upsert_transactions`
-- rewrites `transaction_date`, `accounting_date` and `scheduled_month` when it
-- adopts a row onto its operation id, and its lookup matches a stored row on
-- either of the incoming dates precisely so the shift cannot orphan it. Rows
-- whose movement has already fallen out of that window keep the posting date in
-- `transaction_date` and a NULL `accounting_date`, and that is the honest
-- outcome: fabricating an occurrence date would be worse than admitting we
-- never saw one.
--
-- Safe on a fresh database, and idempotent.

ALTER TABLE transactions
    ADD COLUMN IF NOT EXISTS accounting_date DATE;

COMMENT ON COLUMN transactions.accounting_date IS
    'Date the institution posted the transaction, when it reports one apart '
    'from the date it occurred (transaction_date). NULL when not reported. '
    'Never part of a dedup key.';
