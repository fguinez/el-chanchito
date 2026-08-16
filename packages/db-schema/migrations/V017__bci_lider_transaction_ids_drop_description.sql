-- V017: Re-key the BCI Lider transactions so a billed charge stops duplicating.
--
-- The scraper hashed date + description + amount into `external_id`. The portal
-- rewrites the description once a charge moves from "Por facturar" to "Último
-- periodo facturado" (a city suffix appears, a truncated name grows, or the
-- merchant's legal name replaces the storefront one), so the same charge came
-- back under a new id and was imported twice, inflating the month's spend and
-- breaking the balance reconstruction on the product page. The scraper now
-- hashes date + amount only.
--
-- This collapses the duplicate pairs already stored (keeping the categorized
-- row, else the oldest) and rewrites every remaining BCI Lider id to the new
-- scheme, so the next scrape matches instead of re-importing. Safe on a fresh DB
-- where no such transaction exists.

-- 1. Drop duplicates of the same charge: same product, date and amount.
DELETE FROM transactions t
USING (
    SELECT t.id,
           row_number() OVER (
               PARTITION BY t.product_id, t.transaction_date, t.amount
               ORDER BY t.is_manually_categorized DESC,
                        (t.category_id IS NULL),
                        t.created_at
           ) AS rn
    FROM transactions t
    JOIN products p ON t.product_id = p.id
    JOIN accounts a ON p.account_id = a.id
    JOIN institutions i ON a.institution_id = i.id
    WHERE i.slug = 'bci_lider'
      AND t.external_id LIKE 'bcl\_%'
) dup
WHERE t.id = dup.id
  AND dup.rn > 1;

-- 2. Re-key the survivors with the description-free hash the scraper now emits.
UPDATE transactions t
SET external_id = 'bcl_' || substr(
        md5(t.transaction_date::text || '|' || t.amount::text || '|CLP'), 1, 16
    )
FROM products p
JOIN accounts a ON p.account_id = a.id
JOIN institutions i ON a.institution_id = i.id
WHERE t.product_id = p.id
  AND i.slug = 'bci_lider'
  AND t.external_id LIKE 'bcl\_%';
