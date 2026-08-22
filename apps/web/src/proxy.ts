import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

import {
  LOGIN_PATH,
  SETUP_REQUIRED_PATH,
  isApiPath,
  isPublicPath,
  readSessionCookie,
} from "@/lib/auth/config";
import {
  isAllowedFetchSite,
  isAllowedOrigin,
  isMutatingMethod,
} from "@/lib/auth/csrf";
import { isSecureRequest } from "@/lib/auth/cookie";
import {
  currentAuthMode,
  dashboardPassword,
  trustProxyHeaders,
} from "@/lib/auth/env";
import { verifySessionToken } from "@/lib/auth/session";

/**
 * Single enforcement point for dashboard auth (see ARCHITECTURE.md).
 *
 * - No DASHBOARD_PASSWORD outside production: transparent, `next dev` is
 *   unchanged.
 * - No DASHBOARD_PASSWORD in production: fail closed, nothing is served.
 * - DASHBOARD_PASSWORD set: a valid signed session is required for every page
 *   and every API route, except the login surfaces.
 */

const NO_STORE = { "cache-control": "no-store" } as const;

/** Pages are personal: never let a shared cache hold on to one. */
const PRIVATE_NO_STORE = "private, no-store";

function jsonError(message: string, status: number) {
  return NextResponse.json({ error: message }, { status, headers: NO_STORE });
}

function redirect(url: URL) {
  const response = NextResponse.redirect(url);
  // Without this a shared cache could hand this redirect to a logged-in user,
  // or cache the post-login destination for an anonymous one.
  response.headers.set("cache-control", "no-store");
  return response;
}

export async function proxy(request: NextRequest) {
  const { pathname, search } = request.nextUrl;
  const mode = currentAuthMode();

  if (mode === "disabled") return NextResponse.next();

  const api = isApiPath(pathname);
  // Forwarded headers are believed only where a proxy is declared to exist; see
  // lib/auth/config.ts (parseTrustProxy).
  const trustProxy = trustProxyHeaders();

  if (mode === "misconfigured") {
    if (api) {
      return jsonError(
        "Dashboard authentication is not configured: set DASHBOARD_PASSWORD.",
        503
      );
    }
    return NextResponse.rewrite(new URL(SETUP_REQUIRED_PATH, request.url));
  }

  // Defense in depth on top of SameSite=Lax, which does not stop a same-site
  // cross-origin write (a sibling subdomain, or any host under a shared public
  // suffix). Both checks are allow-lists; see lib/auth/csrf.ts.
  if (api && isMutatingMethod(request.method)) {
    if (
      !isAllowedFetchSite(request.headers.get("sec-fetch-site")) ||
      !isAllowedOrigin(request.headers.get("origin"), request.headers, trustProxy)
    ) {
      return jsonError("Cross-site request rejected", 403);
    }
  }

  const authenticated = await verifySessionToken(
    readSessionCookie(
      (name) => request.cookies.get(name)?.value,
      isSecureRequest(request, trustProxy)
    ),
    dashboardPassword() as string
  );

  if (isPublicPath(pathname)) {
    if (authenticated && pathname === LOGIN_PATH) {
      return redirect(new URL("/", request.url));
    }
    return NextResponse.next();
  }

  if (authenticated) {
    const response = NextResponse.next();
    // Protected pages build as static and would otherwise be served with a
    // year-long s-maxage; a CDN must not keep an authenticated shell around.
    if (!api) response.headers.set("cache-control", PRIVATE_NO_STORE);
    return response;
  }

  if (api) return jsonError("No autenticado", 401);

  const loginUrl = new URL(LOGIN_PATH, request.url);
  const target = `${pathname}${search}`;
  if (target !== "/") loginUrl.searchParams.set("next", target);
  return redirect(loginUrl);
}

export const config = {
  // Everything except Next's own static output and the favicon, both of which
  // the login page itself needs before a session exists.
  matcher: ["/((?!_next/static|_next/image|favicon\\.ico).*)"],
};
