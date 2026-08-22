/**
 * Server-only readers for the auth environment. Kept apart from `config.ts` so
 * the pure helpers there stay importable from client components.
 */

import {
  decideAuthMode,
  parseSessionMaxAge,
  parseTrustProxy,
  type AuthMode,
  type SessionPolicy,
} from "./config";

let warnedAboutSurroundingWhitespace = false;

/**
 * The configured shared secret, or `undefined` when auth is not set up.
 *
 * The value is trimmed: a trailing newline from a Keychain, Docker secret or
 * `.env` round trip would otherwise become part of the password and make the
 * login impossible to reproduce by hand. The mismatch is surfaced once (never
 * the value itself) instead of being trimmed silently.
 */
export function dashboardPassword(): string | undefined {
  const raw = process.env.DASHBOARD_PASSWORD;
  if (!raw) return undefined;

  const value = raw.trim();
  if (value.length === 0) return undefined;

  if (value !== raw && !warnedAboutSurroundingWhitespace) {
    warnedAboutSurroundingWhitespace = true;
    console.warn(
      "[auth] DASHBOARD_PASSWORD had leading or trailing whitespace; it was " +
        "trimmed. Check how the secret is exported (a trailing newline is the " +
        "usual cause)."
    );
  }

  return value;
}

export function currentAuthMode(): AuthMode {
  return decideAuthMode({
    password: process.env.DASHBOARD_PASSWORD,
    nodeEnv: process.env.NODE_ENV,
  });
}

/**
 * Whether this deployment sits behind a reverse proxy whose `X-Forwarded-*`
 * headers can be believed. Off unless DASHBOARD_TRUST_PROXY says otherwise.
 */
export function trustProxyHeaders(): boolean {
  return parseTrustProxy(process.env.DASHBOARD_TRUST_PROXY);
}

/** Reads the session policy, logging at most one warning per invalid value. */
export function currentSessionPolicy(): SessionPolicy {
  const { policy, warning } = parseSessionMaxAge(process.env.DASHBOARD_SESSION_MAX_AGE);
  if (warning) console.warn(`[auth] ${warning}`);
  return policy;
}
