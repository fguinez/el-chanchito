-- V022: Monitor adjustments, the "Variaciones" of the old planning sheet
-- (issue #15).
--
-- An adjustment is a dated, one-off shift of a monitor's thresholds, e.g. a
-- reimbursement or a one-off gift budget. From adjustment_date to the end of
-- that calendar month its amount is added to every threshold of the monitor;
-- a new month starts with none carried over, like DAY_OF_MONTH() ramps.
-- Several adjustments may share a day and add up. amount is in the monitor's
-- currency and its sign is the direction the thresholds move. Nothing is
-- precomputed: the API applies adjustments when it evaluates a monitor and
-- replays its history (see apps/web/src/lib/monitors).
--
-- This replaces budget_adjustments (V002, dropped in V016 with the retired
-- budget engine), which fed the old Planificacion expected-balance column.

CREATE TABLE IF NOT EXISTS monitor_adjustments (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  monitor_id UUID NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
  adjustment_date DATE NOT NULL,
  amount NUMERIC(20, 8) NOT NULL CHECK (amount <> 0),
  description TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_monitor_adjustments_monitor
  ON monitor_adjustments (monitor_id, adjustment_date);
