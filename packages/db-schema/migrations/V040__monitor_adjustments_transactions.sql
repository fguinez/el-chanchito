-- V040: Monitor adjustments ("variaciones") become links to transactions.
--
-- A variación used to be a free-form row (adjustment_date, amount,
-- description, V022) whose amount, in the monitor currency, shifted every
-- threshold from that day to the end of the month. Now it names one
-- transaction to ignore for that monitor, with no amount, date or
-- description of its own; the engine undoes it on read, from its
-- transaction_date to the end of that local month (see
-- apps/web/src/lib/monitors).
--
-- The existing free-form rows cannot be mapped to a transaction and are
-- deleted (approved by the owner). A soft-deleted transaction (V033) stops
-- applying but keeps its link, so the dashboard can show it and the user can
-- remove it; a hard-deleted one takes its links with it (ON DELETE CASCADE).
-- A transaction is linked at most once per monitor. Idempotent.

-- Only while the V022 shape is still there, so a re-run never wipes links.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = current_schema()
      AND table_name = 'monitor_adjustments'
      AND column_name = 'adjustment_date'
  ) THEN
    DELETE FROM monitor_adjustments;
  END IF;
END $$;

DROP INDEX IF EXISTS idx_monitor_adjustments_monitor;

ALTER TABLE monitor_adjustments
  DROP COLUMN IF EXISTS adjustment_date,
  DROP COLUMN IF EXISTS amount,
  DROP COLUMN IF EXISTS description,
  ADD COLUMN IF NOT EXISTS transaction_id UUID NOT NULL
    REFERENCES transactions(id) ON DELETE CASCADE;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'uq_monitor_adjustments_monitor_transaction'
  ) THEN
    ALTER TABLE monitor_adjustments
      ADD CONSTRAINT uq_monitor_adjustments_monitor_transaction
      UNIQUE (monitor_id, transaction_id);
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_monitor_adjustments_transaction
  ON monitor_adjustments (transaction_id);
