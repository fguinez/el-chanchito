-- V043: Fixed expenses become budget items with a kind, plus the monthly income.
--
-- The Gastos fijos page turns into a salary budget (Presupuesto mensual): the
-- monthly income is split into what each item takes and what is left over.
-- Every row of fixed_expenses gets a `kind`:
--   'fixed'      Gasto fijo (rent, utilities); the default and every row so far
--   'estimated'  Consumo estimado (estimated day-to-day spending)
--   'savings'    Ahorro
--
-- Backfill by the naming convention used until now, since there was no other
-- way to tell them apart: a name starting with "Ahorro:" (any case) is savings,
-- and the kind replaces that prefix, so it is stripped and the rest trimmed
-- ("Ahorro: Viaje" -> "Viaje"; a bare "Ahorro:" keeps the name "Ahorro"); a
-- name starting with "Gastos diarios" (any case) is estimated. Every other row
-- stays 'fixed'. The backfill only looks at rows still 'fixed', so a savings
-- row is never stripped twice; but it is not a no-op on a later database: a
-- 'fixed' item named "Ahorro: ..." or "Gastos diarios ..." after this
-- migration would be re-kinded if it ran again. Flyway runs it once.
--
-- budget_settings holds the monthly income: a single row (the boolean primary
-- key can only be true), absent until the page first saves it, which reads
-- as an income of 0. Whole CLP, never negative.
--
-- The DDL is idempotent (IF NOT EXISTS, guarded CHECK) and safe on a fresh
-- database.
--
-- ROLLOUT: run this migration, then deploy the web on the new code. The new
-- web reads fixed_expenses.kind and budget_settings and fails without them
-- (500 on /api/fixed-expenses and /api/budget-settings). The old web keeps
-- working against the migrated schema: it never sends kind, so its new rows
-- default to 'fixed', and it ignores budget_settings. The scrapers do not
-- touch either table.

ALTER TABLE fixed_expenses
  ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'fixed';

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'fixed_expenses_kind_check'
  ) THEN
    ALTER TABLE fixed_expenses
      ADD CONSTRAINT fixed_expenses_kind_check
      CHECK (kind IN ('fixed', 'estimated', 'savings'));
  END IF;
END $$;

UPDATE fixed_expenses
SET kind = 'savings',
    name = COALESCE(NULLIF(btrim(substr(name, length('Ahorro:') + 1)), ''), 'Ahorro'),
    updated_at = now()
WHERE kind = 'fixed'
  AND name ILIKE 'ahorro:%';

UPDATE fixed_expenses
SET kind = 'estimated',
    updated_at = now()
WHERE kind = 'fixed'
  AND name ILIKE 'gastos diarios%';

COMMENT ON COLUMN fixed_expenses.kind IS
    'Budget item kind: fixed (Gasto fijo), estimated (Consumo estimado) or '
    'savings (Ahorro).';

CREATE TABLE IF NOT EXISTS budget_settings (
  id BOOLEAN PRIMARY KEY DEFAULT true CHECK (id),
  monthly_income INTEGER NOT NULL DEFAULT 0 CHECK (monthly_income >= 0),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE budget_settings IS
    'Single row (id is always true) with the monthly income, in whole CLP, '
    'that the Presupuesto page splits among the fixed_expenses items.';
