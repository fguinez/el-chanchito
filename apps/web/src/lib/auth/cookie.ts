/** Session-cookie plumbing shared by the auth route handlers. */

import {
  SECURE_SESSION_COOKIE_NAME,
  SESSION_COOKIE_NAME,
  cookieMaxAgeSeconds,
  sessionCookieName,
  type SessionPolicy,
} from "./config";

/**
 * Whether the cookie may carry the `secure` flag. `x-forwarded-proto` is
 * honored so the flag is still set behind a TLS-terminating reverse proxy.
 */
export function isSecureRequest(request: Request): boolean {
  const forwarded = request.headers.get("x-forwarded-proto");
  if (forwarded) {
    return forwarded.split(",")[0].trim().toLowerCase() === "https";
  }
  return new URL(request.url).protocol === "https:";
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
