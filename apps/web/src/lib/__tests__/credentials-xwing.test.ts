import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

import { XWingKem, privateKeyFromSeed, xwingDecap } from "@/lib/credentials/xwing";

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
    const recipient = await kem.generateKeyPair();
    const other = await kem.generateKeyPair();

    const { sharedSecret, enc } = await kem.encap({ recipientPublicKey: recipient.publicKey });

    expect(enc.byteLength).toBe(kem.encSize);
    expect(sharedSecret.byteLength).toBe(kem.secretSize);
    const mine = await kem.decap({ recipientKey: recipient, enc });
    const theirs = await kem.decap({ recipientKey: other.privateKey, enc });
    expect(Buffer.from(mine).equals(Buffer.from(sharedSecret))).toBe(true);
    expect(Buffer.from(theirs).equals(Buffer.from(sharedSecret))).toBe(false);
  });

  it("round-trips keys through their byte forms and rejects wrong sizes", async () => {
    const kem = new XWingKem();
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
