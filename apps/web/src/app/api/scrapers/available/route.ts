import { NextResponse } from "next/server";

/**
 * GET /api/scrapers/available — which institutions have a live scraper.
 *
 * Thin proxy to the scraper service's control server (`SCRAPER_CONTROL_URL`),
 * which decides at startup which scrapers are enabled based on the configured
 * credentials, so the UI can't hardcode the list. Always answers `200`: when
 * the service isn't configured, unreachable, or replies with something
 * unexpected we degrade to `{ "scrapers": [] }`, which the UI renders as "no
 * per-institution refresh available" — the right call when the service is
 * down anyway; an actual refresh attempt still surfaces a proper `503`
 * through `POST /api/institutions/refresh`.
 */
export async function GET() {
  const controlUrl = process.env.SCRAPER_CONTROL_URL;
  if (!controlUrl) return NextResponse.json({ scrapers: [] });

  try {
    const res = await fetch(`${controlUrl.replace(/\/$/, "")}/scrapers`, {
      signal: AbortSignal.timeout(5000),
    });
    if (!res.ok) return NextResponse.json({ scrapers: [] });
    const data = await res.json();
    const scrapers = Array.isArray(data?.scrapers)
      ? data.scrapers.filter((s: unknown): s is string => typeof s === "string")
      : [];
    return NextResponse.json({ scrapers });
  } catch {
    return NextResponse.json({ scrapers: [] });
  }
}
