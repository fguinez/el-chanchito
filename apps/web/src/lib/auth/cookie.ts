/** Session-cookie plumbing shared by the auth route handlers. */

import { cookieMaxAgeSeconds, type SessionPolicy } from "./config";

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
    httpOnly: true,
    sameSite: "lax",
    path: "/",
    secure,
    // A browser-session cookie must have neither Max-Age nor Expires.
    ...(maxAge === undefined ? {} : { maxAge }),
  };
}
