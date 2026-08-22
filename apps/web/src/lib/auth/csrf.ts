/**
 * Cross-site request rejection for mutating API calls.
 *
 * Defense in depth on top of `SameSite=Lax`, which only stops *cross-site*
 * requests: a sibling subdomain, or any host sharing a registrable suffix with
 * ours (`*.duckdns.org`, `*.nip.io`, `*.ngrok-free.app`, `*.trycloudflare.com`),
 * is "same-site" to a browser and would otherwise be free to POST to every API.
 *
 * Both checks are allow-lists: an unrecognized or oddly cased header value is
 * rejected rather than waved through.
 */

/** Methods that cannot change state and therefore skip the checks below. */
const SAFE_METHODS = new Set(["GET", "HEAD"]);

export function isMutatingMethod(method: string): boolean {
  return !SAFE_METHODS.has(method.toUpperCase());
}

/**
 * `Sec-Fetch-Site` values we accept. `none` is a direct user action (typing the
 * URL, a bookmark); `same-origin` is our own page. Everything else, including
 * `same-site` and `cross-site` in any casing, is refused. An absent header is
 * allowed: non-browser clients (curl, the scraper service) never send it, and
 * the Origin check below is the backstop for browsers that do not.
 */
const ALLOWED_FETCH_SITES = new Set(["same-origin", "none"]);

export function isAllowedFetchSite(raw: string | null | undefined): boolean {
  if (!raw) return true;
  return ALLOWED_FETCH_SITES.has(raw.trim().toLowerCase());
}

/**
 * The host the client believes it is talking to.
 *
 * `X-Forwarded-Host` wins over `Host` so this keeps working behind a reverse
 * proxy that rewrites `Host` to an internal name.
 */
export function requestHost(
  headers: Pick<Headers, "get">
): string | undefined {
  const forwarded = headers.get("x-forwarded-host")?.split(",")[0]?.trim();
  const host = forwarded || headers.get("host")?.trim();
  return host ? host.toLowerCase() : undefined;
}

/**
 * Whether `Origin` refers to us.
 *
 * The comparison is deliberately against the request's own host headers and not
 * against `request.nextUrl.origin`: behind a TLS-terminating reverse proxy the
 * latter reflects the internal scheme and port (`http://localhost:3000`) while
 * the browser sends the external origin (`https://dashboard.example`), so
 * comparing the two would reject every legitimate write in a normal proxied
 * deployment. Comparing hosts keeps the protection intact, because a genuine
 * same-origin request always addresses the host it came from: an attacker page
 * still sends our host in `Host` and its own in `Origin`, which mismatches.
 *
 * The scheme is not compared for the same reason (`X-Forwarded-Proto` is not
 * guaranteed to be present or accurate); HTTPS enforcement belongs to the proxy
 * and to the `Secure` cookie flag, not here. An `Origin` that is not a parseable
 * URL (including the opaque `null`) is rejected.
 */
export function isAllowedOrigin(
  origin: string | null | undefined,
  headers: Pick<Headers, "get">
): boolean {
  if (!origin) return true;

  let originHost: string;
  try {
    originHost = new URL(origin).host.toLowerCase();
  } catch {
    return false;
  }
  if (!originHost) return false;

  const host = requestHost(headers);
  return host !== undefined && originHost === host;
}
