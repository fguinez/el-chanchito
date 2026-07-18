-- V016: Retire the budget and income-source tables.
--
-- These three tables backed the old Planificacion and Configuracion pages: a
-- per-month budget config, its day-level adjustments, and the income sources
-- used to split shared expenses. That budget engine has been superseded by
-- Monitores (V015), so the pages and their APIs are gone and the tables are
-- now orphaned. Drop them. budget_adjustments has a FK to budget_configs, so
-- it goes first; CASCADE guards against any leftover dependent objects. This
-- does NOT touch fixed_expenses, which was created alongside income_sources in
-- V004 and is still in use.

DROP TABLE IF EXISTS budget_adjustments CASCADE;
DROP TABLE IF EXISTS budget_configs CASCADE;
DROP TABLE IF EXISTS income_sources CASCADE;
