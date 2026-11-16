import { readFileSync } from "node:fs";
import path from "node:path";
import { NotSupportedError } from "@hpke/core";
import { describe, expect, it } from "vitest";

import { XWingKem } from "@/lib/credentials/xwing";
import { XWingRecipientKem, privateKeyFromSeed, xwingDecap } from "./xwing-recipient";

interface Vectors {
  hpke_pq: { kem_id: number; skRm: string; pkRm: string; enc: string; shared_secret: string }[];
  xwing: { sk: string; pk: string; ct: string; ss: string }[];
}

const vectors: Vectors = JSON.parse(
  readFileSync(path.resolve(__dirname, "fixtures/xwing-vectors.json"), "utf8")
);
const hex = (s: string) => Buffer.from(s, "hex");

describe("X-Wing KEM against the published vectors", () => {
  it("has both KEM 0x647a vectors of draft-ietf-hpke-pq and the three X-Wing ones", () => {
    expect(vectors.hpke_pq.map((v) => v.kem_id)).toEqual([0x647a, 0x647a]);
    expect(vectors.xwing).toHaveLength(3);
  });

  it.each(vectors.hpke_pq.map((v, i) => [i, v] as const))(
    "hpke-pq vector %i: derives pkRm from skRm and decapsulates enc",
    (_, v) => {
      const key = privateKeyFromSeed(hex(v.skRm));
      expect(key.publicKey.raw.toString("hex")).toBe(v.pkRm);
      expect(xwingDecap(key, hex(v.enc)).toString("hex")).toBe(v.shared_secret);
    }
  );

  it.each(vectors.xwing.map((v, i) => [i, v] as const))(
    "X-Wing vector %i: derives pk from sk and decapsulates ct",
    (_, v) => {
      const key = privateKeyFromSeed(hex(v.sk));
      expect(key.publicKey.raw.toString("hex")).toBe(v.pk);
      expect(xwingDecap(key, hex(v.ct)).toString("hex")).toBe(v.ss);
    }
  );
});

describe("XWingKem", () => {
  it("encapsulates to a shared secret only the recipient recovers", async () => {
    const kem = new XWingKem();
    const recipientKem = new XWingRecipientKem();
    const recipient = await recipientKem.generateKeyPair();
    const other = await recipientKem.generateKeyPair();
    const publicKey = await kem.deserializePublicKey(
      await recipientKem.serializePublicKey(recipient.publicKey)
    );

    const { sharedSecret, enc } = await kem.encap({ recipientPublicKey: publicKey });

    expect(enc.byteLength).toBe(kem.encSize);
    expect(sharedSecret.byteLength).toBe(kem.secretSize);
    const mine = await recipientKem.decap({ recipientKey: recipient, enc });
    const theirs = await recipientKem.decap({ recipientKey: other.privateKey, enc });
    expect(Buffer.from(mine).equals(Buffer.from(sharedSecret))).toBe(true);
    expect(Buffer.from(theirs).equals(Buffer.from(sharedSecret))).toBe(false);
  });

  it("only seals: no private keys, no decapsulation", async () => {
    const kem = new XWingKem();
    const pair = await new XWingRecipientKem().generateKeyPair();
    const seed = new Uint8Array(32);
    await expect(kem.generateKeyPair()).rejects.toBeInstanceOf(NotSupportedError);
    await expect(kem.deserializePrivateKey(seed)).rejects.toBeInstanceOf(NotSupportedError);
    await expect(kem.importKey("raw", seed.buffer, false)).rejects.toBeInstanceOf(NotSupportedError);
    await expect(kem.serializePrivateKey(pair.privateKey)).rejects.toBeInstanceOf(NotSupportedError);
    await expect(
      kem.decap({ recipientKey: pair, enc: new Uint8Array(kem.encSize) })
    ).rejects.toBeInstanceOf(NotSupportedError);
  });

  it("round-trips keys through their byte forms and rejects wrong sizes", async () => {
    const kem = new XWingRecipientKem();
    const pair = await kem.generateKeyPair();
    const pk = await kem.serializePublicKey(pair.publicKey);
    const sk = await kem.serializePrivateKey(pair.privateKey);

    expect(pk.byteLength).toBe(kem.publicKeySize);
    expect(sk.byteLength).toBe(kem.privateKeySize);
    const restored = await kem.deserializePrivateKey(sk);
    expect(Buffer.from(await kem.serializePublicKey(await kem.deserializePublicKey(pk)))).toEqual(
      Buffer.from(pk)
    );
    expect(Buffer.from(await kem.serializePrivateKey(restored))).toEqual(Buffer.from(sk));
    await expect(kem.deserializePublicKey(new Uint8Array(32))).rejects.toThrow();
    await expect(kem.deserializePrivateKey(new Uint8Array(31))).rejects.toThrow();
  });
});
