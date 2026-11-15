-- V048: Scraper credentials entered from the dashboard, sealed.
--
-- The scraper service owns an X25519 key pair. Its private key never leaves
-- the service's key file (CREDENTIALS_KEY_FILE); it publishes the public key
-- here, in the single `credential_keys` row, on every start. The dashboard
-- seals each field to that public key with HPKE (RFC 9180: DHKEM(X25519,
-- HKDF-SHA256), HKDF-SHA256, AES-256-GCM) and stores only the result in
-- `institution_credentials`, so the dashboard can write a credential but not
-- read one back, and this database alone opens nothing.
--
-- `key_id` is the hex SHA-256 of the raw public key a row was sealed to. A
-- row whose key_id is not the current key's (the key file was lost or
-- replaced) can no longer be opened and must be entered again. `sealed` is
-- the HPKE encapsulated key followed by the ciphertext; the sealing context
-- binds it to its scheme, institution, field and key, so a row moved to
-- another field or institution fails to open.
--
-- Idempotent.

CREATE TABLE IF NOT EXISTS credential_keys (
  id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  key_id TEXT NOT NULL CHECK (key_id ~ '^[0-9a-f]{64}$'),
  algorithm TEXT NOT NULL,
  public_key BYTEA NOT NULL CHECK (octet_length(public_key) = 32),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS institution_credentials (
  institution_slug TEXT NOT NULL CHECK (institution_slug ~ '^[a-z][a-z0-9_]*$'),
  field_key TEXT NOT NULL CHECK (field_key ~ '^[a-z][a-z0-9_]*$'),
  scheme TEXT NOT NULL,
  key_id TEXT NOT NULL CHECK (key_id ~ '^[0-9a-f]{64}$'),
  -- 32-byte encapsulated key + at least the 16-byte tag, at most a few KiB.
  sealed BYTEA NOT NULL CHECK (octet_length(sealed) BETWEEN 48 AND 4096),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (institution_slug, field_key)
);
