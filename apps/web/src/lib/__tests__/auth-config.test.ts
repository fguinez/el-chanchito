import { describe, it, expect } from "vitest";
import {
  BROWSER_TOKEN_CAP_SECONDS,
  DEFAULT_SESSION_MAX_AGE_SECONDS,
  UNLIMITED_SESSION_SECONDS,
  cookieMaxAgeSeconds,
  decideAuthMode,
  isApiPath,
  isPublicPath,
  parseSessionMaxAge,
  safeNextPath,
  tokenLifetimeSeconds,
} from "@/lib/auth/config";

// These are the pure decisions behind the whole security posture: the proxy and
// the auth route handlers do nothing but apply them.

describe("parseSessionMaxAge", () => {
  it("defaults to 12h when unset or empty", () => {
    for (const raw of [undefined, null, "", "   "]) {
      const { policy, warning } = parseSessionMaxAge(raw);
      expect(policy).toEqual({
        kind: "duration",
        seconds: DEFAULT_SESSION_MAX_AGE_SECONDS,
      });
      expect(warning).toBeUndefined();
    }
  });

  it("parses duration strings", () => {
    const cases: [string, number][] = [
      ["30m", 30 * 60],
      ["12h", 12 * 60 * 60],
      ["7d", 7 * 24 * 60 * 60],
      ["30d", 30 * 24 * 60 * 60],
      ["45s", 45],
      ["3600", 3600],
      ["  12H  ", 12 * 60 * 60],
    ];
    for (const [raw, seconds] of cases) {
      expect(parseSessionMaxAge(raw).policy).toEqual({ kind: "duration", seconds });
    }
  });

  it("parses the named modes", () => {
    expect(parseSessionMaxAge("browser").policy).toEqual({ kind: "browser" });
    expect(parseSessionMaxAge("UNLIMITED").policy).toEqual({ kind: "unlimited" });
  });

  it("fails closed to the default on invalid input, with a warning", () => {
    for (const raw of ["forever", "12x", "-5", "0", "1.5h", "12 h", "h"]) {
      const { policy, warning } = parseSessionMaxAge(raw);
      expect(policy).toEqual({
        kind: "duration",
        seconds: DEFAULT_SESSION_MAX_AGE_SECONDS,
      });
      expect(warning).toBeTruthy();
    }
  });

  it("clamps absurd durations to the unlimited ceiling", () => {
    // A Max-Age past the browser ceiling is dropped, which would silently mean
    // "no cookie at all" instead of "a very long session".
    for (const raw of ["99999999999999999999d", "500d", "9999999999"]) {
      expect(parseSessionMaxAge(raw).policy, raw).toEqual({
        kind: "duration",
        seconds: UNLIMITED_SESSION_SECONDS,
      });
    }
  });

  it("leaves durations under the ceiling untouched", () => {
    expect(parseSessionMaxAge("399d").policy).toEqual({
      kind: "duration",
      seconds: 399 * 24 * 60 * 60,
    });
  });
});

describe("token lifetime and cookie max age", () => {
  it("mirrors the duration on both sides", () => {
    const policy = { kind: "duration", seconds: 1800 } as const;
    expect(tokenLifetimeSeconds(policy)).toBe(1800);
    expect(cookieMaxAgeSeconds(policy)).toBe(1800);
  });

  it("caps browser sessions in the token but omits Max-Age", () => {
    const policy = { kind: "browser" } as const;
    expect(tokenLifetimeSeconds(policy)).toBe(BROWSER_TOKEN_CAP_SECONDS);
    expect(cookieMaxAgeSeconds(policy)).toBeUndefined();
  });

  it("uses the browser cap for unlimited sessions", () => {
    const policy = { kind: "unlimited" } as const;
    expect(tokenLifetimeSeconds(policy)).toBe(UNLIMITED_SESSION_SECONDS);
    expect(cookieMaxAgeSeconds(policy)).toBe(UNLIMITED_SESSION_SECONDS);
  });
});

