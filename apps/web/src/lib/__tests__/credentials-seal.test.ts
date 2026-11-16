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
  CREDENTIAL_ALGORITHM_V2,
  CREDENTIAL_SCHEME_V2,
  CredentialSealError,
  bindingDigest,
  keyIdFor,
  schemeForAlgorithm,
  sealCredential,
  sealingInfo,
  type SealingKey,
} from "@/lib/credentials/seal";
import { XWingRecipientKem, privateKeyFromSeed } from "./xwing-recipient";

// Every key here is generated per test (or is a fixture's throwaway key,
// which protects nothing) and every value is synthetic.

// The production KEM only seals; this suite also opens (./xwing-recipient).
const suite = new CipherSuite({
  kem: new XWingRecipientKem(),
  kdf: new HkdfSha256(),
  aead: new Aes256Gcm(),
});

// An older install's v1 key (X25519), which the dashboard no longer seals to.
const ALGORITHM_V1 = "HPKE-Base(DHKEM-X25519-HKDF-SHA256, HKDF-SHA256, AES-256-GCM)";
const SCHEME_V1 = "hpke-x25519-sha256-aes256gcm-v1";
const suiteV1 = new CipherSuite({
  kem: new DhkemX25519HkdfSha256(),
  kdf: new HkdfSha256(),
  aead: new Aes256Gcm(),
});

const VALUE = "synthetic-password-1234";

async function recipient(kemSuite: CipherSuite = suite, algorithm = CREDENTIAL_ALGORITHM_V2) {
  const pair = await kemSuite.kem.generateKeyPair();
  const publicKey = new Uint8Array(await kemSuite.kem.serializePublicKey(pair.publicKey));
  const key: SealingKey = { keyId: await keyIdFor(publicKey), algorithm, publicKey };
  return { pair, key };
}

/** `enc || ciphertext`, split at X-Wing's enc size. */
function split(sealed: Uint8Array) {
  const n = suite.kem.encSize;
  return { enc: sealed.slice(0, n), ct: sealed.slice(n) };
}

describe("sealCredential", () => {
  it("opens only with the recipient key and the same context", async () => {
    const { pair, key } = await recipient();
    const { sealed, scheme } = await sealCredential(key, "banchile", "password", VALUE);
    expect(scheme).toBe(CREDENTIAL_SCHEME_V2);
    expect(Buffer.from(sealed).includes(Buffer.from(VALUE))).toBe(false);
    expect(sealed.byteLength).toBe(suite.kem.encSize + VALUE.length + 16);

    const { enc, ct } = split(sealed);
    const open = (info: Uint8Array) =>
      suite.open({ recipientKey: pair.privateKey, enc, info }, ct);
    const opened = await open(sealingInfo(scheme, "banchile", "password", key.keyId));
    expect(new TextDecoder().decode(opened)).toBe(VALUE);

    await expect(open(sealingInfo(scheme, "santander", "password", key.keyId))).rejects.toThrow();
    await expect(open(sealingInfo(SCHEME_V1, "banchile", "password", key.keyId))).rejects.toThrow();
  });

  it("seals the same value differently every time", async () => {
    const { key } = await recipient();
    const { sealed: a } = await sealCredential(key, "buda", "api_key", VALUE);
    const { sealed: b } = await sealCredential(key, "buda", "api_key", VALUE);
    expect(Buffer.from(a).equals(Buffer.from(b))).toBe(false);
  });

  it("refuses a key whose id, algorithm or size does not match", async () => {
    const { key } = await recipient();
    await expect(
      sealCredential({ ...key, keyId: "0".repeat(64) }, "buda", "api_key", VALUE)
    ).rejects.toBeInstanceOf(CredentialSealError);
    await expect(
      sealCredential({ ...key, algorithm: "RSA" }, "buda", "api_key", VALUE)
    ).rejects.toBeInstanceOf(CredentialSealError);
    await expect(
      sealCredential({ ...key, publicKey: key.publicKey.slice(1) }, "buda", "api_key", VALUE)
    ).rejects.toBeInstanceOf(CredentialSealError);
  });

  it("refuses a v1 key until it is rotated to v2", async () => {
    const v1 = await recipient(suiteV1, ALGORITHM_V1);
    await expect(sealCredential(v1.key, "buda", "api_key", VALUE)).rejects.toThrow(
      new CredentialSealError("unsupported credential key algorithm")
    );
    // Nor under the v2 label: its size gives it away.
    await expect(
      sealCredential({ ...v1.key, algorithm: CREDENTIAL_ALGORITHM_V2 }, "buda", "api_key", VALUE)
    ).rejects.toBeInstanceOf(CredentialSealError);
  });
});

