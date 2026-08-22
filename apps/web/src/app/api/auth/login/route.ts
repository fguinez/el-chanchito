import { NextRequest, NextResponse } from "next/server";

import { SESSION_COOKIE_NAME, tokenLifetimeSeconds } from "@/lib/auth/config";
import {
  currentAuthMode,
  currentSessionPolicy,
  dashboardPassword,
} from "@/lib/auth/env";
import { isSecureRequest, sessionCookieOptions } from "@/lib/auth/cookie";
import { createSessionToken, verifyPassword } from "@/lib/auth/session";

const NO_STORE = { "cache-control": "no-store" } as const;

/** Fixed delay on failure: no timing signal, no brute-force speed. */
const FAILURE_DELAY_MS = 500;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** POST /api/auth/login — exchange the shared password for a session cookie */
export async function POST(request: NextRequest) {
  const mode = currentAuthMode();

  if (mode === "disabled") {
    return NextResponse.json(
      { error: "Authentication is not enabled" },
      { status: 400, headers: NO_STORE }
    );
  }

  if (mode === "misconfigured") {
    return NextResponse.json(
      { error: "Dashboard authentication is not configured: set DASHBOARD_PASSWORD." },
      { status: 503, headers: NO_STORE }
    );
  }

  let password = "";
  try {
    const body = await request.json();
    if (typeof body?.password === "string") password = body.password;
  } catch {
    password = "";
  }

  const expected = dashboardPassword() as string;
  const ok = password.length > 0 && (await verifyPassword(password, expected));

  if (!ok) {
    await sleep(FAILURE_DELAY_MS);
    return NextResponse.json(
      { error: "invalid_password" },
      { status: 401, headers: NO_STORE }
    );
  }

  const policy = currentSessionPolicy();
  const token = await createSessionToken(expected, tokenLifetimeSeconds(policy));

  const response = NextResponse.json({ authenticated: true }, { headers: NO_STORE });
  response.cookies.set({
    name: SESSION_COOKIE_NAME,
    value: token,
    ...sessionCookieOptions(policy, isSecureRequest(request)),
  });
  return response;
}
