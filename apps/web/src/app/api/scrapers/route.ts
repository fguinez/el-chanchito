import { NextResponse } from "next/server";
import { db } from "@/lib/db";
import { sql } from "drizzle-orm";

/**
 * GET /api/scrapers — the latest run per institution, with the institution's
 * display name (`institution_name`, null when the slug has no `institutions`
 * row). Keyed on the institution alone because a scraper's method can change
 * with its configuration (Mercado Pago records `http_api` once its token is
 * set, `email` otherwise), and a run under the old method must not linger as
 * a second, stale status.
 */
export async function GET() {
  const latestRuns = await db.execute(sql`
    SELECT DISTINCT ON (r.institution)
      r.id, r.method, r.institution, i.name AS institution_name,
      r.started_at, r.finished_at, r.status,
      r.transactions_imported, r.error_message
    FROM scraper_runs r
    LEFT JOIN institutions i ON i.slug = r.institution
    ORDER BY r.institution, r.started_at DESC
  `);

  return NextResponse.json(latestRuns);
}
