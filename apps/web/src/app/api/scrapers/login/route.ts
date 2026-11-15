import { NextResponse } from "next/server";

import { parseSupportedSignIns, scraperControlUrl } from "@/lib/scraper-login";

/**
 * GET /api/scrapers/login: which institutions the dashboard can sign in to
 * (`{ logins: [{ institution, method }] }`), so a needs_sign_in notice only
 * offers the button where it works. Proxies the scraper service's
 * `GET /login`; like `/api/scrapers/available`, it always answers `200` and
 * degrades to an empty list when the service is not configured or down.
 */
export async function GET() {
  const controlUrl = scraperControlUrl();
  if (!controlUrl) return NextResponse.json({ logins: [] });
  try {
    const res = await fetch(`${controlUrl}/login`, {
      signal: AbortSignal.timeout(5000),
    });
    if (!res.ok) return NextResponse.json({ logins: [] });
    return NextResponse.json({ logins: parseSupportedSignIns(await res.json()) });
  } catch {
    return NextResponse.json({ logins: [] });
  }
}
