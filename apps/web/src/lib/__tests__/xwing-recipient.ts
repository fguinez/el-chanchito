// The recipient side of X-Wing, for tests only: the dashboard seals and
// never opens, so private keys and decapsulation live here, not in
// src/lib/credentials/xwing.ts. `XWingRecipientKem` is that module's
// XWingKem plus the 32-byte-seed private keys and decap, so a suite built on
// it opens what the production KEM sealed. Every key it makes is a
// throwaway.

import {
  createHash,
  createPrivateKey,
  createPublicKey,
  decapsulate,
  diffieHellman,
  randomBytes,
  type KeyObject,
} from "node:crypto";
import { DecapError, DeserializeError, type RecipientContextParams } from "@hpke/core";

import {
  MLKEM_CIPHERTEXT_BYTES,
  MLKEM_PUBLIC_KEY_BYTES,
  X25519_BYTES,
  XWING_ENC_BYTES,
  XWING_SEED_BYTES,
  XWingKem,
  bytes,
  publicKeyFromBytes,
  rawPublicKey,
  toArrayBuffer,
  x25519PublicKey,
  xwingCombine,
  type XWingPublicKey,
} from "@/lib/credentials/xwing";

const hex = (s: string) => Buffer.from(s, "hex");
const MLKEM_PKCS8_SEED = hex("3054020100300b060960864801650304040204428040");
const X25519_PKCS8 = hex("302e020100300506032b656e04220420");

export interface XWingPrivateKey {
  seed: Buffer;
  mlkem: KeyObject;
  x25519: KeyObject;
  publicKey: XWingPublicKey;
}

const pkcs8 = (prefix: Buffer, raw: Uint8Array) =>
  createPrivateKey({ key: Buffer.concat([prefix, raw]), format: "der", type: "pkcs8" });

/** The private key is the 32-byte seed; SHAKE256 expands it into the
 *  ML-KEM-768 seed (d || z) and the X25519 scalar. */
export function privateKeyFromSeed(seed: Buffer): XWingPrivateKey {
  if (seed.byteLength !== XWING_SEED_BYTES) throw new DeserializeError("bad X-Wing seed size");
  const expanded = createHash("shake256", { outputLength: 96 }).update(seed).digest();
  const mlkem = pkcs8(MLKEM_PKCS8_SEED, expanded.subarray(0, 64));
  const x25519 = pkcs8(X25519_PKCS8, expanded.subarray(64));
  const raw = Buffer.concat([
    rawPublicKey(createPublicKey(mlkem), MLKEM_PUBLIC_KEY_BYTES),
    rawPublicKey(createPublicKey(x25519), X25519_BYTES),
  ]);
  return { seed: Buffer.from(seed), mlkem, x25519, publicKey: publicKeyFromBytes(raw) };
}

/** X-Wing decapsulation: the shared secret for `enc` (ML-KEM ct || X25519 ct). */
export function xwingDecap(key: XWingPrivateKey, enc: Buffer): Buffer {
  if (enc.byteLength !== XWING_ENC_BYTES) throw new DecapError("bad X-Wing enc size");
  const ssM = decapsulate(key.mlkem, enc.subarray(0, MLKEM_CIPHERTEXT_BYTES));
  const ctX = enc.subarray(MLKEM_CIPHERTEXT_BYTES);
  const ssX = diffieHellman({ privateKey: key.x25519, publicKey: x25519PublicKey(ctX) });
  return xwingCombine(ssM, ssX, ctX, key.publicKey.raw.subarray(MLKEM_PUBLIC_KEY_BYTES));
}

const asCryptoKey = (key: XWingPublicKey | XWingPrivateKey) => key as unknown as CryptoKey;

export class XWingRecipientKem extends XWingKem {
  override async serializePrivateKey(key: CryptoKey): Promise<ArrayBuffer> {
    return toArrayBuffer((key as unknown as XWingPrivateKey).seed);
  }

  override async deserializePrivateKey(key: ArrayBufferLike | ArrayBufferView): Promise<CryptoKey> {
    return asCryptoKey(privateKeyFromSeed(bytes(key)));
  }

  override async generateKeyPair(): Promise<CryptoKeyPair> {
    const privateKey = privateKeyFromSeed(randomBytes(XWING_SEED_BYTES));
    return { privateKey: asCryptoKey(privateKey), publicKey: asCryptoKey(privateKey.publicKey) };
  }

  override async decap(params: RecipientContextParams): Promise<ArrayBuffer> {
    const recipient = params.recipientKey as CryptoKey | CryptoKeyPair;
    const key = ("privateKey" in recipient ? recipient.privateKey : recipient) as unknown as XWingPrivateKey;
    try {
      return toArrayBuffer(xwingDecap(key, bytes(params.enc)));
    } catch (e: unknown) {
      throw new DecapError(e);
    }
  }
}
