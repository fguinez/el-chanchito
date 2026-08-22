import { NextRequest } from "next/server";
import {
  getRedirectUrl,
  getRewrittenUrl,
  isRewrite,
  unstable_doesMiddlewareMatch,
} from "next/experimental/testing/server";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { config, proxy } from "@/proxy";
import {
  SECURE_SESSION_COOKIE_NAME,
  SESSION_COOKIE_NAME,
} from "@/lib/auth/config";
import { createSessionToken } from "@/lib/auth/session";

// The proxy is the single enforcement point, so it gets tested by direct
// invocation as the Next docs recommend for proxy files. Note that this Next
// build still exports the match helper under its pre-rename name
// (`unstable_doesMiddlewareMatch`); `unstable_doesProxyMatch` does not exist yet.

// Obviously fake shared secret; never use a real one in tests.
const PASSWORD = "changeme-example";

const ORIGIN = "https://dashboard.example";

interface RequestInit {
  path?: string;
  method?: string;
  cookie?: string;
  headers?: Record<string, string>;
  origin?: string;
}

function makeRequest({
  path = "/",
  method = "GET",
  cookie,
  headers = {},
  origin = ORIGIN,
}: RequestInit = {}): NextRequest {
  const url = new URL(path, origin);
  const merged: Record<string, string> = {
    host: url.host,
    ...headers,
  };
  if (cookie) merged.cookie = cookie;
  return new NextRequest(url, { method, headers: merged });
}

// Requests in this suite arrive over HTTPS (see ORIGIN), where a real browser
// carries the host-pinned name, so that is the default here too.
async function validSessionCookie(
  name = SECURE_SESSION_COOKIE_NAME
): Promise<string> {
  const token = await createSessionToken(PASSWORD, 3600);
  return `${name}=${token}`;
}

/** Enforced mode: a password is configured. */
function enforce(): void {
  process.env.DASHBOARD_PASSWORD = PASSWORD;
}

/** Declares a reverse proxy in front, so X-Forwarded-* may be believed. */
function trustProxy(): void {
  process.env.DASHBOARD_TRUST_PROXY = "1";
}

const originalPassword = process.env.DASHBOARD_PASSWORD;
const originalTrustProxy = process.env.DASHBOARD_TRUST_PROXY;
const originalNodeEnv = process.env.NODE_ENV;

beforeEach(() => {
  delete process.env.DASHBOARD_PASSWORD;
  delete process.env.DASHBOARD_TRUST_PROXY;
});

afterEach(() => {
  if (originalPassword === undefined) delete process.env.DASHBOARD_PASSWORD;
  else process.env.DASHBOARD_PASSWORD = originalPassword;
  if (originalTrustProxy === undefined) delete process.env.DASHBOARD_TRUST_PROXY;
  else process.env.DASHBOARD_TRUST_PROXY = originalTrustProxy;
  // NODE_ENV is read-only in the Next types but writable at runtime.
  (process.env as Record<string, string | undefined>).NODE_ENV = originalNodeEnv;
});

describe("proxy: disabled mode", () => {
  it("passes everything through when no password is configured outside production", async () => {
    const response = await proxy(makeRequest({ path: "/history" }));
    expect(response.status).toBe(200);
    expect(isRewrite(response)).toBe(false);
    expect(response.headers.get("cache-control")).toBeNull();
  });

  it("leaves mutating API calls alone, cross-site header and all", async () => {
    const response = await proxy(
      makeRequest({
        path: "/api/month-reset",
        method: "POST",
        headers: { "sec-fetch-site": "cross-site" },
      })
    );
    expect(response.status).toBe(200);
  });
});

describe("proxy: misconfigured mode", () => {
  beforeEach(() => {
    (process.env as Record<string, string | undefined>).NODE_ENV = "production";
  });

  it("rewrites pages to the setup notice", async () => {
    const response = await proxy(makeRequest({ path: "/history" }));
    expect(isRewrite(response)).toBe(true);
    expect(getRewrittenUrl(response)).toBe(`${ORIGIN}/setup-required`);
  });

  it("answers 503 on every API route", async () => {
    const response = await proxy(makeRequest({ path: "/api/balances" }));
    expect(response.status).toBe(503);
    expect(response.headers.get("cache-control")).toBe("no-store");
  });
});

