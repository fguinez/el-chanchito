import { NextRequest, NextResponse } from "next/server";

import { SESSION_COOKIE_NAME } from "@/lib/auth/config";
import { currentAuthMode, dashboardPassword } from "@/lib/auth/env";
import { verifySessionToken } from "@/lib/auth/session";

const NO_STORE = { "cache-control": "no-store" } as const;

/**
 * GET /api/auth/session — public probe telling the UI whether auth is enabled
 * and whether the caller currently holds a valid session.
 */
export async function GET(request: NextRequest) {
  const mode = currentAuthMode();

  if (mode !== "enforced") {
    return NextResponse.json(
      { enabled: mode === "misconfigured", authenticated: false },
      { headers: NO_STORE }
    );
  }

  const authenticated = await verifySessionToken(
    request.cookies.get(SESSION_COOKIE_NAME)?.value,
    dashboardPassword() as string
  );

  return NextResponse.json({ enabled: true, authenticated }, { headers: NO_STORE });
}
