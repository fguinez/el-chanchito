import { generateKeyPairSync } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import {
  Aes256Gcm,
  CipherSuite,
  DhkemX25519HkdfSha256,
  HkdfSha256,
} from "@hpke/core";
import { describe, expect, it } from "vitest";

import {
  CREDENTIAL_ALGORITHM_V1,
  CREDENTIAL_ALGORITHM_V2,
  CREDENTIAL_SCHEME_V1,
  CREDENTIAL_SCHEME_V2,
  CredentialSealError,
  bindingDigest,
  credentialScheme,
  keyIdFor,
  schemeForAlgorithm,
  sealCredential,
  sealingInfo,
  type SealingKey,
} from "@/lib/credentials/seal";
import { XWingKem, privateKeyFromSeed } from "@/lib/credentials/xwing";

// Every key here is generated per test (or is a fixture's throwaway key,
// which protects nothing) and every value is synthetic.

const SCHEMES = [
  {
    name: "v2 (X-Wing)",
    algorithm: CREDENTIAL_ALGORITHM_V2,
    scheme: CREDENTIAL_SCHEME_V2,
    suite: new CipherSuite({ kem: new XWingKem(), kdf: new HkdfSha256(), aead: new Aes256Gcm() }),
  },
  {
    name: "v1 (X25519)",
    algorithm: CREDENTIAL_ALGORITHM_V1,
    scheme: CREDENTIAL_SCHEME_V1,
    suite: new CipherSuite({
      kem: new DhkemX25519HkdfSha256(),
      kdf: new HkdfSha256(),
      aead: new Aes256Gcm(),
    }),
  },
];
const [V2, V1] = SCHEMES;

const VALUE = "synthetic-password-1234";

async function recipient({ algorithm, suite }: (typeof SCHEMES)[number]) {
  const pair = await suite.kem.generateKeyPair();
  const publicKey = new Uint8Array(await suite.kem.serializePublicKey(pair.publicKey));
  const key: SealingKey = { keyId: await keyIdFor(publicKey), algorithm, publicKey };
  return { pair, key };
}

/** `enc || ciphertext`, split at the suite's own enc size. */
function split(suite: CipherSuite, sealed: Uint8Array) {
  const n = suite.kem.encSize;
  return { enc: sealed.slice(0, n), ct: sealed.slice(n) };
}

describe.each(SCHEMES)("sealCredential to a $name key", (kind) => {
  const { scheme, suite } = kind;

  it("opens only with the recipient key and the same context", async () => {
    const { pair, key } = await recipient(kind);
    const sealed = await sealCredential(key, "banchile", "password", VALUE);
    expect(Buffer.from(sealed).includes(Buffer.from(VALUE))).toBe(false);
    expect(sealed.byteLength).toBe(suite.kem.encSize + VALUE.length + 16);
    expect(credentialScheme(key)).toBe(scheme);

    const { enc, ct } = split(suite, sealed);
    const open = (info: Uint8Array) =>
      suite.open({ recipientKey: pair.privateKey, enc, info }, ct);
    const opened = await open(sealingInfo(scheme, "banchile", "password", key.keyId));
    expect(new TextDecoder().decode(opened)).toBe(VALUE);

    await expect(open(sealingInfo(scheme, "santander", "password", key.keyId))).rejects.toThrow();
    const otherScheme = scheme === CREDENTIAL_SCHEME_V1 ? CREDENTIAL_SCHEME_V2 : CREDENTIAL_SCHEME_V1;
    await expect(open(sealingInfo(otherScheme, "banchile", "password", key.keyId))).rejects.toThrow();
  });

  it("seals the same value differently every time", async () => {
    const { key } = await recipient(kind);
    const a = await sealCredential(key, "buda", "api_key", VALUE);
    const b = await sealCredential(key, "buda", "api_key", VALUE);
    expect(Buffer.from(a).equals(Buffer.from(b))).toBe(false);
  });

  it("refuses a key whose id, algorithm or size does not match", async () => {
    const { key } = await recipient(kind);
    await expect(
      sealCredential({ ...key, keyId: "0".repeat(64) }, "buda", "api_key", VALUE)
    ).rejects.toBeInstanceOf(CredentialSealError);
    await expect(
      sealCredential({ ...key, algorithm: "RSA" }, "buda", "api_key", VALUE)
    ).rejects.toBeInstanceOf(CredentialSealError);
    await expect(
      sealCredential({ ...key, publicKey: key.publicKey.slice(1) }, "buda", "api_key", VALUE)
    ).rejects.toBeInstanceOf(CredentialSealError);
    expect(() => credentialScheme({ ...key, algorithm: "RSA" })).toThrow(CredentialSealError);
  });
});

describe("credential schemes", () => {
  it("follow the key's published algorithm", () => {
    expect(schemeForAlgorithm(CREDENTIAL_ALGORITHM_V1)).toBe(CREDENTIAL_SCHEME_V1);
    expect(schemeForAlgorithm(CREDENTIAL_ALGORITHM_V2)).toBe(CREDENTIAL_SCHEME_V2);
    expect(schemeForAlgorithm("RSA")).toBeNull();
  });

  it("refuse a key of one algorithm labelled with the other", async () => {
    const v1 = await recipient(V1);
    const v2 = await recipient(V2);
    await expect(
      sealCredential({ ...v1.key, algorithm: CREDENTIAL_ALGORITHM_V2 }, "buda", "api_key", VALUE)
    ).rejects.toBeInstanceOf(CredentialSealError);
    await expect(
      sealCredential({ ...v2.key, algorithm: CREDENTIAL_ALGORITHM_V1 }, "buda", "api_key", VALUE)
    ).rejects.toBeInstanceOf(CredentialSealError);
  });

  it("refuse a malformed context", () => {
    const keyId = "0".repeat(64);
    expect(() => sealingInfo(CREDENTIAL_SCHEME_V2, "bad|slug", "password", keyId)).toThrow(CredentialSealError);
    expect(() => sealingInfo(CREDENTIAL_SCHEME_V2, "banchile", "Password", keyId)).toThrow(CredentialSealError);
    expect(() => sealingInfo(CREDENTIAL_SCHEME_V2, "banchile", "password", "xyz")).toThrow(CredentialSealError);
  });
});