describe("proxy: enforced mode without a session", () => {
  beforeEach(enforce);

  it("redirects a page to the login, remembering where to come back to", async () => {
    const response = await proxy(makeRequest({ path: "/history?range=30d" }));
    expect(response.status).toBe(307);
    expect(getRedirectUrl(response)).toBe(
      `${ORIGIN}/login?next=%2Fhistory%3Frange%3D30d`
    );
  });

  it("omits the next parameter for the dashboard root", async () => {
    const response = await proxy(makeRequest({ path: "/" }));
    expect(getRedirectUrl(response)).toBe(`${ORIGIN}/login`);
  });

  it("marks the login redirect no-store", async () => {
    const response = await proxy(makeRequest({ path: "/history" }));
    expect(response.headers.get("cache-control")).toBe("no-store");
  });

  it("answers 401 with no-store on API routes", async () => {
    const response = await proxy(makeRequest({ path: "/api/balances" }));
    expect(response.status).toBe(401);
    expect(response.headers.get("cache-control")).toBe("no-store");
  });

  it("keeps the login surfaces reachable", async () => {
    for (const path of [
      "/login",
      "/api/auth/login",
      "/api/auth/session",
      "/api/auth/logout",
    ]) {
      const method = path.startsWith("/api/auth/") && path !== "/api/auth/session"
        ? "POST"
        : "GET";
      const response = await proxy(makeRequest({ path, method }));
      expect(response.status, path).toBe(200);
    }
  });

  it("rejects an invalid token exactly like a missing one", async () => {
    const response = await proxy(
      makeRequest({
        path: "/api/balances",
        cookie: `${SECURE_SESSION_COOKIE_NAME}=1799999999999.deadbeef`,
      })
    );
    expect(response.status).toBe(401);
  });
});

describe("proxy: enforced mode with a session", () => {
  beforeEach(enforce);

  it("serves a protected page as private and uncacheable", async () => {
    const response = await proxy(
      makeRequest({ path: "/history", cookie: await validSessionCookie() })
    );
    expect(response.status).toBe(200);
    expect(isRewrite(response)).toBe(false);
    expect(response.headers.get("cache-control")).toBe("private, no-store");
  });

  it("accepts the plain cookie name over plain HTTP", async () => {
    // The only channel where browsers reject the __Host- prefix.
    const response = await proxy(
      makeRequest({
        path: "/history",
        origin: "http://localhost:3000",
        cookie: await validSessionCookie(SESSION_COOKIE_NAME),
      })
    );
    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toBe("private, no-store");
  });

  it("refuses the plain cookie name over HTTPS", async () => {
    // Nuisance only (the name cannot be forged into a valid token), but a
    // sibling subdomain has no business being read on a secure request.
    const response = await proxy(
      makeRequest({
        path: "/history",
        cookie: await validSessionCookie(SESSION_COOKIE_NAME),
      })
    );
    expect(response.status).toBe(307);
  });

  it("prefers the host-pinned cookie over a tossed plain one", async () => {
    const good = await createSessionToken(PASSWORD, 3600);
    const response = await proxy(
      makeRequest({
        path: "/history",
        cookie: `${SESSION_COOKIE_NAME}=garbage; ${SECURE_SESSION_COOKIE_NAME}=${good}`,
      })
    );
    expect(response.status).toBe(200);
  });

  it("sends an authenticated visitor away from the login page", async () => {
    const response = await proxy(
      makeRequest({ path: "/login", cookie: await validSessionCookie() })
    );
    expect(response.status).toBe(307);
    expect(getRedirectUrl(response)).toBe(`${ORIGIN}/`);
    expect(response.headers.get("cache-control")).toBe("no-store");
  });

  it("does not add page cache headers to API responses", async () => {
    const response = await proxy(
      makeRequest({ path: "/api/balances", cookie: await validSessionCookie() })
    );
    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toBeNull();
  });
});

