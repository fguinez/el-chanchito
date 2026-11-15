import { NextResponse } from "next/server";

import { scraperControlUrl } from "@/lib/scraper-login";

/** Institution slugs as the scraper service names them (`bci_lider`). */
export const INSTITUTION_SLUG = /^[a-z0-9_]{1,64}$/;

/**
 * Forward one `/login` call to the scraper service and pass its answer
 * through. POSTs carry a JSON body, which the service requires; the request
 * is made server-side, so it carries no browser `Origin`, which the service
 * refuses. `503` when the service is not configured or unreachable.
 */
export async function forwardLogin(
  path: string,
  init: { method: "GET" } | { method: "POST"; body: unknown }
): Promise<NextResponse> {
  const controlUrl = scraperControlUrl();
  if (!controlUrl) {
    return NextResponse.json(
      { error: "Servicio de scrapers no configurado (SCRAPER_CONTROL_URL)." },
      { status: 503 }
    );
  }
  try {
    const res = await fetch(`${controlUrl}${path}`, {
      method: init.method,
      headers:
        init.method === "POST" ? { "content-type": "application/json" } : undefined,
      body: init.method === "POST" ? JSON.stringify(init.body) : undefined,
      signal: AbortSignal.timeout(5000),
    });
    const data = await res.json().catch(() => ({}));
    return NextResponse.json(data, {
      status: res.status,
      headers: { "cache-control": "no-store" },
    });
  } catch {
    return NextResponse.json(
      { error: "Servicio de scrapers no disponible." },
      { status: 503 }
    );
  }
}

export function badInstitution(): NextResponse {
  return NextResponse.json({ error: "Institución inválida" }, { status: 400 });
}
