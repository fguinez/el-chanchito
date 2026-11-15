-- V049: credential_keys becomes a key ring.
--
-- V048 kept one public key, which any starting scraper service replaced with
-- its own; two services with different key files on one database (`make dev`
-- and `make up`) kept orphaning each other's rows. Now every key the service
-- ever published is a row, `key_id` its primary key, and at most one is
-- `active` (the one the dashboard seals to). Rotation re-seals every row to a
-- new key and retires the old one in one transaction; the scraper service
-- then deletes the old private key, so a backup taken before opens nothing.
--
-- `rotation_failed_at` / `rotation_error` record the last failed attempt to
-- rotate away from the active key (a short reason, never a value), for the
-- dashboard to show; a successful rotation starts a new row without them.
--
-- A row of `institution_credentials` may only be written under the active
-- key: the trigger below locks that key row (FOR SHARE), so a rotation, which
-- locks it FOR UPDATE, either sees the write and re-seals it or makes it fail.
--
-- Idempotent.

ALTER TABLE credential_keys ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';
ALTER TABLE credential_keys ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ;
ALTER TABLE credential_keys ADD COLUMN IF NOT EXISTS retired_at TIMESTAMPTZ;
ALTER TABLE credential_keys ADD COLUMN IF NOT EXISTS rotation_failed_at TIMESTAMPTZ;
ALTER TABLE credential_keys ADD COLUMN IF NOT EXISTS rotation_error TEXT;

-- The V048 singleton: its row becomes the active key, dated by when it was
-- last published.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = current_schema() AND table_name = 'credential_keys'
      AND column_name = 'id'
  ) THEN
    UPDATE credential_keys SET created_at = updated_at WHERE created_at IS NULL;
    ALTER TABLE credential_keys DROP CONSTRAINT IF EXISTS credential_keys_pkey;
    ALTER TABLE credential_keys DROP COLUMN id;
    ALTER TABLE credential_keys DROP COLUMN updated_at;
    ALTER TABLE credential_keys ADD CONSTRAINT credential_keys_pkey PRIMARY KEY (key_id);
  END IF;
END $$;

UPDATE credential_keys SET created_at = now() WHERE created_at IS NULL;
ALTER TABLE credential_keys ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE credential_keys ALTER COLUMN created_at SET NOT NULL;
ALTER TABLE credential_keys ALTER COLUMN status DROP DEFAULT;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'credential_keys_status_check'
  ) THEN
    ALTER TABLE credential_keys ADD CONSTRAINT credential_keys_status_check CHECK (
      (status = 'active' AND retired_at IS NULL)
      OR (status = 'retired' AND retired_at IS NOT NULL)
    );
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'credential_keys_rotation_error_check'
  ) THEN
    ALTER TABLE credential_keys ADD CONSTRAINT credential_keys_rotation_error_check
      CHECK (char_length(rotation_error) <= 500);
  END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS credential_keys_one_active
  ON credential_keys ((true)) WHERE status = 'active';

CREATE OR REPLACE FUNCTION institution_credentials_require_active_key()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM 1 FROM credential_keys
    WHERE key_id = NEW.key_id AND status = 'active'
    FOR SHARE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'credential key % is not the active key', left(NEW.key_id, 12)
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS institution_credentials_active_key ON institution_credentials;
CREATE TRIGGER institution_credentials_active_key
  BEFORE INSERT OR UPDATE OF key_id, sealed ON institution_credentials
  FOR EACH ROW EXECUTE FUNCTION institution_credentials_require_active_key();
