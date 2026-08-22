/**
 * Single-user dashboard auth configuration.
 *
 * Everything here is a pure function of environment values so the enforcement
 * decisions can be unit tested without booting a server. See ARCHITECTURE.md
 * ("Dashboard authentication") for the security posture.
 */

/** Cookie carrying the signed session token. */
export const SESSION_COOKIE_NAME = "chanchito_session";

/** Public paths reachable without a session (login UI plus its endpoints). */
export const LOGIN_PATH = "/login";
export const SETUP_REQUIRED_PATH = "/setup-required";

const PUBLIC_PATHS = new Set([
  LOGIN_PATH,
  "/api/auth/login",
  "/api/auth/session",
  "/favicon.ico",
]);

/** Default re-login window when DASHBOARD_SESSION_MAX_AGE is unset. */
export const DEFAULT_SESSION_MAX_AGE_SECONDS = 12 * 60 * 60;

/**
 * Hard cap baked into the token of a `browser` session: the cookie dies with
 * the browser session, but an exfiltrated cookie must not live forever.
 */
export const BROWSER_TOKEN_CAP_SECONDS = 30 * 24 * 60 * 60;

/** `unlimited` uses the browser's own maximum cookie age (400 days). */
export const UNLIMITED_SESSION_SECONDS = 400 * 24 * 60 * 60;

export type SessionPolicy =
  /** Absolute expiry after `seconds` since login. */
  | { kind: "duration"; seconds: number }
  /** Session cookie: gone when the browser closes. */
  | { kind: "browser" }
  /** Practically no re-login (400 days). */
  | { kind: "unlimited" };

export interface ParsedSessionPolicy {
  policy: SessionPolicy;
  /** Set when the configured value was unusable and the default was applied. */
  warning?: string;
}

const DURATION_PATTERN = /^(\d+)([smhd])?$/;

const UNIT_SECONDS: Record<string, number> = {
  s: 1,
  m: 60,
  h: 60 * 60,
  d: 24 * 60 * 60,
};

/**
 * Parses DASHBOARD_SESSION_MAX_AGE: `browser`, `unlimited`, a duration string
 * (`30m`, `12h`, `7d`) or a bare number of seconds. Invalid values fail closed
 * to the 12h default and report a warning for the caller to log once.
 */
export function parseSessionMaxAge(raw: string | undefined | null): ParsedSessionPolicy {
  const value = raw?.trim();
  if (!value) {
    return { policy: { kind: "duration", seconds: DEFAULT_SESSION_MAX_AGE_SECONDS } };
  }

  const normalized = value.toLowerCase();
  if (normalized === "browser") return { policy: { kind: "browser" } };
  if (normalized === "unlimited") return { policy: { kind: "unlimited" } };

  const match = DURATION_PATTERN.exec(normalized);
  if (match) {
    const amount = Number(match[1]);
    const seconds = amount * UNIT_SECONDS[match[2] ?? "s"];
    if (seconds > 0 && Number.isFinite(seconds)) {
      return { policy: { kind: "duration", seconds } };
    }
  }

  return {
    policy: { kind: "duration", seconds: DEFAULT_SESSION_MAX_AGE_SECONDS },
    warning:
      `Invalid DASHBOARD_SESSION_MAX_AGE value; falling back to ` +
      `${DEFAULT_SESSION_MAX_AGE_SECONDS}s. Accepted: a duration (30m, 12h, 7d), ` +
      `a number of seconds, "browser" or "unlimited".`,
  };
}

/** How long the signed token stays valid for a given policy. */
export function tokenLifetimeSeconds(policy: SessionPolicy): number {
  switch (policy.kind) {
    case "duration":
      return policy.seconds;
    case "browser":
      return BROWSER_TOKEN_CAP_SECONDS;
    case "unlimited":
      return UNLIMITED_SESSION_SECONDS;
  }
}

/** Cookie Max-Age, or `undefined` for a browser-session cookie. */
export function cookieMaxAgeSeconds(policy: SessionPolicy): number | undefined {
  switch (policy.kind) {
    case "duration":
      return policy.seconds;
    case "browser":
      return undefined;
    case "unlimited":
      return UNLIMITED_SESSION_SECONDS;
  }
}

export type AuthMode =
  /** No password configured in development: behave as if auth did not exist. */
  | "disabled"
  /** A password is configured: enforce it everywhere. */
  | "enforced"
  /** Production without a password: refuse to serve anything (fail closed). */
  | "misconfigured";

export interface AuthEnv {
  password?: string | null;
  nodeEnv?: string;
}

/**
 * The whole security posture in one pure function: a configured password always
 * wins, a missing one is tolerated only outside production.
 */
export function decideAuthMode({ password, nodeEnv }: AuthEnv): AuthMode {
  if (password && password.length > 0) return "enforced";
  return nodeEnv === "production" ? "misconfigured" : "disabled";
}

export function isApiPath(pathname: string): boolean {
  return pathname === "/api" || pathname.startsWith("/api/");
}

export function isPublicPath(pathname: string): boolean {
  return PUBLIC_PATHS.has(pathname);
}

/**
 * Validates a redirect-back target: only same-origin relative paths, so `next`
 * can never be turned into an open redirect.
 */
export function safeNextPath(raw: string | null | undefined): string {
  if (!raw) return "/";
  if (!raw.startsWith("/")) return "/";
  // "//host" and "/\host" are protocol-relative URLs, not local paths.
  if (raw.startsWith("//") || raw.startsWith("/\\")) return "/";
  if (raw.includes("\\") || /[\x00-\x1f]/.test(raw)) return "/";
  return raw;
}
