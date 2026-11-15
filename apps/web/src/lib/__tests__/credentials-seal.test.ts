import { generateKeyPairSync } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import {
  Aes256Gcm,
  CipherSuite,
  DhkemX25519HkdfSha256,
  HkdfSha256,
} from "@hpke/core";
import { describe, expect, it } from "vitest";

import {
  CREDENTIAL_ALGORITHM,
  CREDENTIAL_SCHEME,
  CredentialSealError,
  bindingDigest,
  keyIdFor,
  sealCredential,
  sealingInfo,
  type SealingKey,
} from "@/lib/credentials/seal";

// Every key here is generated per test (or is the fixture's throwaway key,
// which protects nothing) and every value is synthetic.

const suite = new CipherSuite({
  kem: new DhkemX25519HkdfSha256(),
  kdf: new HkdfSha256(),
  aead: new Aes256Gcm(),
});

const VALUE = "synthetic-password-1234";

async function recipient() {
  const pair = await suite.kem.generateKeyPair();
  const publicKey = new Uint8Array(await suite.kem.serializePublicKey(pair.publicKey));
  const key: SealingKey = {
    keyId: await keyIdFor(publicKey),
    algorithm: CREDENTIAL_ALGORITHM,
    publicKey,
  };
  return { pair, key };
}

function split(sealed: Uint8Array) {
  return { enc: sealed.slice(0, 32), ct: sealed.slice(32) };
}

describe("sealCredential", () => {
  it("opens only with the recipient key and the same context", async () => {
    const { pair, key } = await recipient();
    const sealed = await sealCredential(key, "banchile", "password", VALUE);
    expect(Buffer.from(sealed).includes(Buffer.from(VALUE))).toBe(false);

    const { enc, ct } = split(sealed);
    const opened = await suite.open(
      { recipientKey: pair.privateKey, enc, info: sealingInfo("banchile", "password", key.keyId) },
      ct
    );
    expect(new TextDecoder().decode(opened)).toBe(VALUE);

    await expect(
      suite.open(
        {
          recipientKey: pair.privateKey,
          enc,
          info: sealingInfo("santander", "password", key.keyId),
        },
        ct
      )
    ).rejects.toThrow();
  });

  it("seals the same value differently every time", async () => {
    const { key } = await recipient();
    const a = await sealCredential(key, "buda", "api_key", VALUE);
    const b = await sealCredential(key, "buda", "api_key", VALUE);
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

  it("refuses a malformed context", () => {
    expect(() => sealingInfo("bad|slug", "password", "0".repeat(64))).toThrow(CredentialSealError);
    expect(() => sealingInfo("banchile", "Password", "0".repeat(64))).toThrow(CredentialSealError);
    expect(() => sealingInfo("banchile", "password", "xyz")).toThrow(CredentialSealError);
  });
});

// The Python side opens a fixture sealed here (apps/scrapers/tests/
// test_credentials.py::test_dashboard_fixture_opens). Regenerate it with:
//   WRITE_CREDENTIALS_FIXTURE=1 corepack pnpm --filter @chanchito/web test credentials-seal
const FIXTURE = path.resolve(
  __dirname,
  "../../../../scrapers/tests/fixtures/credentials/ts_sealed.json"
);

describe.runIf(process.env.WRITE_CREDENTIALS_FIXTURE === "1")("cross-language fixture", () => {
  it("writes a throwaway key and values sealed with it", async () => {
    const { privateKey, publicKey } = generateKeyPairSync("x25519");
    const jwk = publicKey.export({ format: "jwk" });
    const raw = new Uint8Array(Buffer.from(jwk.x as string, "base64url"));
    const key: SealingKey = { keyId: await keyIdFor(raw), algorithm: CREDENTIAL_ALGORITHM, publicKey: raw };
    const entries: { slug: string; field: string; value: string; binding_values?: string[] }[] = [
      { slug: "banchile", field: "rut", value: "11.111.111-1" },
      { slug: "banchile", field: "password", value: "synthetic-password" },
      {
        slug: "email",
        field: "imap_password",
        value: "synthetic äpp pässwörd ✓",
        binding_values: ["imap.gmail.com", "üser@example.com"],
      },
    ];
    // Raw hex rather than PEM, so secret scanners do not flag a throwaway.
    const scalar = Buffer.from(privateKey.export({ format: "jwk" }).d as string, "base64url");
    const fixture = {
      note: "Synthetic: a throwaway X25519 recipient generated for this test, protecting nothing.",
      scheme: CREDENTIAL_SCHEME,
      recipient_fingerprint: key.keyId,
      recipient_scalar_hex: scalar.toString("hex"),
      entries: await Promise.all(
        entries.map(async (entry) => ({
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
      ),
    };
    mkdirSync(path.dirname(FIXTURE), { recursive: true });
    writeFileSync(FIXTURE, JSON.stringify(fixture, null, 2) + "\n");
  });
});
