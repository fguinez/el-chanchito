import { describe, expect, it } from "vitest";

import {
  SECURE_SESSION_COOKIE_NAME,
  SESSION_COOKIE_NAME,
  readSessionCookie,
  sessionCookieName,
} from "@/lib/auth/config";
import {
  clearedSessionCookies,
  derivesSecureFromForwardedProto,
  isSecureRequest,
  sessionCookieOptions,
} from "@/lib/auth/cookie";

function request(headers: Record<string, string>, url = "http://localhost:3000/") {
  return new Request(url, { headers });
}

describe("isSecureRequest", () => {
  it("trusts the first x-forwarded-proto entry when a proxy is trusted", () => {
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
      expect(
        isSecureRequest(request({ "x-forwarded-proto": value }), true),
        value
      ).toBe(expected);
    }
  });

  it("falls back to the request URL when the header is absent", () => {
    expect(isSecureRequest(request({}, "http://localhost:3000/"), true)).toBe(false);
    expect(isSecureRequest(request({}, "https://dashboard.example/"), true)).toBe(
      true
    );
  });

  it("ignores x-forwarded-proto entirely when no proxy is trusted", () => {
    // A client can send its own, so believing it without a proxy invents a
    // phantom https. The request URL is no help either: the Next server builds
    // its scheme from this very header, so a present-but-untrusted header means
    // "not secure" rather than "ask the URL".
    for (const url of ["http://localhost:3000/", "https://dashboard.example/"]) {
      expect(
        isSecureRequest(request({ "x-forwarded-proto": "https" }, url), false),
        url
      ).toBe(false);
    }
    // With no forwarded proto in play the URL does reflect the connection.
    expect(
      isSecureRequest(request({}, "https://dashboard.example/"), false)
    ).toBe(true);
    expect(isSecureRequest(request({}, "http://localhost:3000/"), false)).toBe(
      false
    );
  });
});

describe("derivesSecureFromForwardedProto", () => {
  it("flags a Secure flag that rests on the forwarded header alone", () => {
    // The shape behind the silent loop: nothing but the header says https, so a
    // plain-HTTP browser leg means the __Host- cookie is dropped without a word.
    const forwarded = request(
      { "x-forwarded-proto": "https" },
      "http://localhost:3000/"
    );
    expect(derivesSecureFromForwardedProto(forwarded, true)).toBe(true);
    expect(derivesSecureFromForwardedProto(forwarded, false)).toBe(false);
  });

  it("is quiet when no forwarded proto claims https", () => {
    expect(
      derivesSecureFromForwardedProto(
        request({ "x-forwarded-proto": "http" }, "http://localhost:3000/"),
        true
      )
    ).toBe(false);
    expect(
      derivesSecureFromForwardedProto(request({}, "https://dashboard.example/"), true)
    ).toBe(false);
    expect(derivesSecureFromForwardedProto(request({}), true)).toBe(false);
  });
});

describe("__Host- cookie across the proto and trust combinations", () => {
  const cases: [string, Record<string, string>, string, boolean, string][] = [
    // [label, headers, url, trustProxy, expected cookie name]
    [
      "plain HTTP, no proxy trusted",
      {},
      "http://localhost:3000/",
      false,
      SESSION_COOKIE_NAME,
    ],
    [
      "forged forwarded proto, no proxy trusted",
      { "x-forwarded-proto": "https" },
      "http://localhost:3000/",
      false,
      SESSION_COOKIE_NAME,
    ],
    [
      "TLS-terminating proxy, trusted",
      { "x-forwarded-proto": "https" },
      "http://127.0.0.1:3000/",
      true,
      SECURE_SESSION_COOKIE_NAME,
    ],
    [
      "HTTPS connection with no forwarded proto in play",
      {},
      "https://dashboard.example/",
      false,
      SECURE_SESSION_COOKIE_NAME,
    ],
    [
      "forged forwarded proto over an HTTPS URL, no proxy trusted",
      // The URL's scheme comes from the same untrusted header, so it cannot
      // rescue this case; the plain name is the safe answer.
      { "x-forwarded-proto": "https" },
      "https://dashboard.example/",
      false,
      SESSION_COOKIE_NAME,
    ],
    [
      "proxy trusted but reporting plain HTTP",
      { "x-forwarded-proto": "http" },
      "http://127.0.0.1:3000/",
      true,
      SESSION_COOKIE_NAME,
    ],
  ];

  it("names and flags the cookie consistently", () => {
    for (const [label, headers, url, trustProxy, expected] of cases) {
      const secure = isSecureRequest(request(headers, url), trustProxy);
      const options = sessionCookieOptions({ kind: "duration", seconds: 60 }, secure);
      expect(options.name, label).toBe(expected);
      expect(options.secure, label).toBe(expected === SECURE_SESSION_COOKIE_NAME);
      expect(options.path, label).toBe("/");
      expect(options.httpOnly, label).toBe(true);
      expect(options, label).not.toHaveProperty("domain");
    }
  });

  it("refuses the plain cookie name on a secure request", () => {
    // A sibling subdomain can write the unprefixed name but never the pinned
    // one, so over HTTPS only the pinned name is honored.
    const jar: Record<string, string> = { [SESSION_COOKIE_NAME]: "tossed" };
    expect(readSessionCookie((name) => jar[name], true)).toBeUndefined();
    expect(readSessionCookie((name) => jar[name], false)).toBe("tossed");
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
