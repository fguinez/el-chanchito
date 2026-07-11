import { NextRequest, NextResponse } from "next/server";

/**
 * POST /api/institutions/refresh — ask the scraper service to run now.
 *
 * Body (optional): `{ "institution": "<slug>" }`. With a slug we trigger just
 * that institution's scraper; without one every configured scraper runs. This
 * is a thin proxy to the scraper service's internal control server
 * (`SCRAPER_CONTROL_URL`); the scrape itself runs asynchronously there, so a
 * `202` means "accepted", not "done" — poll `GET /api/scrapers` for progress.
 *
 * Returns `503` when the scraper service isn't configured or can't be reached
 * (e.g. the container isn't running), so the UI can explain it instead of
 * spinning forever.
 */
export async function POST(request: NextRequest) {
  const controlUrl = process.env.SCRAPER_CONTROL_URL;
  if (!controlUrl) {
    return NextResponse.json(
      { error: "Servicio de scrapers no configurado (SCRAPER_CONTROL_URL)." },
      { status: 503 }
    );
  }

  // Body is optional — an empty/absent body means "refresh everything".
  let institution: string | undefined;
  try {
    const body = await request.json();
    if (typeof body?.institution === "string") institution = body.institution;
  } catch {
    /* no body */
  }

  const target = institution
    ? `${controlUrl.replace(/\/$/, "")}/refresh/${encodeURIComponent(institution)}`
    : `${controlUrl.replace(/\/$/, "")}/refresh`;

  try {
    const res = await fetch(target, {
      method: "POST",
      signal: AbortSignal.timeout(5000),
    });
    const data = await res.json().catch(() => ({}));
    return NextResponse.json(data, { status: res.status });
  } catch {
    return NextResponse.json(
      { error: "Servicio de scrapers no disponible." },
      { status: 503 }
    );
  }
}
