-- V020: Drop the Buda crypto movements that were stored as CLP (issue #3).
--
-- The Buda scraper fetched BTC deposits and withdrawals next to the CLP ones,
-- truncated every amount to whole units (0.5 BTC became 0 and was skipped,
-- 1.7 BTC became 1) and, since it set no currency, filed the result under the
-- Buda CLP product as if it were pesos. `transactions.amount` is integer CLP by
-- design, so the scraper now imports CLP movements only and crypto value
-- history comes from the balance snapshots.
--
-- The rows already stored are not pesos and nothing stored can repair them (the
-- fraction is gone and there is no price for their date), so they are deleted.
-- They are recognised by the description the old scraper wrote,
-- "Buda {deposit|withdrawal} {CURRENCY}", with a currency other than CLP, and
-- by its `buda_` external id. A category or note set on one of them goes with
-- it. Nothing references `transactions`, so no other table needs touching.
-- Safe on a fresh DB where no such row exists.

DELETE FROM transactions t
USING products p
JOIN accounts a ON p.account_id = a.id
JOIN institutions i ON a.institution_id = i.id
WHERE t.product_id = p.id
  AND i.slug = 'buda'
  AND t.external_id LIKE 'buda\_%'
  AND t.description ~ '^Buda (deposit|withdrawal) \S+$'
  AND t.description !~* ' CLP$';
