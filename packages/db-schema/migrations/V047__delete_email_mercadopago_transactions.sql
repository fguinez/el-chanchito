-- V047: Delete the transactions the retired Mercado Pago e-mail parser stored.
--
-- Mercado Pago is now read from its wallet ledger (`mp_{movement_id}` keys).
-- The rows the old e-mail leg stored (`email_mercadopago_{digest}` keys) cover
-- the same movements, so every one of them shows up twice next to its ledger
-- twin. They are hard-deleted, soft-deleted ones included: no scraper writes
-- that key any more, so there is nothing for a kept `deleted_at` to block.
--
-- Rows pointing at them follow their foreign keys: internal transfer links
-- (V028) lose that leg (SET NULL), dismissed transfer suggestions (V031) and
-- monitor adjustments (V040) are removed with them (CASCADE).
--
-- Idempotent: a second run finds nothing to delete.

DELETE FROM transactions
WHERE starts_with(external_id, 'email_mercadopago_');
