-- V013: Retire the legacy summed BanChile inversiones products.
--
-- The BanChile scraper used to read only the "Resumen de Inversión" totals,
-- emitting one summed `term_deposit` and one summed `investment` product with
-- no external_ref. It now emits one product per depósito a plazo and per fondo
-- mutuo (external_ref = deposit number / fund name+serie), so the old sums
-- would double-count net worth next to the per-holding products (issue #36).
-- Deactivate them and delete their snapshot history so the wealth carry-forward
-- can't keep counting the retired sums (a brief dip in the historical series
-- until the per-holding products accrue history is accepted and expected, same
-- trade-off as V012). Scoped to institution slug 'banchile', kinds
-- 'term_deposit'/'investment', external_ref IS NULL, and safe to run on a fresh
-- DB where no such product exists.

-- product_snapshots has no ON DELETE CASCADE, so drop the history first
DELETE FROM product_snapshots s
USING products p
JOIN accounts a ON p.account_id = a.id
JOIN institutions i ON a.institution_id = i.id
WHERE s.product_id = p.id
  AND i.slug = 'banchile'
  AND p.kind IN ('term_deposit', 'investment')
  AND p.external_ref IS NULL;

UPDATE products p
SET is_active = false,
    current_balance = NULL,
    metrics = NULL,
    balance_as_of = NULL
FROM accounts a
JOIN institutions i ON a.institution_id = i.id
WHERE p.account_id = a.id
  AND i.slug = 'banchile'
  AND p.kind IN ('term_deposit', 'investment')
  AND p.external_ref IS NULL;
