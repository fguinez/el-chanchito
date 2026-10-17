-- V028: Link each internal transfer to the scraped rows that moved the money.
--
-- An internal transfer records money moving between two of the user's
-- products, but until now it was not tied to the transactions the scrapers
-- import for it (the source debit and the target credit), so both showed up in
-- Gastos and Ingresos as real spending and income. The transfer does not
-- create rows of its own (PR #70 tried that and double counted): it labels the
-- rows that already exist.
--
--   from_transaction_id   the outgoing row on from_product_id (amount < 0)
--   to_transaction_id     the incoming row on to_product_id (amount > 0)
--
-- Both are optional: a product without a transaction feed (Fintual, term
-- deposits, funds) has no row to link. UNIQUE keeps one row from serving two
-- transfers; ON DELETE SET NULL keeps the transfer when a linked row is
-- deleted (for example by the scrapers' twin cleanup, which already skips
-- rows flagged is_internal_transfer). The product, sign and amount checks and
-- the is_internal_transfer flag that follows the link live in the API
-- (PUT /api/transfers/[id]/links).
--
-- No backfill: existing transfers start unlinked. Safe on a fresh database,
-- and idempotent.

ALTER TABLE internal_transfers
  ADD COLUMN IF NOT EXISTS from_transaction_id UUID
    REFERENCES transactions(id) ON DELETE SET NULL;

ALTER TABLE internal_transfers
  ADD COLUMN IF NOT EXISTS to_transaction_id UUID
    REFERENCES transactions(id) ON DELETE SET NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'uq_internal_transfers_from_transaction'
  ) THEN
    ALTER TABLE internal_transfers
      ADD CONSTRAINT uq_internal_transfers_from_transaction
      UNIQUE (from_transaction_id);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'uq_internal_transfers_to_transaction'
  ) THEN
    ALTER TABLE internal_transfers
      ADD CONSTRAINT uq_internal_transfers_to_transaction
      UNIQUE (to_transaction_id);
  END IF;
END $$;

COMMENT ON COLUMN internal_transfers.from_transaction_id IS
    'The outgoing transaction on from_product_id this transfer accounts for '
    '(flagged is_internal_transfer while linked).';
COMMENT ON COLUMN internal_transfers.to_transaction_id IS
    'The incoming transaction on to_product_id this transfer accounts for '
    '(flagged is_internal_transfer while linked).';
