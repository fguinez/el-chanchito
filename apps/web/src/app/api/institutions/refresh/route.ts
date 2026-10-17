import { NextRequest, NextResponse } from "next/server";
import { requireJsonContentType } from "@/lib/api/validation";

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
 * spinning forever. A scraper inside its manual-refresh cooldown is passed
 * through as the service reports it: `429` + `Retry-After` for a single
 * institution, a `skipped` list in the `202` of a refresh-all.
 */
export async function POST(request: NextRequest) {
  const controlUrl = process.env.SCRAPER_CONTROL_URL;
  if (!controlUrl) {
    return NextResponse.json(
      { error: "Servicio de scrapers no configurado (SCRAPER_CONTROL_URL)." },
      { status: 503 }
    );
  }

  // Body is optional: none at all means "refresh everything". One that is
  // there must be labelled JSON, so a cross-origin form post cannot start a
  // live bank login (the proxy's Origin check is the other half).
  let institution: string | undefined;
  const raw = await request.text();
  if (raw.trim() !== "" || request.headers.has("content-type")) {
    const unsupported = requireJsonContentType(request);
    if (unsupported) return unsupported;
    try {
      const body = raw.trim() === "" ? {} : JSON.parse(raw);
      if (typeof body?.institution === "string") institution = body.institution;
    } catch {
      return NextResponse.json({ error: "Invalid JSON body" }, { status: 400 });
    }
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
    const retryAfter = res.headers.get("Retry-After");
    return NextResponse.json(data, {
      status: res.status,
      headers: retryAfter ? { "Retry-After": retryAfter } : undefined,
    });
  } catch {
    return NextResponse.json(
      { error: "Servicio de scrapers no disponible." },
      { status: 503 }
    );
  }
}
