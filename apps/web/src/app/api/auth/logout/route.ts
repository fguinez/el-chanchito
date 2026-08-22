import { NextRequest, NextResponse } from "next/server";

import { clearedSessionCookies, isSecureRequest } from "@/lib/auth/cookie";

const NO_STORE = { "cache-control": "no-store" } as const;

/**
 * POST /api/auth/logout — clear the session cookie
 *
 * Public by design (see PUBLIC_PATHS): an expired token must still be flushable
 * from the browser, and this only ever deletes a cookie.
 */
export async function POST(request: NextRequest) {
  const response = NextResponse.json({ authenticated: false }, { headers: NO_STORE });
  for (const cookie of clearedSessionCookies(isSecureRequest(request))) {
    response.cookies.set(cookie);
  }
  return response;
}
