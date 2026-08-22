import { describe, expect, it } from "vitest";

import {
  isAllowedFetchSite,
  isAllowedOrigin,
  isMutatingMethod,
  requestHost,
} from "@/lib/auth/csrf";

function headers(values: Record<string, string>): Headers {
  return new Headers(values);
}

describe("isMutatingMethod", () => {
  it("treats only GET and HEAD as safe", () => {
    for (const method of ["GET", "HEAD", "get", "head"]) {
      expect(isMutatingMethod(method), method).toBe(false);
    }
    for (const method of ["POST", "PUT", "PATCH", "DELETE", "post"]) {
      expect(isMutatingMethod(method), method).toBe(true);
    }
  });
});

describe("isAllowedFetchSite", () => {
  it("allows same-origin, none, and an absent header", () => {
    for (const value of ["same-origin", "none", "Same-Origin", "NONE", "  none "]) {
      expect(isAllowedFetchSite(value), value).toBe(true);
    }
    expect(isAllowedFetchSite(null)).toBe(true);
    expect(isAllowedFetchSite(undefined)).toBe(true);
    expect(isAllowedFetchSite("")).toBe(true);
  });

  it("rejects same-site and cross-site in any casing", () => {
    for (const value of [
      "cross-site",
      "Cross-Site",
      "CROSS-SITE",
      "same-site",
      "Same-Site",
      " cross-site ",
    ]) {
      expect(isAllowedFetchSite(value), value).toBe(false);
    }
  });

  it("rejects unknown values rather than waving them through", () => {
    for (const value of ["whatever", "same_origin", "cross site", "0"]) {
      expect(isAllowedFetchSite(value), value).toBe(false);
    }
  });
});

describe("requestHost", () => {
  it("prefers the forwarded host and lowercases it when a proxy is trusted", () => {
    expect(requestHost(headers({ host: "internal:3000" }), true)).toBe(
      "internal:3000"
    );
    expect(
      requestHost(
        headers({ host: "internal:3000", "x-forwarded-host": "Dashboard.Example" }),
        true
      )
    ).toBe("dashboard.example");
  });

  it("ignores the forwarded host entirely when no proxy is trusted", () => {
    // The header is client-writable, so with trust off only Host counts.
    expect(
      requestHost(
        headers({ host: "internal:3000", "x-forwarded-host": "dashboard.example" }),
        false
      )
    ).toBe("internal:3000");
  });

  it("takes the first entry of a forwarded chain", () => {
    expect(
      requestHost(headers({ "x-forwarded-host": "dashboard.example, internal" }), true)
    ).toBe("dashboard.example");
  });

  it("is undefined when neither header is present", () => {
    expect(requestHost(headers({}), true)).toBeUndefined();
    expect(requestHost(headers({}), false)).toBeUndefined();
  });
});

describe("isAllowedOrigin", () => {
  it("allows an absent Origin (non-browser clients never send one)", () => {
    expect(isAllowedOrigin(null, headers({ host: "dashboard.example" }), false)).toBe(true);
    expect(isAllowedOrigin(undefined, headers({ host: "dashboard.example" }), false)).toBe(
      true
    );
  });

  it("matches the request's own host", () => {
    const h = headers({ host: "dashboard.example" });
    expect(isAllowedOrigin("https://dashboard.example", h, false)).toBe(true);
    expect(isAllowedOrigin("http://dashboard.example", h, false)).toBe(true);
    expect(isAllowedOrigin("https://DASHBOARD.example", h, false)).toBe(true);
  });

  it("rejects any other host, sibling subdomains included", () => {
    const h = headers({ host: "dashboard.example" });
    for (const origin of [
      "https://evil.example",
      "https://other.dashboard.example",
      "https://dashboard.example.evil.test",
      "https://dashboard.example:8443",
    ]) {
      expect(isAllowedOrigin(origin, h, false), origin).toBe(false);
    }
  });

  it("rejects an opaque or unparseable Origin", () => {
    const h = headers({ host: "dashboard.example" });
    for (const origin of ["null", "not a url", "://", "https://"]) {
      expect(isAllowedOrigin(origin, h, false), origin).toBe(false);
    }
  });

  it("compares against the forwarded host behind a TLS-terminating proxy", () => {
    // The scheme is deliberately not compared: the internal hop is plain HTTP
    // while the browser sends the external HTTPS origin.
    const h = headers({
      host: "127.0.0.1:3000",
      "x-forwarded-host": "dashboard.example",
      "x-forwarded-proto": "https",
    });
    expect(isAllowedOrigin("https://dashboard.example", h, true)).toBe(true);
    expect(isAllowedOrigin("https://evil.example", h, true)).toBe(false);
    // The internal host is no longer accepted once a forwarded host is declared.
    expect(isAllowedOrigin("http://127.0.0.1:3000", h, true)).toBe(false);
  });

  it("rejects a forged forwarded-host and Origin pair when no proxy is trusted", () => {
    // Both headers are the client's to write, so a matching pair proves nothing:
    // with trust off the comparison falls back to Host, which does not match.
    const h = headers({
      host: "dashboard.example",
      "x-forwarded-host": "evil.example",
    });
    expect(isAllowedOrigin("https://evil.example", h, false)).toBe(false);
    expect(isAllowedOrigin("https://evil.example", h, true)).toBe(true);
  });

  it("rejects everything when no host header is available to compare against", () => {
    expect(isAllowedOrigin("https://dashboard.example", headers({}), false)).toBe(false);
  });
});
