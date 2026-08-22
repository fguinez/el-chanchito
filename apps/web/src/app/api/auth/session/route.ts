import { NextRequest, NextResponse } from "next/server";

import { readSessionCookie } from "@/lib/auth/config";
import { currentAuthMode, dashboardPassword } from "@/lib/auth/env";
import { verifySessionToken } from "@/lib/auth/session";

const NO_STORE = { "cache-control": "no-store" } as const;

/**
 * GET /api/auth/session — public probe telling the UI whether auth is enabled
 * and whether the caller currently holds a valid session.
 */
export async function GET(request: NextRequest) {
  const mode = currentAuthMode();

  // Only `disabled` can reach this: the proxy answers 503 for every /api/*
  // route while misconfigured.
  if (mode !== "enforced") {
    return NextResponse.json(
      { enabled: false, authenticated: false },
      { headers: NO_STORE }
    );
  }

  const authenticated = await verifySessionToken(
    readSessionCookie((name) => request.cookies.get(name)?.value),
    dashboardPassword() as string
  );

  return NextResponse.json({ enabled: true, authenticated }, { headers: NO_STORE });
}