describe("proxy: cross-site write rejection", () => {
  beforeEach(enforce);

  async function postWith(headers: Record<string, string>) {
    return proxy(
      makeRequest({
        path: "/api/month-reset",
        method: "POST",
        cookie: await validSessionCookie(),
        headers,
      })
    );
  }

  it("allows only same-origin, none, or an absent Sec-Fetch-Site", async () => {
    const cases: [Record<string, string>, number][] = [
      [{ "sec-fetch-site": "same-origin" }, 200],
      [{ "sec-fetch-site": "none" }, 200],
      [{ "sec-fetch-site": "None" }, 200],
      [{}, 200],
      // SameSite=Lax does not stop these: a sibling subdomain or any host under
      // a shared public suffix is "same-site" to the browser.
      [{ "sec-fetch-site": "same-site" }, 403],
      [{ "sec-fetch-site": "Same-Site" }, 403],
      [{ "sec-fetch-site": "cross-site" }, 403],
      [{ "sec-fetch-site": "Cross-Site" }, 403],
      [{ "sec-fetch-site": "CROSS-SITE" }, 403],
      [{ "sec-fetch-site": "  cross-site  " }, 403],
      // Unknown values are refused rather than waved through.
      [{ "sec-fetch-site": "whatever" }, 403],
    ];

    for (const [headers, status] of cases) {
      const response = await postWith(headers);
      expect(response.status, JSON.stringify(headers)).toBe(status);
    }
  });

  it("accepts our own Origin and refuses anyone else's", async () => {
    expect((await postWith({ origin: ORIGIN })).status).toBe(200);
    expect((await postWith({ origin: "https://evil.example" })).status).toBe(403);
    // A same-site sibling subdomain is still a different origin.
    expect((await postWith({ origin: "https://other.example" })).status).toBe(403);
    // An opaque origin cannot be matched against anything.
    expect((await postWith({ origin: "null" })).status).toBe(403);
  });

  it("does not reject a legitimate request behind a trusted TLS-terminating proxy", async () => {
    trustProxy();
    // The internal request arrives over plain HTTP on an internal host while the
    // browser sends the external HTTPS origin. Comparing Origin against
    // nextUrl.origin would 403 every write in this very ordinary deployment.
    const request = new NextRequest(new URL("http://127.0.0.1:3000/api/month-reset"), {
      method: "POST",
      headers: {
        host: "127.0.0.1:3000",
        "x-forwarded-host": "dashboard.example",
        "x-forwarded-proto": "https",
        origin: "https://dashboard.example",
        "sec-fetch-site": "same-origin",
        cookie: await validSessionCookie(),
      },
    });
    expect((await proxy(request)).status).toBe(200);
  });

  it("still rejects a forged Origin behind that same proxy", async () => {
    trustProxy();
    const request = new NextRequest(new URL("http://127.0.0.1:3000/api/month-reset"), {
      method: "POST",
      headers: {
        host: "127.0.0.1:3000",
        "x-forwarded-host": "dashboard.example",
        "x-forwarded-proto": "https",
        origin: "https://evil.example",
        cookie: await validSessionCookie(),
      },
    });
    expect((await proxy(request)).status).toBe(403);
  });

  it("rejects a forged forwarded-host and Origin pair unless a proxy is trusted", async () => {
    // Both headers are the caller's to write. With DASHBOARD_TRUST_PROXY unset
    // the Origin check compares against Host, which the forged pair misses.
    const forged = async () =>
      new NextRequest(new URL("http://127.0.0.1:3000/api/month-reset"), {
        method: "POST",
        headers: {
          host: "127.0.0.1:3000",
          "x-forwarded-host": "evil.example",
          origin: "https://evil.example",
          cookie: await validSessionCookie(SESSION_COOKIE_NAME),
        },
      });

    expect((await proxy(await forged())).status).toBe(403);

    // With a proxy declared the forwarded host is believed again, which is the
    // whole point of the opt-in: only the operator can turn it on.
    trustProxy();
    expect((await proxy(await forged())).status).toBe(200);
  });

  it("leaves safe methods alone", async () => {
    for (const method of ["GET", "HEAD"]) {
      const response = await proxy(
        makeRequest({
          path: "/api/balances",
          method,
          cookie: await validSessionCookie(),
          headers: { "sec-fetch-site": "cross-site" },
        })
      );
      expect(response.status, method).toBe(200);
    }
  });

  it("guards the public logout endpoint as well", async () => {
    const response = await proxy(
      makeRequest({
        path: "/api/auth/logout",
        method: "POST",
        headers: { "sec-fetch-site": "cross-site" },
      })
    );
    expect(response.status).toBe(403);
  });

  it("checks writes before authentication, so a page is never leaked", async () => {
    const response = await proxy(
      makeRequest({
        path: "/api/month-reset",
        method: "POST",
        headers: { "sec-fetch-site": "cross-site" },
      })
    );
    expect(response.status).toBe(403);
  });
});

describe("proxy matcher", () => {
  const nextConfig = {};

  it("runs for pages and API routes", () => {
    for (const url of ["/", "/history", "/monitors", "/api/balances", "/login"]) {
      expect(
        unstable_doesMiddlewareMatch({ config, nextConfig, url }),
        url
      ).toBe(true);
    }
  });

  it("skips Next's static output and the favicon", () => {
    for (const url of [
      "/_next/static/chunks/main.js",
      "/_next/image",
      "/favicon.ico",
    ]) {
      expect(
        unstable_doesMiddlewareMatch({ config, nextConfig, url }),
        url
      ).toBe(false);
    }
  });

  it("does not treat the escaped dot as a wildcard", () => {
    // With an unescaped `.` this path would slip past the matcher entirely.
    expect(
      unstable_doesMiddlewareMatch({ config, nextConfig, url: "/faviconxico" })
    ).toBe(true);
  });
});
