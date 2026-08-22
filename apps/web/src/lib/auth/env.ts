/**
 * Server-only readers for the auth environment. Kept apart from `config.ts` so
 * the pure helpers there stay importable from client components.
 */

import {
  decideAuthMode,
  parseSessionMaxAge,
  type AuthMode,
  type SessionPolicy,
} from "./config";

/** The configured shared secret, or `undefined` when auth is not set up. */
export function dashboardPassword(): string | undefined {
  const value = process.env.DASHBOARD_PASSWORD;
  return value && value.length > 0 ? value : undefined;
}

export function currentAuthMode(): AuthMode {
  return decideAuthMode({
    password: process.env.DASHBOARD_PASSWORD,
    nodeEnv: process.env.NODE_ENV,
  });
}

/** Reads the session policy, logging at most one warning per invalid value. */
export function currentSessionPolicy(): SessionPolicy {
  const { policy, warning } = parseSessionMaxAge(process.env.DASHBOARD_SESSION_MAX_AGE);
  if (warning) console.warn(`[auth] ${warning}`);
  return policy;
}
