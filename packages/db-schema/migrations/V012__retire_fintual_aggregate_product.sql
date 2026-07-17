-- V012: Retire the legacy summed Fintual product.
--
-- The Fintual scraper used to collapse every goal into one `investment`
-- product with no external_ref. It now emits one product per goal
-- (external_ref = goal id, nav/deposited/profit metrics), so the old
-- aggregate would double-count net worth next to the per-goal products.
-- Deactivate it and delete its snapshot history so the wealth carry-forward
-- can't keep counting the retired sum (a brief dip in the historical series
-- until the per-goal products accrue history is accepted and expected).
-- Scoped to institution slug 'fintual', kind 'investment', external_ref IS
-- NULL, and safe to run on a fresh DB where no such product exists.

-- product_snapshots has no ON DELETE CASCADE, so drop the history first
DELETE FROM product_snapshots s
USING products p
JOIN accounts a ON p.account_id = a.id
JOIN institutions i ON a.institution_id = i.id
WHERE s.product_id = p.id
  AND i.slug = 'fintual'
  AND p.kind = 'investment'
  AND p.external_ref IS NULL;

UPDATE products p
SET is_active = false,
    current_balance = NULL,
    metrics = NULL,
    balance_as_of = NULL
FROM accounts a
JOIN institutions i ON a.institution_id = i.id
WHERE p.account_id = a.id
  AND i.slug = 'fintual'
  AND p.kind = 'investment'
  AND p.external_ref IS NULL;
