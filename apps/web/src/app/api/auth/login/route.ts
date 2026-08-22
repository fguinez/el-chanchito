import { NextRequest, NextResponse } from "next/server";

import { tokenLifetimeSeconds } from "@/lib/auth/config";
import {
  currentAuthMode,
  currentSessionPolicy,
  dashboardPassword,
  trustProxyHeaders,
} from "@/lib/auth/env";
import {
  isSecureRequest,
  sessionCookieOptions,
  warnOnForwardedProtoMismatch,
} from "@/lib/auth/cookie";
import { createSessionToken, verifyPassword } from "@/lib/auth/session";
import { loginClientKey, loginThrottle } from "@/lib/auth/throttle";

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
  const trustProxy = trustProxyHeaders();

  // Serialized process wide (one attempt at a time, so the failure delay is a
  // real global ceiling) and locked out per client, so no remote party can lock
  // the owner out of the only account.
  const outcome = await loginThrottle.run(
    loginClientKey(request.headers, trustProxy),
    async () => {
      const ok =
        password.length > 0 && (await verifyPassword(password, expected));
      return { ok, result: ok };
    }
  );

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

  // Says out loud when the Secure flag rests on a forwarded header alone, which
  // is the shape that used to loop the login silently.
  warnOnForwardedProtoMismatch(request, trustProxy);

  const response = NextResponse.json({ authenticated: true }, { headers: NO_STORE });
  response.cookies.set({
    value: token,
    ...sessionCookieOptions(policy, isSecureRequest(request, trustProxy)),
  });
  return response;
}