// The Python side opens fixtures sealed here (apps/scrapers/tests/
// test_credentials.py). Regenerate both with WRITE_CREDENTIALS_FIXTURE=1, or
// one with =v1 or =v2:
//   WRITE_CREDENTIALS_FIXTURE=1 corepack pnpm --filter @chanchito/web test credentials-seal
const FIXTURES = path.resolve(__dirname, "../../../../scrapers/tests/fixtures/credentials");
const FIXTURE_V1 = path.join(FIXTURES, "ts_sealed.json");
const FIXTURE_V2 = path.join(FIXTURES, "ts_sealed_v2.json");
const WRITE = process.env.WRITE_CREDENTIALS_FIXTURE;

const FIXTURE_ENTRIES: { slug: string; field: string; value: string; binding_values?: string[] }[] = [
  { slug: "banchile", field: "rut", value: "11.111.111-1" },
  { slug: "banchile", field: "password", value: "synthetic-password" },
  {
    slug: "email",
    field: "imap_password",
    value: "synthetic äpp pässwörd ✓",
    binding_values: ["imap.gmail.com", "üser@example.com"],
  },
];

async function sealEntries(key: SealingKey) {
  return Promise.all(
    FIXTURE_ENTRIES.map(async (entry) => ({
      ...entry,
      sealed: Buffer.from(
        await sealCredential(
          key,
          entry.slug,
          entry.field,
          entry.value,
          entry.binding_values ? await bindingDigest(entry.binding_values) : undefined
        )
      ).toString("base64"),
    }))
  );
}

function writeFixture(file: string, fixture: object) {
  mkdirSync(path.dirname(file), { recursive: true });
  writeFileSync(file, JSON.stringify(fixture, null, 2) + "\n");
}

describe.runIf(WRITE === "1" || WRITE === "v1")("cross-language fixture (v1)", () => {
  it("writes a throwaway key and values sealed with it", async () => {
    const { privateKey, publicKey } = generateKeyPairSync("x25519");
    const jwk = publicKey.export({ format: "jwk" });
    const raw = new Uint8Array(Buffer.from(jwk.x as string, "base64url"));
    const key: SealingKey = { keyId: await keyIdFor(raw), algorithm: CREDENTIAL_ALGORITHM_V1, publicKey: raw };
    // Raw hex rather than PEM, so secret scanners do not flag a throwaway.
    const scalar = Buffer.from(privateKey.export({ format: "jwk" }).d as string, "base64url");
    writeFixture(FIXTURE_V1, {
      note: "Synthetic: a throwaway X25519 recipient generated for this test, protecting nothing.",
      scheme: CREDENTIAL_SCHEME_V1,
      recipient_fingerprint: key.keyId,
      recipient_scalar_hex: scalar.toString("hex"),
      entries: await sealEntries(key),
    });
  });
});

describe.runIf(WRITE === "1" || WRITE === "v2")("cross-language fixture (v2)", () => {
  it("writes a throwaway key and values sealed with it", async () => {
    const pair = await V2.suite.kem.generateKeyPair();
    const raw = new Uint8Array(await V2.suite.kem.serializePublicKey(pair.publicKey));
    const key: SealingKey = { keyId: await keyIdFor(raw), algorithm: CREDENTIAL_ALGORITHM_V2, publicKey: raw };
    const seed = Buffer.from(await V2.suite.kem.serializePrivateKey(pair.privateKey));
    writeFixture(FIXTURE_V2, {
      note: "Synthetic: a throwaway X-Wing recipient (random 32-byte seed) generated for this test, protecting nothing.",
      scheme: CREDENTIAL_SCHEME_V2,
      recipient_fingerprint: key.keyId,
      recipient_seed_hex: seed.toString("hex"),
      entries: await sealEntries(key),
    });
  });
});

describe.skipIf(WRITE !== undefined)("committed cross-language fixture (v2)", () => {
  it("opens with its seed, under the v2 scheme and its key id", async () => {
    const fixture = JSON.parse(readFileSync(FIXTURE_V2, "utf8"));
    const seed = Buffer.from(fixture.recipient_seed_hex, "hex");
    const privateKey = await V2.suite.kem.deserializePrivateKey(seed);

    expect(fixture.scheme).toBe(CREDENTIAL_SCHEME_V2);
    expect(await keyIdFor(privateKeyFromSeed(seed).publicKey.raw)).toBe(fixture.recipient_fingerprint);
    expect(fixture.entries).toHaveLength(FIXTURE_ENTRIES.length);
    for (const entry of fixture.entries) {
      const binding = entry.binding_values ? await bindingDigest(entry.binding_values) : undefined;
      const { enc, ct } = split(V2.suite, Buffer.from(entry.sealed, "base64"));
      const opened = await V2.suite.open(
        {
          recipientKey: privateKey,
          enc,
          info: sealingInfo(CREDENTIAL_SCHEME_V2, entry.slug, entry.field, fixture.recipient_fingerprint, binding),
        },
        ct
      );
      expect(new TextDecoder().decode(opened)).toBe(entry.value);
    }
  });
});
