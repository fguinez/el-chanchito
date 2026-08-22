import { describe, it, expect } from "vitest";
import {
  createSessionToken,
  timingSafeEqual,
  verifyPassword,
  verifySessionToken,
} from "@/lib/auth/session";

// Obviously fake shared secrets; never use a real one in tests.
const PASSWORD = "changeme-example";
const OTHER_PASSWORD = "changeme-example-2";

describe("session tokens", () => {
  it("round-trips a freshly minted token", async () => {
    const now = 1_700_000_000_000;
    const token = await createSessionToken(PASSWORD, 3600, now);
    expect(token).toMatch(/^\d+\.[0-9a-f]{64}$/);
    expect(await verifySessionToken(token, PASSWORD, now + 1000)).toBe(true);
  });

  it("rejects a tampered signature", async () => {
    const now = 1_700_000_000_000;
    const token = await createSessionToken(PASSWORD, 3600, now);
    const [payload, signature] = token.split(".");

    const flipped = signature.startsWith("a")
      ? `b${signature.slice(1)}`
      : `a${signature.slice(1)}`;

    expect(await verifySessionToken(`${payload}.${flipped}`, PASSWORD, now)).toBe(
      false
    );
    expect(await verifySessionToken(`${payload}.`, PASSWORD, now)).toBe(false);
    expect(await verifySessionToken(`${payload}.deadbeef`, PASSWORD, now)).toBe(
      false
    );
  });

  it("rejects a token whose expiry was extended by the client", async () => {
    const now = 1_700_000_000_000;
    const token = await createSessionToken(PASSWORD, 3600, now);
    const signature = token.split(".")[1];
    const extended = `${now + 10 * 3600 * 1000}.${signature}`;
    expect(await verifySessionToken(extended, PASSWORD, now)).toBe(false);
  });

  it("rejects an expired token", async () => {
    const now = 1_700_000_000_000;
    const token = await createSessionToken(PASSWORD, 60, now);
    expect(await verifySessionToken(token, PASSWORD, now + 59_000)).toBe(true);
    expect(await verifySessionToken(token, PASSWORD, now + 61_000)).toBe(false);
  });

  it("rejects a token signed with a different password", async () => {
    const now = 1_700_000_000_000;
    const token = await createSessionToken(PASSWORD, 3600, now);
    expect(await verifySessionToken(token, OTHER_PASSWORD, now)).toBe(false);
  });

  it("rejects malformed and missing tokens", async () => {
    const now = 1_700_000_000_000;
    for (const token of [undefined, null, "", ".", "abc", "abc.def", ".abc"]) {
      expect(await verifySessionToken(token, PASSWORD, now)).toBe(false);
    }
  });
});

describe("derived key memoization", () => {
  // The HMAC key is PBKDF2-derived and cached per password so it is not
  // recomputed on every request. The cache must never outlive its password.
  it("does not serve a stale key after a password change", async () => {
    const now = 1_700_000_000_000;

    // Warm the cache for the old password.
    const oldToken = await createSessionToken(PASSWORD, 3600, now);
    expect(await verifySessionToken(oldToken, PASSWORD, now)).toBe(true);

    // The password rotates: the old session must die and the new one must work.
    const newToken = await createSessionToken(OTHER_PASSWORD, 3600, now);
    expect(await verifySessionToken(oldToken, OTHER_PASSWORD, now)).toBe(false);
    expect(await verifySessionToken(newToken, OTHER_PASSWORD, now)).toBe(true);
    expect(await verifySessionToken(newToken, PASSWORD, now)).toBe(false);

    // And going back to the old password still derives the old key correctly.
    expect(await verifySessionToken(oldToken, PASSWORD, now)).toBe(true);
  });

  it("keeps signatures stable across repeated derivations", async () => {
    const now = 1_700_000_000_000;
    const first = await createSessionToken(PASSWORD, 3600, now);
    const second = await createSessionToken(PASSWORD, 3600, now);
    expect(second).toBe(first);
  });

  it("survives more distinct passwords than the cache holds", async () => {
    const now = 1_700_000_000_000;
    const tokens: [string, string][] = [];
    for (let i = 0; i < 12; i += 1) {
      const password = `changeme-example-${i}`;
      tokens.push([password, await createSessionToken(password, 3600, now)]);
    }
    for (const [password, token] of tokens) {
      expect(await verifySessionToken(token, password, now), password).toBe(true);
    }
  });
});

describe("verifyPassword", () => {
  it("accepts the configured password and nothing else", async () => {
    expect(await verifyPassword(PASSWORD, PASSWORD)).toBe(true);
    expect(await verifyPassword(OTHER_PASSWORD, PASSWORD)).toBe(false);
    expect(await verifyPassword("", PASSWORD)).toBe(false);
    expect(await verifyPassword(`${PASSWORD} `, PASSWORD)).toBe(false);
  });
});

describe("timingSafeEqual", () => {
  it("compares content, not identity", () => {
    expect(timingSafeEqual("abc", "abc")).toBe(true);
    expect(timingSafeEqual("abc", "abd")).toBe(false);
    expect(timingSafeEqual("abc", "abcd")).toBe(false);
    expect(timingSafeEqual("", "")).toBe(true);
  });
});
