import { NextRequest } from "next/server";
import {
  Aes256Gcm,
  CipherSuite,
  DhkemX25519HkdfSha256,
  HkdfSha256,
} from "@hpke/core";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { proxy } from "@/proxy";
import {
  CREDENTIAL_ALGORITHM,
  CREDENTIAL_SCHEME,
  bindingDigest,
  keyIdFor,
  sealingInfo,
  type SealingKey,
} from "@/lib/credentials/seal";
import type { CredentialMeta, SealedField, StoredKeyInfo } from "@/lib/credentials/db";
import type { ControlCredentialStatus } from "@/lib/credentials/control";

// The credential routes run against in-memory doubles of the DB layer and of
// the scraper service's control endpoints. The point of most cases: no
// response, in any branch, ever carries a submitted value. Every value is
// synthetic and every key is generated per run.

const suite = new CipherSuite({
  kem: new DhkemX25519HkdfSha256(),
  kdf: new HkdfSha256(),
  aead: new Aes256Gcm(),
});
const pair = await suite.kem.generateKeyPair();
const publicKey = new Uint8Array(await suite.kem.serializePublicKey(pair.publicKey));
const KEY: SealingKey = {
  keyId: await keyIdFor(publicKey),
  algorithm: CREDENTIAL_ALGORITHM,
  publicKey,
};

const state = {
  key: KEY as SealingKey | null,
  meta: [] as CredentialMeta[],
  upserts: [] as { slug: string; scheme: string; keyId: string; fields: SealedField[] }[],
  rotateBeforeStore: [] as (() => void)[],
  keyInfo: null as StoredKeyInfo | null,
  deletes: [] as { slug: string; field?: string }[],
  control: null as ControlCredentialStatus | null,
  enabled: null as string[] | null,
  reload: null as { applied: boolean; added: string[]; scrapers: string[] } | null,
  reloads: 0,
  refreshed: [] as string[],
};

vi.mock("@/lib/credentials/db", () => ({
  readSealingKey: async () => state.key,
  listCredentialMeta: async () => state.meta,
  readKeyInfo: async () => state.keyInfo,
  upsertSealedFields: async (slug: string, scheme: string, keyId: string, fields: SealedField[]) => {
    state.upserts.push({ slug, scheme, keyId, fields });
    // A rotation that lands between sealing and storing: the key is retired.
    const rotate = state.rotateBeforeStore.shift();
    if (rotate) {
      rotate();
      return false;
    }
    return true;
  },
  deleteSealedFields: async (slug: string, fields?: readonly string[]) => {
    state.deletes.push({ slug, field: fields?.join(",") });
    return 1;
  },
}));

vi.mock("@/lib/credentials/control", () => ({
  fetchControlCredentialStatus: async () => state.control,
  fetchEnabledScrapers: async () => state.enabled,
  reloadControlCredentials: async () => {
    state.reloads += 1;
    return state.reload;
  },
  triggerControlRefresh: async (slug: string) => {
    state.refreshed.push(slug);
    return true;
  },
}));

const { GET } = await import("@/app/api/credentials/route");
const { PUT, DELETE } = await import("@/app/api/credentials/[slug]/route");

const PASSWORD = "synthetic-password-1234";
const RUT = "11.111.111-1";

function put(slug: string, body: unknown, contentType = "application/json") {
  return PUT(
    new NextRequest(`http://localhost:3000/api/credentials/${slug}`, {
      method: "PUT",
      headers: { "content-type": contentType },
      body: typeof body === "string" ? body : JSON.stringify(body),
    }),
    { params: Promise.resolve({ slug }) }
  );
}

function del(slug: string, field?: string) {
  const query = field ? `?field=${field}` : "";
  return DELETE(
    new NextRequest(`http://localhost:3000/api/credentials/${slug}${query}`, { method: "DELETE" }),
    { params: Promise.resolve({ slug }) }
  );
}

async function text(response: Response) {
  return response.clone().text();
}

