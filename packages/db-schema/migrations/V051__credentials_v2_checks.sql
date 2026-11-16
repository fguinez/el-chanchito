-- V051: the sealed credentials speak only the two schemes, and v2 is the
-- only one a key can newly become active under.
--
-- 1. `institution_credentials.scheme` takes only the two scheme strings. v1
--    rows (`hpke-x25519-sha256-aes256gcm-v1`) may still exist on an install
--    whose v1 key has not been rotated yet; rotation re-seals them as v2
--    (`hpke-mlkem768x25519-sha256-aes256gcm-v2`).
--
-- 2. `sealed` gets the floor of its scheme instead of V048's 48 bytes for
--    every row: `enc` then the AES-GCM ciphertext and its 16-byte tag, so
--      v1  32-byte DHKEM(X25519) enc + 16 =   48 bytes
--      v2  1120-byte X-Wing enc (ML-KEM-768 ciphertext 1088, X25519 32)
--          + 16                            = 1136 bytes
--    and at most 4096 bytes for both, as before (the registry caps field
--    lengths to fit, MAX_FIELD_LENGTH).
--
-- 3. A credential key may only become active as v2
--    (`HPKE-Base(MLKEM768-X25519, HKDF-SHA256, AES-256-GCM)`); the dashboard
--    no longer seals to anything else. This is a trigger, not a CHECK, so
--    it does not break the v1 -> v2 upgrade of an older install:
--    - that install's v1 key is already the active row (V049 turned the V048
--      singleton into it, or the service published it since), and a CHECK
--      on (status, algorithm) would fail this migration on that row;
--    - even NOT VALID, a CHECK applies to every later UPDATE of the row, and
--      the service updates the active v1 row in place when an automatic
--      rotation fails (rotation_failed_at / rotation_error);
--    - rotation (apps/scrapers/db/credentials.py, rotate_key) needs the v1
--      key to be the active row: it retires it (status -> 'retired') and only
--      then inserts the new v2 key as active, in one transaction. There is
--      no pending status; the ring holds 'active' and 'retired' only.
--    So the trigger lets a row that already is active stay active as the
--    same key (same key_id, public_key and algorithm), and be retired; any
--    other way a key that is not v2 ends up active raises: an INSERT,
--    re-activating a retired row (`_activate`'s ON CONFLICT ... SET status =
--    'active'), or rewriting the active row's key_id, public_key or
--    algorithm into another key. The one case it closes: a v1 key file meeting a
--    database where that key is not already the active row (a recreated
--    database, or `make credentials-reset-key` with a v1 key file). No row
--    of that database is sealed to such a key, so nothing is lost: delete
--    the v1 key file and the service makes a v2 one.
--
-- Idempotent.

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'institution_credentials_scheme_check'
  ) THEN
    ALTER TABLE institution_credentials ADD CONSTRAINT institution_credentials_scheme_check
      CHECK (scheme IN (
        'hpke-x25519-sha256-aes256gcm-v1',
        'hpke-mlkem768x25519-sha256-aes256gcm-v2'
      ));
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'institution_credentials_scheme_sealed_check'
  ) THEN
    ALTER TABLE institution_credentials ADD CONSTRAINT institution_credentials_scheme_sealed_check
      CHECK (
        octet_length(sealed) <= 4096
        AND octet_length(sealed) >= CASE scheme
          WHEN 'hpke-mlkem768x25519-sha256-aes256gcm-v2' THEN 1136
          ELSE 48
        END
      );
  END IF;
END $$;

-- V048's flat 48 to 4096-byte check, superseded by the one above.
ALTER TABLE institution_credentials DROP CONSTRAINT IF EXISTS institution_credentials_sealed_check;

CREATE OR REPLACE FUNCTION credential_keys_activate_only_v2()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.status = 'active'
    AND NEW.algorithm <> 'HPKE-Base(MLKEM768-X25519, HKDF-SHA256, AES-256-GCM)'
    AND NOT (
      TG_OP = 'UPDATE'
      AND OLD.status = 'active'
      AND OLD.key_id = NEW.key_id
      AND OLD.public_key = NEW.public_key
      AND OLD.algorithm = NEW.algorithm
    )
  THEN
    RAISE EXCEPTION 'credential key % cannot become active: only v2 (X-Wing) keys can',
      left(NEW.key_id, 12)
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS credential_keys_activate_only_v2 ON credential_keys;
CREATE TRIGGER credential_keys_activate_only_v2
  BEFORE INSERT OR UPDATE OF status, algorithm, key_id, public_key ON credential_keys
  FOR EACH ROW EXECUTE FUNCTION credential_keys_activate_only_v2();
