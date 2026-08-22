import { describe, expect, it } from "vitest";

import {
  SECURE_SESSION_COOKIE_NAME,
  SESSION_COOKIE_NAME,
  readSessionCookie,
  sessionCookieName,
} from "@/lib/auth/config";
import {
  clearedSessionCookies,
  isSecureRequest,
  sessionCookieOptions,
} from "@/lib/auth/cookie";

function request(headers: Record<string, string>, url = "http://localhost:3000/") {
  return new Request(url, { headers });
}

describe("isSecureRequest", () => {
  it("trusts the first x-forwarded-proto entry", () => {
    const cases: [string, boolean][] = [
      ["https", true],
      ["http", false],
      // A proxy chain appends; only the closest hop to the client counts.
      ["https,http", true],
      ["http,https", false],
      ["https, http", true],
      ["HTTPS", true],
      ["  https  ", true],
      ["httpsx", false],
      ["", false],
    ];
    for (const [value, expected] of cases) {
      expect(isSecureRequest(request({ "x-forwarded-proto": value })), value).toBe(
        expected
      );
    }
  });

  it("falls back to the request URL when the header is absent", () => {
    expect(isSecureRequest(request({}, "http://localhost:3000/"))).toBe(false);
    expect(isSecureRequest(request({}, "https://dashboard.example/"))).toBe(true);
  });
});

describe("sessionCookieOptions", () => {
  it("hardens every cookie it writes", () => {
    const options = sessionCookieOptions({ kind: "duration", seconds: 1800 }, true);
    expect(options).toEqual({
      name: SECURE_SESSION_COOKIE_NAME,
      httpOnly: true,
      sameSite: "lax",
      path: "/",
      secure: true,
      maxAge: 1800,
    });
  });

  it("omits Max-Age entirely for a browser session", () => {
    const options = sessionCookieOptions({ kind: "browser" }, true);
    expect(options).not.toHaveProperty("maxAge");
    expect(options.name).toBe(SECURE_SESSION_COOKIE_NAME);
  });

  it("carries Max-Age for a duration policy", () => {
    const options = sessionCookieOptions({ kind: "duration", seconds: 60 }, false);
    expect(options.maxAge).toBe(60);
  });

  it("uses the __Host- name only where browsers would accept it", () => {
    // The prefix requires Secure, Path=/ and no Domain; over plain HTTP the
    // browser would drop the cookie outright.
    expect(sessionCookieOptions({ kind: "browser" }, false).name).toBe(
      SESSION_COOKIE_NAME
    );
    expect(sessionCookieName(true)).toBe("__Host-chanchito_session");
    expect(sessionCookieName(false)).toBe("chanchito_session");

    const secure = sessionCookieOptions({ kind: "browser" }, true);
    expect(secure.secure).toBe(true);
    expect(secure.path).toBe("/");
    expect(secure).not.toHaveProperty("domain");
  });
});

describe("readSessionCookie", () => {
  it("prefers the host-pinned cookie over a plain one", () => {
    const jar: Record<string, string> = {
      [SESSION_COOKIE_NAME]: "plain",
      [SECURE_SESSION_COOKIE_NAME]: "pinned",
    };
    expect(readSessionCookie((name) => jar[name])).toBe("pinned");
  });

  it("accepts whichever name is present", () => {
    expect(readSessionCookie((name) => (name === SESSION_COOKIE_NAME ? "a" : undefined))).toBe("a");
    expect(
      readSessionCookie((name) =>
        name === SECURE_SESSION_COOKIE_NAME ? "b" : undefined
      )
    ).toBe("b");
    expect(readSessionCookie(() => undefined)).toBeUndefined();
  });
});

describe("clearedSessionCookies", () => {
  it("clears both names with attributes browsers honor for deletion", () => {
    for (const secure of [true, false]) {
      const cleared = clearedSessionCookies(secure);
      expect(cleared.map((c) => c.name)).toEqual([
        SESSION_COOKIE_NAME,
        SECURE_SESSION_COOKIE_NAME,
      ]);
      for (const cookie of cleared) {
        expect(cookie.value).toBe("");
        expect(cookie.maxAge).toBe(0);
        expect(cookie.path).toBe("/");
        expect(cookie.httpOnly).toBe(true);
      }
      // A __Host- cookie can only be deleted with Secure set, which is also the
      // only way it could have been created.
      expect(cleared[1].secure).toBe(true);
      expect(cleared[0].secure).toBe(secure);
    }
  });
});