beforeEach(() => {
  state.key = KEY;
  state.meta = [];
  state.upserts = [];
  state.rotateBeforeStore = [];
  state.keyInfo = null;
  state.deletes = [];
  state.control = { key_available: true, key_id: KEY.keyId, fields: {} };
  state.enabled = null;
  state.reload = null;
  state.reloads = 0;
  state.refreshed = [];
  delete process.env.EMAIL_IMAP_ALLOW_CUSTOM_HOST;
});

describe("PUT /api/credentials/[slug]", () => {
  it("seals each field to the published key and never echoes a value", async () => {
    state.reload = { applied: true, added: [], scrapers: ["banchile"] };
    const response = await put("banchile", { fields: { rut: ` ${RUT} `, password: PASSWORD } });

    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toBe("no-store");
    const body = await text(response);
    expect(body).not.toContain(PASSWORD);
    expect(body).not.toContain(RUT);
    expect(JSON.parse(body)).toEqual({ saved: ["rut", "password"], applied: true, triggered: [] });

    const [upsert] = state.upserts;
    expect(upsert.slug).toBe("banchile");
    expect(upsert.scheme).toBe(CREDENTIAL_SCHEME);
    expect(upsert.keyId).toBe(KEY.keyId);
    for (const field of upsert.fields) {
      expect(Buffer.from(field.sealed).includes(Buffer.from(PASSWORD))).toBe(false);
    }
    // The identifier is trimmed; the stored ciphertext opens to it.
    const rut = upsert.fields.find((f) => f.fieldKey === "rut")!;
    const opened = await suite.open(
      {
        recipientKey: pair.privateKey,
        enc: rut.sealed.slice(0, 32),
        info: sealingInfo("banchile", "rut", KEY.keyId),
      },
      rut.sealed.slice(32)
    );
    expect(new TextDecoder().decode(opened)).toBe(RUT);
    expect(state.reloads).toBe(1);
  });

  it("accepts a partial update and reports an unreachable service as null", async () => {
    const response = await put("banchile", { fields: { password: PASSWORD } });
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ saved: ["password"], applied: null, triggered: [] });
  });

  it.each([
    ["an unknown field", "banchile", { fields: { pin: PASSWORD } }, 400],
    ["an empty field set", "banchile", { fields: {} }, 400],
    ["a body without fields", "banchile", { password: PASSWORD }, 400],
    ["extra top-level keys", "banchile", { fields: { password: PASSWORD }, admin: true }, 400],
    ["a non-string value", "banchile", { fields: { password: 1234 } }, 400],
    ["a value with a newline", "banchile", { fields: { password: "synthetic\npassword" } }, 400],
    ["a blank identifier", "banchile", { fields: { rut: "   " } }, 400],
    ["a value over its max length", "skipo", { fields: { api_key: "x".repeat(257) } }, 400],
    ["a 5-digit Falabella clave", "banco_falabella", { fields: { password: "12345" } }, 400],
    ["an IMAP host with a scheme", "email", { fields: { imap_host: "imaps://imap.example.com" } }, 400],
    ["an unknown institution", "nope", { fields: { password: PASSWORD } }, 404],
    ["an institution with nothing to store", "mercadopago", { fields: {} }, 400],
  ])("refuses %s without echoing it", async (_label, slug, body, status) => {
    const response = await put(slug, body);
    expect(response.status).toBe(status);
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(await text(response)).not.toContain(PASSWORD);
    expect(state.upserts).toHaveLength(0);
  });

  it("accepts a 6-digit Falabella clave", async () => {
    const response = await put("banco_falabella", { fields: { password: "123456" } });
    expect(response.status).toBe(200);
  });

  it("refuses a body not labelled JSON", async () => {
    const response = await put("banchile", { fields: { password: PASSWORD } }, "text/plain");
    expect(response.status).toBe(415);
    expect(state.upserts).toHaveLength(0);
  });

  it("refuses an oversized body", async () => {
    const response = await put("banchile", { fields: { password: "x".repeat(20_000) } });
    expect(response.status).toBe(413);
    expect(state.upserts).toHaveLength(0);
  });

  it("answers 503 with a hint when no key is published yet", async () => {
    state.key = null;
    const response = await put("banchile", { fields: { password: PASSWORD } });
    expect(response.status).toBe(503);
    const body = await response.json();
    expect(body.reason).toBe("no_key");
    expect(body.error).toContain("clave de cifrado");
    expect(state.upserts).toHaveLength(0);
  });

  it("refuses to seal when the service cannot confirm the key", async () => {
    state.control = null;
    const response = await put("banchile", { fields: { password: PASSWORD } });
    expect(response.status).toBe(503);
    const body = await response.json();
    expect(body.reason).toBe("service_unreachable");
    expect(body.error).toContain("no responde");
    expect(state.upserts).toHaveLength(0);
  });

  it("refuses to save while the database seals to another service's key", async () => {
    state.control = { key_available: true, key_id: "0".repeat(64), key_mismatch: true, fields: {} };
    const response = await put("banchile", { fields: { password: PASSWORD } });
    expect(response.status).toBe(503);
    const body = await response.json();
    expect(body.reason).toBe("key_mismatch");
    expect(body.error).toContain("make credentials-reset-key");
    expect(state.upserts).toHaveLength(0);
  });

  it("seals again to the new key when a rotation lands mid-save", async () => {
    const next = await suite.kem.generateKeyPair();
    const nextPublic = new Uint8Array(await suite.kem.serializePublicKey(next.publicKey));
    const NEXT: SealingKey = { ...KEY, keyId: await keyIdFor(nextPublic), publicKey: nextPublic };
    state.rotateBeforeStore = [
      () => {
        state.key = NEXT;
        state.control = { key_available: true, key_id: NEXT.keyId, fields: {} };
      },
    ];
    const response = await put("skipo", { fields: { api_key: PASSWORD } });
    expect(response.status).toBe(200);
    expect(state.upserts.map((u) => u.keyId)).toEqual([KEY.keyId, NEXT.keyId]);
    const sealed = state.upserts[1].fields[0].sealed;
    const opened = await suite.open(
      {
        recipientKey: next.privateKey,
        enc: sealed.slice(0, 32),
        info: sealingInfo("skipo", "api_key", NEXT.keyId),
      },
      sealed.slice(32)
    );
    expect(new TextDecoder().decode(opened)).toBe(PASSWORD);
  });

  it("answers a retryable 409 when the key changes twice under a save", async () => {
    state.rotateBeforeStore = [() => {}, () => {}];
    const response = await put("skipo", { fields: { api_key: PASSWORD } });
    expect(response.status).toBe(409);
    const body = await text(response);
    expect(body).not.toContain(PASSWORD);
    expect(JSON.parse(body).reason).toBe("key_changed");
    expect(state.upserts).toHaveLength(2);
  });

  it("runs a scraper the save just enabled", async () => {
    state.reload = { applied: true, added: ["skipo"], scrapers: ["skipo"] };
    const response = await put("skipo", { fields: { api_key: PASSWORD } });
    expect(await response.json()).toEqual({ saved: ["api_key"], applied: true, triggered: ["skipo"] });
    expect(state.refreshed).toEqual(["skipo"]);
  });

  it.each([
    ["a lone surrogate", "synthetic\ud800"],
    ["a C1 control", "synthetic\u0085"],
  ])("refuses %s", async (_label, value) => {
    const response = await put("skipo", { fields: { api_key: value } });
    expect(response.status).toBe(400);
    expect(state.upserts).toHaveLength(0);
  });

  it("refuses a chunked body past the cap without buffering it all", async () => {
    let pulled = 0;
    const chunk = new TextEncoder().encode("x".repeat(4096));
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        pulled += 1;
        if (pulled > 100) controller.close();
        else controller.enqueue(chunk);
      },
    });
    const response = await PUT(
      new NextRequest("http://localhost:3000/api/credentials/skipo", {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: stream,
        duplex: "half",
      } as ConstructorParameters<typeof NextRequest>[1]),
      { params: Promise.resolve({ slug: "skipo" }) }
    );
    expect(response.status).toBe(413);
    expect(pulled).toBeLessThan(10);
  });

  it("refuses to seal when the service holds another key or none", async () => {
    for (const control of [
      { key_available: true, key_id: "0".repeat(64), fields: {} },
      { key_available: false, key_id: null, fields: {} },
    ]) {
      state.control = control;
      const response = await put("banchile", { fields: { password: PASSWORD } });
      expect(response.status).toBe(503);
    }
    expect(state.upserts).toHaveLength(0);
  });

  it("refuses a published key whose id does not match it", async () => {
    state.key = { ...KEY, keyId: "f".repeat(64) };
    const response = await put("banchile", { fields: { password: PASSWORD } });
    expect(response.status).toBe(503);
    expect(state.upserts).toHaveLength(0);
  });
});

