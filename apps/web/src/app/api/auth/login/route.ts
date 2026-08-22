import { NextRequest, NextResponse } from "next/server";

import { tokenLifetimeSeconds } from "@/lib/auth/config";
import {
  currentAuthMode,
  currentSessionPolicy,
  dashboardPassword,
} from "@/lib/auth/env";
import { isSecureRequest, sessionCookieOptions } from "@/lib/auth/cookie";
import { createSessionToken, verifyPassword } from "@/lib/auth/session";
import { loginThrottle } from "@/lib/auth/throttle";

const NO_STORE = { "cache-control": "no-store" } as const;

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
    // A malformed body is indistinguishable from a wrong password below.
    password = "";
  }

  const expected = dashboardPassword() as string;

  // Serialized and rate limited: one attempt at a time, process wide.
  const outcome = await loginThrottle.run(async () => {
    const ok = password.length > 0 && (await verifyPassword(password, expected));
    return { ok, result: ok };
  });

  if (outcome.status === "locked") {
    return NextResponse.json(
      { error: "too_many_attempts" },
      {
        status: 429,
        headers: {
          ...NO_STORE,
          "retry-after": String(outcome.retryAfterSeconds),
        },
      }
    );
  }

  if (!outcome.result) {
    return NextResponse.json(
      { error: "invalid_password" },
      { status: 401, headers: NO_STORE }
    );
  }

  const policy = currentSessionPolicy();
  const token = await createSessionToken(expected, tokenLifetimeSeconds(policy));

  const response = NextResponse.json({ authenticated: true }, { headers: NO_STORE });
  response.cookies.set({
    value: token,
    ...sessionCookieOptions(policy, isSecureRequest(request)),
  });
  return response;
}
