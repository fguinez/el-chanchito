-- V032: Mark the transfers the suggestions linked on their own.
--
-- A pair suggestion (a debit and a credit of exactly the same amount on two
-- products) that scores high enough and shares neither row with another
-- suggestion is confirmed without a click (see AUTO_LINK_MIN_SCORE in
-- lib/transfer-suggestions). The dashboard labels those transfers
-- "Automática" and offers an undo, which deletes the transfer and dismisses
-- the pair so it is not linked again.
--
--   auto_linked   true while the transfer is the one the suggestions created;
--                 editing its links by hand makes it the user's (false).
--
-- Existing transfers were all confirmed or created by hand: they keep the
-- default. Safe on a fresh database, and idempotent.

ALTER TABLE internal_transfers
  ADD COLUMN IF NOT EXISTS auto_linked BOOLEAN NOT NULL DEFAULT false;

COMMENT ON COLUMN internal_transfers.auto_linked IS
    'Created by an automatic transfer suggestion confirm; cleared once the '
    'user edits its links.';