describe("PUT /api/credentials/email (bound IMAP password)", () => {
  const HOST = "imap.gmail.com";
  const USER = "user@example.com";

  it.each([
    [{ imap_host: "imap.mail.me.com" }],
    [{ imap_user: "other@example.com" }],
    [{ imap_password: PASSWORD }],
    [{ imap_host: HOST, imap_password: PASSWORD }],
  ])("refuses a partial group %j", async (fields) => {
    const response = await put("email", { fields });
    expect(response.status).toBe(400);
    expect((await response.json()).error).toContain("se guardan juntos");
    expect(state.upserts).toHaveLength(0);
  });

  it("seals the password to the host and user saved with it", async () => {
    const response = await put("email", {
      fields: { imap_host: HOST, imap_user: USER, imap_password: PASSWORD },
    });
    expect(response.status).toBe(200);
    const sealed = state.upserts[0].fields.find((f) => f.fieldKey === "imap_password")!.sealed;
    const open = (binding?: string) =>
      suite.open(
        {
          recipientKey: pair.privateKey,
          enc: sealed.slice(0, 32),
          info: sealingInfo("email", "imap_password", KEY.keyId, binding),
        },
        sealed.slice(32)
      );
    expect(new TextDecoder().decode(await open(await bindingDigest([HOST, USER])))).toBe(PASSWORD);
    await expect(open(await bindingDigest(["imap.example.com", USER]))).rejects.toThrow();
    await expect(open()).rejects.toThrow();
  });

  it("refuses an unknown IMAP host unless custom hosts are allowed", async () => {
    const fields = { imap_host: "imap.example.com", imap_user: USER, imap_password: PASSWORD };
    expect((await put("email", { fields })).status).toBe(400);
    process.env.EMAIL_IMAP_ALLOW_CUSTOM_HOST = "1";
    expect((await put("email", { fields })).status).toBe(200);
  });

  it("deletes the whole group when one of its fields is deleted", async () => {
    await del("email", "imap_host");
    expect(state.deletes).toEqual([
      { slug: "email", field: "imap_password,imap_host,imap_user" },
    ]);
  });
});

