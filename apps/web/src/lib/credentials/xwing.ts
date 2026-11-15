// X-Wing (draft-connolly-cfrg-xwing-kem): the hybrid ML-KEM-768 + X25519
// KEM that draft-ietf-hpke-pq registers as HPKE KEM 0x647a
// (MLKEM768-X25519), as a KemInterface for @hpke/core over node:crypto.
// Keys go through DER so it runs on every Node with crypto.encapsulate
// (24.7+); @hpke/core only passes the key objects back, never inspects them.

import "server-only";
import {
  createHash,
  createPrivateKey,
  createPublicKey,
  decapsulate,
  diffieHellman,
  encapsulate,
  generateKeyPairSync,
  randomBytes,
  type KeyObject,
} from "node:crypto";
import {
  DecapError,
  DeserializeError,
  EncapError,
  KemId,
  NotSupportedError,
  type KemInterface,
  type RecipientContextParams,
  type SenderContextParams,
} from "@hpke/core";

const MLKEM_PUBLIC = 1184;
const MLKEM_CIPHERTEXT = 1088;
const X25519_BYTES = 32;
export const XWING_PUBLIC_KEY_BYTES = MLKEM_PUBLIC + X25519_BYTES;
export const XWING_ENC_BYTES = MLKEM_CIPHERTEXT + X25519_BYTES;
export const XWING_SEED_BYTES = 32;

const hex = (s: string) => Buffer.from(s, "hex");
const MLKEM_SPKI = hex("308204b2300b0609608648016503040402038204a100");
const MLKEM_PKCS8_SEED = hex("3054020100300b060960864801650304040204428040");
const X25519_SPKI = hex("302a300506032b656e032100");
const X25519_PKCS8 = hex("302e020100300506032b656e04220420");
// "\.//^\", the X-Wing combiner label.
const XWING_LABEL = hex("5c2e2f2f5e5c");

interface XWingPublicKey {
  raw: Buffer;
  mlkem: KeyObject;
  x25519: KeyObject;
}

interface XWingPrivateKey {
  seed: Buffer;
  mlkem: KeyObject;
  x25519: KeyObject;
  publicKey: XWingPublicKey;
}

const spki = (prefix: Buffer, raw: Uint8Array) =>
  createPublicKey({ key: Buffer.concat([prefix, raw]), format: "der", type: "spki" });
const pkcs8 = (prefix: Buffer, raw: Uint8Array) =>
  createPrivateKey({ key: Buffer.concat([prefix, raw]), format: "der", type: "pkcs8" });
const rawPublic = (key: KeyObject, size: number) =>
  key.export({ format: "der", type: "spki" }).subarray(-size);

function bytes(input: ArrayBufferLike | ArrayBufferView): Buffer {
  return ArrayBuffer.isView(input)
    ? Buffer.from(input.buffer, input.byteOffset, input.byteLength)
    : Buffer.from(input);
}

function toArrayBuffer(buf: Uint8Array): ArrayBuffer {
  return buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength) as ArrayBuffer;
}

function publicKeyFromBytes(raw: Buffer): XWingPublicKey {
  if (raw.byteLength !== XWING_PUBLIC_KEY_BYTES) throw new DeserializeError("bad X-Wing public key size");
  return {
    raw: Buffer.from(raw),
    mlkem: spki(MLKEM_SPKI, raw.subarray(0, MLKEM_PUBLIC)),
    x25519: spki(X25519_SPKI, raw.subarray(MLKEM_PUBLIC)),
  };
}

/** The private key is the 32-byte seed; SHAKE256 expands it into the
 *  ML-KEM-768 seed (d || z) and the X25519 scalar. */
export function privateKeyFromSeed(seed: Buffer): XWingPrivateKey {
  if (seed.byteLength !== XWING_SEED_BYTES) throw new DeserializeError("bad X-Wing seed size");
  const expanded = createHash("shake256", { outputLength: 96 }).update(seed).digest();
  const mlkem = pkcs8(MLKEM_PKCS8_SEED, expanded.subarray(0, 64));
  const x25519 = pkcs8(X25519_PKCS8, expanded.subarray(64));
  const raw = Buffer.concat([
    rawPublic(createPublicKey(mlkem), MLKEM_PUBLIC),
    rawPublic(createPublicKey(x25519), X25519_BYTES),
  ]);
  return { seed: Buffer.from(seed), mlkem, x25519, publicKey: publicKeyFromBytes(raw) };
}

