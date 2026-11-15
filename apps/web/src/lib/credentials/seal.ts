// Seals a scraper credential to the scraper service's public key, server
// side only (route handlers; never imported by a client component).
//
// HPKE (RFC 9180) base mode with DHKEM(X25519, HKDF-SHA256), HKDF-SHA256 and
// AES-256-GCM, through @hpke/core on the runtime's WebCrypto. The `info`
// string binds each ciphertext to its scheme, institution, field and key, so
// a row moved to another field, institution or key fails to open. The scraper
// service opens it (apps/scrapers/scrapers/credentials.py); this side holds
// no private key and can never read a stored value back.

import "server-only";
import {
  Aes256Gcm,
  CipherSuite,
  DhkemX25519HkdfSha256,
  HkdfSha256,
} from "@hpke/core";

/** Version of the whole construction; stored on every row. Must match
 *  SCHEME in apps/scrapers/scrapers/credentials.py. */
export const CREDENTIAL_SCHEME = "hpke-x25519-sha256-aes256gcm-v1";
/** What the scraper service publishes next to its key; any other value means
 *  it expects another suite, so nothing is sealed to it. */
export const CREDENTIAL_ALGORITHM =
  "HPKE-Base(DHKEM-X25519-HKDF-SHA256, HKDF-SHA256, AES-256-GCM)";

const INFO_LABEL = "el-chanchito/credential";
const NAME_RE = /^[a-z][a-z0-9_]*$/;
const KEY_ID_RE = /^[0-9a-f]{64}$/;
const X25519_PUBLIC_KEY_BYTES = 32;

const suite = new CipherSuite({
  kem: new DhkemX25519HkdfSha256(),
  kdf: new HkdfSha256(),
  aead: new Aes256Gcm(),
});

export class CredentialSealError extends Error {}

/** NUL-separated label, scheme, slug, field key and key id, as ASCII, plus
 *  `bind:<digest>` for a secret sealed to the fields it binds (`binds` in the
 *  registry). Mirrors `sealing_info` in apps/scrapers/scrapers/credentials.py. */
export function sealingInfo(
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
  const parts = [INFO_LABEL, CREDENTIAL_SCHEME, slug, fieldKey, keyId];
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

/** `enc || ciphertext` for one field. Refuses a key whose id or algorithm
 *  does not match what the scraper service published with it. */
export async function sealCredential(
  key: SealingKey,
  slug: string,
  fieldKey: string,
  value: string,
  binding?: string
): Promise<Uint8Array> {
  if (key.algorithm !== CREDENTIAL_ALGORITHM) {
    throw new CredentialSealError("unsupported credential key algorithm");
  }
  if (key.publicKey.byteLength !== X25519_PUBLIC_KEY_BYTES) {
    throw new CredentialSealError("malformed credential public key");
  }
  if ((await keyIdFor(key.publicKey)) !== key.keyId) {
    throw new CredentialSealError("credential key id does not match its public key");
  }
  const info = sealingInfo(slug, fieldKey, key.keyId, binding);
  const recipientPublicKey = await suite.kem.deserializePublicKey(toArrayBuffer(key.publicKey));
  const { enc, ct } = await suite.seal(
    { recipientPublicKey, info: toArrayBuffer(info) },
    toArrayBuffer(new TextEncoder().encode(value))
  );
  const sealed = new Uint8Array(enc.byteLength + ct.byteLength);
  sealed.set(new Uint8Array(enc), 0);
  sealed.set(new Uint8Array(ct), enc.byteLength);
  return sealed;
}
