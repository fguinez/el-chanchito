import { NextRequest, NextResponse } from "next/server";

import { SESSION_COOKIE_NAME } from "@/lib/auth/config";
import { isSecureRequest } from "@/lib/auth/cookie";

const NO_STORE = { "cache-control": "no-store" } as const;

/** POST /api/auth/logout — clear the session cookie */
export async function POST(request: NextRequest) {
  const response = NextResponse.json({ authenticated: false }, { headers: NO_STORE });
  response.cookies.set({
    name: SESSION_COOKIE_NAME,
    value: "",
    httpOnly: true,
    sameSite: "lax",
    path: "/",
    secure: isSecureRequest(request),
    maxAge: 0,
  });
  return response;
}