describe("DELETE /api/credentials/[slug]", () => {
  it("deletes one field and tells the service", async () => {
    state.reload = { applied: true, added: [], scrapers: [] };
    const response = await del("banchile", "password");
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ deleted: 1, applied: true });
    expect(state.deletes).toEqual([{ slug: "banchile", field: "password" }]);
  });

  it("deletes every field of an institution", async () => {
    await del("buda");
    expect(state.deletes).toEqual([{ slug: "buda", field: undefined }]);
  });

  it("refuses an unknown field or institution", async () => {
    expect((await del("banchile", "pin")).status).toBe(400);
    expect((await del("nope")).status).toBe(404);
    expect(state.deletes).toHaveLength(0);
  });
});

describe("GET /api/credentials", () => {
  const updatedAt = new Date("2026-01-01T00:00:00Z");

  it("merges stored rows with the service's fallback booleans", async () => {
    state.meta = [
      { institutionSlug: "banchile", fieldKey: "password", keyId: KEY.keyId, scheme: CREDENTIAL_SCHEME, updatedAt },
      { institutionSlug: "buda", fieldKey: "api_key", keyId: "0".repeat(64), scheme: CREDENTIAL_SCHEME, updatedAt },
    ];
    state.control = {
      key_available: true,
      key_id: KEY.keyId,
      fields: {
        BANCHILE_RUT: { fallback: true, stored: false, needs_reentry: false },
        BANCHILE_PASSWORD: { fallback: true, stored: true, needs_reentry: false },
      },
    };
    state.enabled = ["banchile"];

    const response = await GET();
    expect(response.headers.get("cache-control")).toBe("no-store");
    const body = await response.json();
    expect(body.serviceReachable).toBe(true);
    expect(body.keyPublished).toBe(true);

    const banchile = body.institutions.find((i: { slug: string }) => i.slug === "banchile");
    expect(banchile.enabled).toBe(true);
    expect(banchile.fields).toEqual([
      { key: "rut", stored: false, needsReentry: false, fallback: true, updatedAt: null },
      {
        key: "password",
        stored: true,
        needsReentry: false,
        fallback: true,
        updatedAt: updatedAt.toISOString(),
      },
    ]);
    const buda = body.institutions.find((i: { slug: string }) => i.slug === "buda");
    expect(buda.fields[0]).toMatchObject({ stored: false, needsReentry: true });
    const mercadopago = body.institutions.find((i: { slug: string }) => i.slug === "mercadopago");
    expect(mercadopago.fields).toEqual([]);
  });

  it("still answers with the service down, fallback unknown, saving off", async () => {
    state.control = null;
    const body = await (await GET()).json();
    expect(body.serviceReachable).toBe(false);
    expect(body.keyAvailable).toBeNull();
    expect(body.keyMismatch).toBeNull();
    expect(body.canSave).toBe(false);
    expect(body.saveBlocked).toBe("service_unreachable");
    expect(body.institutions[0].enabled).toBeNull();
    expect(body.institutions[0].fields[0].fallback).toBeNull();
  });

  it("says saving works when the service holds the active key", async () => {
    const body = await (await GET()).json();
    expect(body.canSave).toBe(true);
    expect(body.saveBlocked).toBeNull();
    expect(body.keyMismatch).toBe(false);
  });

  it("reports a key mismatch and why saving is off", async () => {
    state.control = { key_available: true, key_id: "0".repeat(64), key_mismatch: true, fields: {} };
    const body = await (await GET()).json();
    expect(body.keyMismatch).toBe(true);
    expect(body.canSave).toBe(false);
    expect(body.saveBlocked).toBe("key_mismatch");
  });

  it("shows the active key's dates and the last rotation failure, never key material", async () => {
    state.keyInfo = {
      createdAt: new Date("2026-01-01T12:00:00Z"),
      lastRotatedAt: new Date("2026-01-01T12:00:00Z"),
      rotationFailedAt: new Date("2026-04-02T12:00:00Z"),
      rotationError: "OperationalError",
    };
    state.control = { key_available: true, key_id: KEY.keyId, max_age_days: 90, fields: {} };
    const response = await GET();
    const raw = await text(response);
    const body = JSON.parse(raw);
    expect(body.key).toEqual({
      createdAt: "2026-01-01T12:00:00.000Z",
      lastRotatedAt: "2026-01-01T12:00:00.000Z",
      rotationFailedAt: "2026-04-02T12:00:00.000Z",
      rotationError: "OperationalError",
      maxAgeDays: 90,
    });
    expect(raw).not.toContain(KEY.keyId);
  });
});

describe("proxy guards the credential routes", () => {
  const LOCAL = "http://localhost:3000";

  it("refuses a cross-site write", async () => {
    for (const method of ["PUT", "DELETE"]) {
      const response = await proxy(
        new NextRequest(new URL("/api/credentials/banchile", LOCAL), {
          method,
          headers: { host: "localhost:3000", origin: "https://evil.example" },
        })
      );
      expect(response.status).toBe(403);
    }
  });

  it("requires a session when a password is configured", async () => {
    const original = process.env.DASHBOARD_PASSWORD;
    process.env.DASHBOARD_PASSWORD = "changeme-example";
    try {
      const response = await proxy(
        new NextRequest(new URL("/api/credentials", "https://dashboard.example"), {
          headers: { host: "dashboard.example" },
        })
      );
      expect(response.status).toBe(401);
    } finally {
      if (original === undefined) delete process.env.DASHBOARD_PASSWORD;
      else process.env.DASHBOARD_PASSWORD = original;
    }
  });
});
