-- V050: credential keys may be X-Wing (scheme v2).
--
-- New scraper-service keys are hybrid post-quantum: HPKE with the X-Wing KEM
-- (ML-KEM-768 + X25519, HPKE KEM 0x647a), HKDF-SHA256, AES-256-GCM. Their
-- public key is 1216 bytes (ML-KEM-768's 1184 then X25519's 32), so V048's
-- `octet_length(public_key) = 32` no longer holds for every key. It becomes a
-- check that ties the length to the published algorithm, and rejects any
-- algorithm the service does not use:
--
--   v1  HPKE-Base(DHKEM-X25519-HKDF-SHA256, HKDF-SHA256, AES-256-GCM)  32 bytes
--   v2  HPKE-Base(MLKEM768-X25519, HKDF-SHA256, AES-256-GCM)          1216 bytes
--
-- A v1 key keeps working until it is rotated; rotation makes a v2 key and
-- re-seals every row to it, storing the v2 scheme
-- (`hpke-mlkem768x25519-sha256-aes256gcm-v2`) in institution_credentials.
-- That table's `sealed` CHECK (48 to 4096 bytes) stays: a v2 row is its
-- 1120-byte encapsulated key, the ciphertext and the 16-byte tag, so at least
-- 1136 bytes, and the registry caps fields to fit (MAX_FIELD_LENGTH).
--
-- Idempotent.

ALTER TABLE credential_keys DROP CONSTRAINT IF EXISTS credential_keys_public_key_check;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'credential_keys_algorithm_public_key_check'
  ) THEN
    ALTER TABLE credential_keys ADD CONSTRAINT credential_keys_algorithm_public_key_check CHECK (
      (algorithm = 'HPKE-Base(DHKEM-X25519-HKDF-SHA256, HKDF-SHA256, AES-256-GCM)'
        AND octet_length(public_key) = 32)
      OR (algorithm = 'HPKE-Base(MLKEM768-X25519, HKDF-SHA256, AES-256-GCM)'
        AND octet_length(public_key) = 1216)
    );
  END IF;
END $$;
