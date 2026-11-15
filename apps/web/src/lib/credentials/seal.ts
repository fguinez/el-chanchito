// Seals a scraper credential to the scraper service's public key, server
// side only (route handlers; never imported by a client component).
//
// HPKE (RFC 9180) base mode with HKDF-SHA256 and AES-256-GCM through
// @hpke/core. The KEM follows the published key's algorithm: X-Wing
// (ML-KEM-768 + X25519, ./xwing) for v2 keys, DHKEM(X25519) for v1 keys still
// active before their rotation. The `info` string binds each ciphertext to
// its scheme, institution, field and key, so a row moved to another field,
// institution or key fails to open. The scraper service opens it
// (apps/scrapers/scrapers/credentials.py); this side holds no private key and
// can never read a stored value back.

import "server-only";
import {
  Aes256Gcm,
  CipherSuite,
  DhkemX25519HkdfSha256,
  HkdfSha256,
} from "@hpke/core";
import { XWING_PUBLIC_KEY_BYTES, XWingKem } from "./xwing";

// Must match apps/scrapers/scrapers/credentials.py. The algorithm is what
// the scraper service publishes next to its key; the scheme, stored on every
// row, names the whole construction sealed to that key.
export const CREDENTIAL_SCHEME_V1 = "hpke-x25519-sha256-aes256gcm-v1";
export const CREDENTIAL_ALGORITHM_V1 =
  "HPKE-Base(DHKEM-X25519-HKDF-SHA256, HKDF-SHA256, AES-256-GCM)";
export const CREDENTIAL_SCHEME_V2 = "hpke-mlkem768x25519-sha256-aes256gcm-v2";
export const CREDENTIAL_ALGORITHM_V2 = "HPKE-Base(MLKEM768-X25519, HKDF-SHA256, AES-256-GCM)";

const INFO_LABEL = "el-chanchito/credential";
const NAME_RE = /^[a-z][a-z0-9_]*$/;
const KEY_ID_RE = /^[0-9a-f]{64}$/;

interface Scheme {
  scheme: string;
  publicKeyBytes: number;
  suite: CipherSuite;
}

const SCHEMES = new Map<string, Scheme>([
  [
    CREDENTIAL_ALGORITHM_V1,
    {
      scheme: CREDENTIAL_SCHEME_V1,
      publicKeyBytes: 32,
      suite: new CipherSuite({
        kem: new DhkemX25519HkdfSha256(),
        kdf: new HkdfSha256(),
        aead: new Aes256Gcm(),
      }),
    },
  ],
  [
    CREDENTIAL_ALGORITHM_V2,
    {
      scheme: CREDENTIAL_SCHEME_V2,
      publicKeyBytes: XWING_PUBLIC_KEY_BYTES,
      suite: new CipherSuite({ kem: new XWingKem(), kdf: new HkdfSha256(), aead: new Aes256Gcm() }),
    },
  ],
]);

/** The scheme rows sealed to a key with this published algorithm carry, or
 *  null for an algorithm this side cannot seal to. */
export function schemeForAlgorithm(algorithm: string): string | null {
  return SCHEMES.get(algorithm)?.scheme ?? null;
}

export class CredentialSealError extends Error {}

/** NUL-separated label, scheme (the key's), slug, field key and key id, as
 *  ASCII, plus `bind:<digest>` for a secret sealed to the fields it binds
 *  (`binds` in the registry). Mirrors `sealing_info` in apps/scrapers/scrapers/credentials.py. */
export function sealingInfo(
  scheme: string,
  slug: string,
  fieldKey: string,
  keyId: string,
  binding?: string
): Uint8Array {
  if (!NAME_RE.test(slug) || !NAME_RE.test(fieldKey)) {
    throw new CredentialSealError("malformed credential slug or field key");
  }
  if (!KEY_ID_RE.test(keyId)) {
    throw new CredentialSealError("malformed credential key id");
  }
  const parts = [INFO_LABEL, scheme, slug, fieldKey, keyId];
  if (binding !== undefined) {
    if (!KEY_ID_RE.test(binding)) throw new CredentialSealError("malformed credential binding");
    parts.push(`bind:${binding}`);
  }
  return new TextEncoder().encode(parts.join("\u0000"));
}

async function sha256Hex(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", toArrayBuffer(bytes));
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

/** Hex SHA-256 of the bound values, each prefixed with its 4-byte big-endian
 *  UTF-8 length. Mirrors `binding_digest` on the Python side. */
export async function bindingDigest(values: readonly string[]): Promise<string> {
  const chunks = values.map((value) => new TextEncoder().encode(value));
  const total = chunks.reduce((n, c) => n + 4 + c.byteLength, 0);
  const out = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    new DataView(out.buffer).setUint32(offset, chunk.byteLength);
    out.set(chunk, offset + 4);
    offset += 4 + chunk.byteLength;
  }
  return sha256Hex(out);
}

function toArrayBuffer(bytes: Uint8Array): ArrayBuffer {
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
}

/** Hex SHA-256 of the raw public key: the id rows record. */
export async function keyIdFor(publicKey: Uint8Array): Promise<string> {
  return sha256Hex(publicKey);
}

export interface SealingKey {
  keyId: string;
  algorithm: string;
  publicKey: Uint8Array;
}

function schemeOf(key: SealingKey): Scheme {
  const scheme = SCHEMES.get(key.algorithm);
  if (!scheme) throw new CredentialSealError("unsupported credential key algorithm");
  return scheme;
}

/** `enc || ciphertext` for one field, sealed with the scheme of the key's
 *  algorithm, plus that scheme (the one rows must store). Refuses a key whose
 *  id, algorithm or size does not match what the scraper service published
 *  with it. */
export async function sealCredential(
  key: SealingKey,
  slug: string,
  fieldKey: string,
  value: string,
  binding?: string
): Promise<{ sealed: Uint8Array; scheme: string }> {
  const { scheme, publicKeyBytes, suite } = schemeOf(key);
  if (key.publicKey.byteLength !== publicKeyBytes) {
    throw new CredentialSealError("malformed credential public key");
  }
  if ((await keyIdFor(key.publicKey)) !== key.keyId) {
    throw new CredentialSealError("credential key id does not match its public key");
  }
  const info = sealingInfo(scheme, slug, fieldKey, key.keyId, binding);
  const recipientPublicKey = await suite.kem.deserializePublicKey(toArrayBuffer(key.publicKey));
  const { enc, ct } = await suite.seal(
    { recipientPublicKey, info: toArrayBuffer(info) },
    toArrayBuffer(new TextEncoder().encode(value))
  );
  const sealed = new Uint8Array(enc.byteLength + ct.byteLength);
  sealed.set(new Uint8Array(enc), 0);
  sealed.set(new Uint8Array(ct), enc.byteLength);
  return { sealed, scheme };
}
