import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

import {
  LOGIN_PATH,
  SESSION_COOKIE_NAME,
  SETUP_REQUIRED_PATH,
  isApiPath,
  isPublicPath,
} from "@/lib/auth/config";
import { currentAuthMode, dashboardPassword } from "@/lib/auth/env";
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

function jsonError(message: string, status: number) {
  return NextResponse.json({ error: message }, { status, headers: NO_STORE });
}

export async function proxy(request: NextRequest) {
  const { pathname, search } = request.nextUrl;
  const mode = currentAuthMode();

  if (mode === "disabled") return NextResponse.next();

  const api = isApiPath(pathname);

  if (mode === "misconfigured") {
    if (api) {
      return jsonError(
        "Dashboard authentication is not configured: set DASHBOARD_PASSWORD.",
        503
      );
    }
    return NextResponse.rewrite(new URL(SETUP_REQUIRED_PATH, request.url));
  }

  // Defense in depth on top of SameSite=Lax: browsers that send Sec-Fetch-Site
  // let us drop cross-site writes before any handler touches the database.
  if (api && request.method !== "GET" && request.method !== "HEAD") {
    if (request.headers.get("sec-fetch-site") === "cross-site") {
      return jsonError("Cross-site request rejected", 403);
    }
  }

  const authenticated = await verifySessionToken(
    request.cookies.get(SESSION_COOKIE_NAME)?.value,
    dashboardPassword() as string
  );

  if (isPublicPath(pathname)) {
    if (authenticated && pathname === LOGIN_PATH) {
      return NextResponse.redirect(new URL("/", request.url));
    }
    return NextResponse.next();
  }

  if (authenticated) return NextResponse.next();

  if (api) return jsonError("No autenticado", 401);

  const loginUrl = new URL(LOGIN_PATH, request.url);
  const target = `${pathname}${search}`;
  if (target !== "/") loginUrl.searchParams.set("next", target);
  return NextResponse.redirect(loginUrl);
}

export const config = {
  // Everything except Next's own static output and the favicon, both of which
  // the login page itself needs before a session exists.
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