describe("credential schemes", () => {
  it("follow the key's published algorithm, v2 only", () => {
    expect(schemeForAlgorithm(CREDENTIAL_ALGORITHM_V2)).toBe(CREDENTIAL_SCHEME_V2);
    expect(schemeForAlgorithm(ALGORITHM_V1)).toBeNull();
    expect(schemeForAlgorithm("RSA")).toBeNull();
  });

  it("refuse a malformed context", () => {
    const keyId = "0".repeat(64);
    expect(() => sealingInfo(CREDENTIAL_SCHEME_V2, "bad|slug", "password", keyId)).toThrow(CredentialSealError);
    expect(() => sealingInfo(CREDENTIAL_SCHEME_V2, "banchile", "Password", keyId)).toThrow(CredentialSealError);
    expect(() => sealingInfo(CREDENTIAL_SCHEME_V2, "banchile", "password", "xyz")).toThrow(CredentialSealError);
  });
});

// The Python side opens the fixture sealed here (apps/scrapers/tests/
// test_credentials_xwing.py). Regenerate it with WRITE_CREDENTIALS_FIXTURE=1:
//   WRITE_CREDENTIALS_FIXTURE=1 corepack pnpm --filter @chanchito/web test credentials-seal
// Its v1 sibling, ts_sealed.json, is frozen: the dashboard no longer seals
// v1, and the Python side keeps opening it because rotation still re-seals
// v1 rows.
const FIXTURE_V2 = path.resolve(
  __dirname,
  "../../../../scrapers/tests/fixtures/credentials/ts_sealed_v2.json"
);
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
        (
          await sealCredential(
            key,
            entry.slug,
            entry.field,
            entry.value,
            entry.binding_values ? await bindingDigest(entry.binding_values) : undefined
          )
        ).sealed
      ).toString("base64"),
    }))
  );
}

function writeFixture(file: string, fixture: object) {
  mkdirSync(path.dirname(file), { recursive: true });
  writeFileSync(file, JSON.stringify(fixture, null, 2) + "\n");
}

describe.runIf(WRITE === "1")("cross-language fixture (v2)", () => {
  it("writes a throwaway key and values sealed with it", async () => {
    const pair = await suite.kem.generateKeyPair();
    const raw = new Uint8Array(await suite.kem.serializePublicKey(pair.publicKey));
    const key: SealingKey = { keyId: await keyIdFor(raw), algorithm: CREDENTIAL_ALGORITHM_V2, publicKey: raw };
    const seed = Buffer.from(await suite.kem.serializePrivateKey(pair.privateKey));
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
    const privateKey = await suite.kem.deserializePrivateKey(seed);

    expect(fixture.scheme).toBe(CREDENTIAL_SCHEME_V2);
    expect(await keyIdFor(privateKeyFromSeed(seed).publicKey.raw)).toBe(fixture.recipient_fingerprint);
    expect(fixture.entries).toHaveLength(FIXTURE_ENTRIES.length);
    for (const entry of fixture.entries) {
      const binding = entry.binding_values ? await bindingDigest(entry.binding_values) : undefined;
      const { enc, ct } = split(Buffer.from(entry.sealed, "base64"));
      const opened = await suite.open(
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
