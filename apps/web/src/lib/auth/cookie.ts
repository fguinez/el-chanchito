/** Session-cookie plumbing shared by the auth route handlers. */

import {
  SECURE_SESSION_COOKIE_NAME,
  SESSION_COOKIE_NAME,
  cookieMaxAgeSeconds,
  sessionCookieName,
  type SessionPolicy,
} from "./config";

/**
 * Whether the cookie may carry the `secure` flag.
 *
 * `x-forwarded-proto` is honored so the flag is still set behind a
 * TLS-terminating reverse proxy, but only when DASHBOARD_TRUST_PROXY declares
 * one: the header is client-writable otherwise. Believing a phantom `https` is
 * what used to produce the silent login loop described in USAGE.md (a `Secure`
 * `__Host-` cookie the browser drops on a plain-HTTP leg).
 *
 * With the header present but untrusted the answer is `false`, not "ask the
 * URL": the Next server builds `request.url`'s scheme *from* that same header
 * (`next-server.js`, and `resolve-routes.js` for the socket case), so the URL is
 * no more independent than the header is. The URL is therefore only consulted
 * when no forwarded proto is in play at all, which is the one case where its
 * scheme reflects the connection. `next start` always stamps the header, so in
 * practice a `Secure` cookie needs either TLS in front plus
 * DASHBOARD_TRUST_PROXY, or a custom server terminating TLS itself.
 */
export function isSecureRequest(request: Request, trustProxy: boolean): boolean {
  const forwarded = request.headers.get("x-forwarded-proto");
  if (forwarded) {
    if (!trustProxy) return false;
    return forwarded.split(",")[0].trim().toLowerCase() === "https";
  }
  return new URL(request.url).protocol === "https:";
}

/**
 * True when the `Secure` flag rests on a forwarded header and nothing else.
 *
 * Normal behind a TLS-terminating proxy, and impossible to tell apart from the
 * broken case from inside the request: if the browser leg is plain HTTP too, the
 * `Secure` cookie is dropped without a word and the login loops. Hence the
 * warning below (once, for the operator) and the client-side re-check on the
 * login page (certain, for the user).
 */
export function derivesSecureFromForwardedProto(
  request: Request,
  trustProxy: boolean
): boolean {
  if (!trustProxy) return false;
  const forwarded = request.headers.get("x-forwarded-proto");
  if (!forwarded) return false;
  return forwarded.split(",")[0].trim().toLowerCase() === "https";
}

let warnedAboutForwardedProto = false;

/** Logs the loop-shaped misconfiguration once per process. Never logs secrets. */
export function warnOnForwardedProtoMismatch(
  request: Request,
  trustProxy: boolean
): void {
  if (warnedAboutForwardedProto) return;
  if (!derivesSecureFromForwardedProto(request, trustProxy)) return;

  warnedAboutForwardedProto = true;
  console.warn(
    "[auth] Setting a Secure __Host- session cookie on the strength of " +
      "x-forwarded-proto alone (DASHBOARD_TRUST_PROXY is on). That is expected " +
      "behind a TLS-terminating proxy. If the browser reaches the dashboard " +
      "over http:// anyway, the cookie is dropped silently and the login loops " +
      "back to /login: serve it over HTTPS, or unset DASHBOARD_TRUST_PROXY."
  );
}

export interface SessionCookieOptions {
  name: string;
  httpOnly: true;
  sameSite: "lax";
  path: "/";
  secure: boolean;
  maxAge?: number;
}

export function sessionCookieOptions(
  policy: SessionPolicy,
  secure: boolean
): SessionCookieOptions {
  const maxAge = cookieMaxAgeSeconds(policy);
  return {
    // `__Host-` over HTTPS; the plain name is the only option over plain HTTP,
    // where browsers reject the prefix outright.
    name: sessionCookieName(secure),
    httpOnly: true,
    sameSite: "lax",
    path: "/",
    secure,
    // A browser-session cookie must have neither Max-Age nor Expires.
    ...(maxAge === undefined ? {} : { maxAge }),
  };
}

export interface ClearedSessionCookie {
  name: string;
  value: "";
  httpOnly: true;
  sameSite: "lax";
  path: "/";
  secure: boolean;
  maxAge: 0;
}

/**
 * Every cookie name a session could be sitting under, with attributes browsers
 * accept for deletion. `__Host-` cookies are only ever deleted with `Secure`
 * set, which is the same condition under which they can exist at all.
 */
export function clearedSessionCookies(secure: boolean): ClearedSessionCookie[] {
  const base = {
    value: "",
    httpOnly: true,
    sameSite: "lax",
    path: "/",
    maxAge: 0,
  } as const;
  return [
    { name: SESSION_COOKIE_NAME, ...base, secure },
    { name: SECURE_SESSION_COOKIE_NAME, ...base, secure: true },
  ];
}
