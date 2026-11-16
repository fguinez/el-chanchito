// X-Wing (draft-connolly-cfrg-xwing-kem): the hybrid ML-KEM-768 + X25519
// KEM that draft-ietf-hpke-pq registers as HPKE KEM 0x647a
// (MLKEM768-X25519), as a KemInterface for @hpke/core over node:crypto.
// Keys go through DER so it runs on every Node with crypto.encapsulate
// (24.7+); @hpke/core only passes the key objects back, never inspects them.
//
// The dashboard only seals, so this side is public keys and encapsulation
// alone: private keys and decapsulation throw NotSupportedError. The tests
// open what it seals with the recipient side in
// src/lib/__tests__/xwing-recipient.ts, built on the exports below.

import "server-only";
import {
  createHash,
  createPublicKey,
  diffieHellman,
  encapsulate,
  generateKeyPairSync,
  type KeyObject,
} from "node:crypto";
import {
  DeserializeError,
  EncapError,
  KemId,
  NotSupportedError,
  type KemInterface,
  type RecipientContextParams,
  type SenderContextParams,
} from "@hpke/core";

export const MLKEM_PUBLIC_KEY_BYTES = 1184;
export const MLKEM_CIPHERTEXT_BYTES = 1088;
export const X25519_BYTES = 32;
export const XWING_PUBLIC_KEY_BYTES = MLKEM_PUBLIC_KEY_BYTES + X25519_BYTES;
export const XWING_ENC_BYTES = MLKEM_CIPHERTEXT_BYTES + X25519_BYTES;
export const XWING_SEED_BYTES = 32;

const hex = (s: string) => Buffer.from(s, "hex");
const MLKEM_SPKI = hex("308204b2300b0609608648016503040402038204a100");
const X25519_SPKI = hex("302a300506032b656e032100");
// "\.//^\", the X-Wing combiner label.
const XWING_LABEL = hex("5c2e2f2f5e5c");

export interface XWingPublicKey {
  raw: Buffer;
  mlkem: KeyObject;
  x25519: KeyObject;
}

const spki = (prefix: Buffer, raw: Uint8Array) =>
  createPublicKey({ key: Buffer.concat([prefix, raw]), format: "der", type: "spki" });

/** A raw X25519 public key (an X-Wing key's last 32 bytes, or its enc's). */
export const x25519PublicKey = (raw: Uint8Array) => spki(X25519_SPKI, raw);

/** The raw bytes of a public key object, the last `size` bytes of its SPKI. */
export const rawPublicKey = (key: KeyObject, size: number) =>
  key.export({ format: "der", type: "spki" }).subarray(-size);

export function bytes(input: ArrayBufferLike | ArrayBufferView): Buffer {
  return ArrayBuffer.isView(input)
    ? Buffer.from(input.buffer, input.byteOffset, input.byteLength)
    : Buffer.from(input);
}

export function toArrayBuffer(buf: Uint8Array): ArrayBuffer {
  return buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength) as ArrayBuffer;
}

export function publicKeyFromBytes(raw: Buffer): XWingPublicKey {
  if (raw.byteLength !== XWING_PUBLIC_KEY_BYTES) throw new DeserializeError("bad X-Wing public key size");
  return {
    raw: Buffer.from(raw),
    mlkem: spki(MLKEM_SPKI, raw.subarray(0, MLKEM_PUBLIC_KEY_BYTES)),
    x25519: x25519PublicKey(raw.subarray(MLKEM_PUBLIC_KEY_BYTES)),
  };
}

/** The X-Wing combiner: SHA3-256(ss_ML-KEM, ss_X25519, ct_X25519, pk_X25519, label). */
export function xwingCombine(ssM: Buffer, ssX: Buffer, ctX: Buffer, pkX: Buffer): Buffer {
  return createHash("sha3-256").update(Buffer.concat([ssM, ssX, ctX, pkX, XWING_LABEL])).digest();
}

// The key objects are not WebCrypto keys; the interface types them as such.
const asCryptoKey = (key: XWingPublicKey) => key as unknown as CryptoKey;
const SEAL_ONLY = "the dashboard only seals: X-Wing private keys are not supported here";

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

  // eslint-disable-next-line @typescript-eslint/no-unused-vars -- the KemInterface signature
  async serializePrivateKey(_key: CryptoKey): Promise<ArrayBuffer> {
    throw new NotSupportedError(SEAL_ONLY);
  }

  // eslint-disable-next-line @typescript-eslint/no-unused-vars -- the KemInterface signature
  async deserializePrivateKey(_key: ArrayBufferLike | ArrayBufferView): Promise<CryptoKey> {
    throw new NotSupportedError(SEAL_ONLY);
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
    throw new NotSupportedError(SEAL_ONLY);
  }

  async deriveKeyPair(): Promise<CryptoKeyPair> {
    throw new NotSupportedError("X-Wing deriveKeyPair is not supported");
  }

  async encap(params: SenderContextParams): Promise<{ sharedSecret: ArrayBuffer; enc: ArrayBuffer }> {
    if (params.senderKey !== undefined) throw new NotSupportedError("X-Wing has no auth mode");
    try {
      const pk = params.recipientPublicKey as unknown as XWingPublicKey;
      const { sharedKey: ssM, ciphertext: ctM } = encapsulate(pk.mlkem);
      const ephemeral = generateKeyPairSync("x25519");
      const ctX = rawPublicKey(ephemeral.publicKey, X25519_BYTES);
      const ssX = diffieHellman({ privateKey: ephemeral.privateKey, publicKey: pk.x25519 });
      return {
        sharedSecret: toArrayBuffer(xwingCombine(ssM, ssX, ctX, pk.raw.subarray(MLKEM_PUBLIC_KEY_BYTES))),
        enc: toArrayBuffer(Buffer.concat([ctM, ctX])),
      };
    } catch (e: unknown) {
      throw new EncapError(e);
    }
  }

  // eslint-disable-next-line @typescript-eslint/no-unused-vars -- the KemInterface signature
  async decap(_params: RecipientContextParams): Promise<ArrayBuffer> {
    throw new NotSupportedError(SEAL_ONLY);
  }
}