describe("decideAuthMode", () => {
  it("enforces whenever a password is configured", () => {
    expect(
      decideAuthMode({ password: "changeme-example", nodeEnv: "development" })
    ).toBe("enforced");
    expect(
      decideAuthMode({ password: "changeme-example", nodeEnv: "production" })
    ).toBe("enforced");
  });

  it("stays transparent in development without a password", () => {
    expect(decideAuthMode({ nodeEnv: "development" })).toBe("disabled");
    expect(decideAuthMode({ password: "", nodeEnv: "test" })).toBe("disabled");
    expect(decideAuthMode({ password: null, nodeEnv: undefined })).toBe("disabled");
  });

  it("fails closed in production without a password", () => {
    expect(decideAuthMode({ nodeEnv: "production" })).toBe("misconfigured");
    expect(decideAuthMode({ password: "", nodeEnv: "production" })).toBe(
      "misconfigured"
    );
  });

  it("counts a whitespace-only password as unset", () => {
    // Otherwise a botched secret round trip enforces auth with a one-space
    // password, which is worse than failing closed.
    for (const password of [" ", "   ", "\n", "\t\n "]) {
      expect(decideAuthMode({ password, nodeEnv: "production" })).toBe(
        "misconfigured"
      );
      expect(decideAuthMode({ password, nodeEnv: "development" })).toBe("disabled");
    }
  });

  it("still enforces a password that merely has surrounding whitespace", () => {
    expect(
      decideAuthMode({ password: " changeme-example\n", nodeEnv: "production" })
    ).toBe("enforced");
  });
});

describe("path classification", () => {
  it("recognizes API paths", () => {
    expect(isApiPath("/api")).toBe(true);
    expect(isApiPath("/api/balances")).toBe(true);
    expect(isApiPath("/apiary")).toBe(false);
    expect(isApiPath("/history")).toBe(false);
  });

  it("keeps only the login surfaces public", () => {
    expect(isPublicPath("/login")).toBe(true);
    expect(isPublicPath("/api/auth/login")).toBe(true);
    expect(isPublicPath("/api/auth/session")).toBe(true);
    expect(isPublicPath("/favicon.ico")).toBe(true);

    expect(isPublicPath("/")).toBe(false);
    expect(isPublicPath("/api/balances")).toBe(false);
    expect(isPublicPath("/login/extra")).toBe(false);
  });

  it("keeps logout public so a stale cookie can always be cleared", () => {
    // Gating it means an expired token 401s on "Cerrar sesión" and the dead
    // cookie is stuck in the browser forever.
    expect(isPublicPath("/api/auth/logout")).toBe(true);
  });
});

describe("safeNextPath", () => {
  it("keeps same-origin relative paths", () => {
    expect(safeNextPath("/history")).toBe("/history");
    expect(safeNextPath("/monitors?range=30d")).toBe("/monitors?range=30d");
  });

  it("rejects anything that could leave the origin", () => {
    for (const raw of [
      undefined,
      null,
      "",
      "history",
      "//evil.example",
      "/\\evil.example",
      "https://evil.example",
      "http://evil.example/x",
      "/ok\\..\\evil",
      "/bad\nheader",
    ]) {
      expect(safeNextPath(raw)).toBe("/");
    }
  });

  // These all stay same-origin, but only because the browser resolves them
  // against our own origin rather than decoding them into an authority. Pinned
  // so a future "helpful" decode step cannot quietly open a redirect.
  it("keeps percent-encoded slashes as an opaque path, never an authority", () => {
    for (const raw of [
      "/%2f%2fevil.example",
      "/%2F%2Fevil.example",
      "/%5c%5cevil.example",
      "/%5C%5Cevil.example",
      "/%09//evil.example",
      "/%2e%2e//evil.example",
    ]) {
      // Returned unchanged: still a single-slash-prefixed relative path.
      expect(safeNextPath(raw), raw).toBe(raw);
      expect(new URL(raw, "https://dashboard.example").host).toBe(
        "dashboard.example"
      );
    }
  });

  it("keeps Unicode look-alikes and dot segments same-origin", () => {
    for (const raw of [
      "/\u2028//evil.example", // line separator
      "/\u00a0//evil.example", // no-break space
      "/\u2044\u2044evil.example", // fraction slash
      "/..//evil.example",
      "/@evil.example",
    ]) {
      expect(safeNextPath(raw), raw).toBe(raw);
      expect(new URL(raw, "https://dashboard.example").host).toBe(
        "dashboard.example"
      );
    }
  });
});