function combine(ssM: Buffer, ssX: Buffer, ctX: Buffer, pkX: Buffer): Buffer {
  return createHash("sha3-256").update(Buffer.concat([ssM, ssX, ctX, pkX, XWING_LABEL])).digest();
}

/** X-Wing decapsulation: the shared secret for `enc` (ML-KEM ct || X25519 ct). */
export function xwingDecap(key: XWingPrivateKey, enc: Buffer): Buffer {
  if (enc.byteLength !== XWING_ENC_BYTES) throw new DecapError("bad X-Wing enc size");
  const ssM = decapsulate(key.mlkem, enc.subarray(0, MLKEM_CIPHERTEXT));
  const ctX = enc.subarray(MLKEM_CIPHERTEXT);
  const ssX = diffieHellman({ privateKey: key.x25519, publicKey: spki(X25519_SPKI, ctX) });
  return combine(ssM, ssX, ctX, key.publicKey.raw.subarray(MLKEM_PUBLIC));
}

// The key objects are not WebCrypto keys; the interface types them as such.
const asCryptoKey = (key: XWingPublicKey | XWingPrivateKey) => key as unknown as CryptoKey;

export class XWingKem implements KemInterface {
  readonly id = KemId.XWing;
  readonly secretSize = 32;
  readonly encSize = XWING_ENC_BYTES;
  readonly publicKeySize = XWING_PUBLIC_KEY_BYTES;
  readonly privateKeySize = XWING_SEED_BYTES;

  async serializePublicKey(key: CryptoKey): Promise<ArrayBuffer> {
    return toArrayBuffer((key as unknown as XWingPublicKey).raw);
  }

  async deserializePublicKey(key: ArrayBufferLike | ArrayBufferView): Promise<CryptoKey> {
    return asCryptoKey(publicKeyFromBytes(bytes(key)));
  }

  async serializePrivateKey(key: CryptoKey): Promise<ArrayBuffer> {
    return toArrayBuffer((key as unknown as XWingPrivateKey).seed);
  }

  async deserializePrivateKey(key: ArrayBufferLike | ArrayBufferView): Promise<CryptoKey> {
    return asCryptoKey(privateKeyFromSeed(bytes(key)));
  }

  async importKey(
    format: "raw" | "jwk",
    key: ArrayBuffer | JsonWebKey,
    isPublic = true
  ): Promise<CryptoKey> {
    if (format !== "raw" || !(key instanceof ArrayBuffer)) {
      throw new NotSupportedError("X-Wing keys import as raw bytes only");
    }
    return isPublic ? this.deserializePublicKey(key) : this.deserializePrivateKey(key);
  }

  async generateKeyPair(): Promise<CryptoKeyPair> {
    const privateKey = privateKeyFromSeed(randomBytes(XWING_SEED_BYTES));
    return { privateKey: asCryptoKey(privateKey), publicKey: asCryptoKey(privateKey.publicKey) };
  }

  async deriveKeyPair(): Promise<CryptoKeyPair> {
    throw new NotSupportedError("X-Wing deriveKeyPair is not supported");
  }

  async encap(params: SenderContextParams): Promise<{ sharedSecret: ArrayBuffer; enc: ArrayBuffer }> {
    if (typeof encapsulate !== "function") {
      throw new NotSupportedError("X-Wing needs crypto.encapsulate (Node 24.7 or later)");
    }
    if (params.senderKey !== undefined) throw new NotSupportedError("X-Wing has no auth mode");
    try {
      const pk = params.recipientPublicKey as unknown as XWingPublicKey;
      const { sharedKey: ssM, ciphertext: ctM } = encapsulate(pk.mlkem);
      const ephemeral = generateKeyPairSync("x25519");
      const ctX = rawPublic(ephemeral.publicKey, X25519_BYTES);
      const ssX = diffieHellman({ privateKey: ephemeral.privateKey, publicKey: pk.x25519 });
      return {
        sharedSecret: toArrayBuffer(combine(ssM, ssX, ctX, pk.raw.subarray(MLKEM_PUBLIC))),
        enc: toArrayBuffer(Buffer.concat([ctM, ctX])),
      };
    } catch (e: unknown) {
      throw new EncapError(e);
    }
  }

  async decap(params: RecipientContextParams): Promise<ArrayBuffer> {
    const recipient = params.recipientKey as CryptoKey | CryptoKeyPair;
    const key = ("privateKey" in recipient ? recipient.privateKey : recipient) as unknown as XWingPrivateKey;
    try {
      return toArrayBuffer(xwingDecap(key, bytes(params.enc)));
    } catch (e: unknown) {
      throw new DecapError(e);
    }
  }
}
