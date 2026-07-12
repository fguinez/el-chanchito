-- V010: Retire the legacy "Cuenta de Ahorro Banco de Chile" savings product.
--
-- V009 created this product only so the wealth_snapshots.banchile_savings
-- backfill had somewhere to land. The real Banco de Chile account has no
-- cuenta de ahorro: those funds are the term_deposit / investment products
-- the scraper now maintains, so keeping the legacy product double-counts
-- net worth. Scoped exactly like V009's insert predicate (institution slug
-- 'banchile', kind 'savings') and safe to run when no such product exists.
-- wealth_snapshots is untouched: its banchile_savings column remains the
-- authoritative source for legacy manual-snapshot dates.

-- product_balances has no ON DELETE CASCADE, so drop the history first
DELETE FROM product_balances b
USING products p
JOIN accounts a ON p.account_id = a.id
JOIN institutions i ON a.institution_id = i.id
WHERE b.product_id = p.id
  AND i.slug = 'banchile'
  AND p.kind = 'savings';

DELETE FROM products p
USING accounts a
JOIN institutions i ON a.institution_id = i.id
WHERE p.account_id = a.id
  AND i.slug = 'banchile'
  AND p.kind = 'savings';
