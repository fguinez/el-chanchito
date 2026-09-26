import { NextResponse } from "next/server";
import { db } from "@/lib/db";
import { sql } from "drizzle-orm";

/**
 * GET /api/scrapers — the latest run per institution. Keyed on the institution
 * alone because a scraper's method can change with its configuration (Mercado
 * Pago records `http_api` once its token is set, `email` otherwise), and a run
 * under the old method must not linger as a second, stale status.
 */
export async function GET() {
  const latestRuns = await db.execute(sql`
    SELECT DISTINCT ON (institution)
      id, method, institution, started_at, finished_at, status,
      transactions_imported, error_message
    FROM scraper_runs
    ORDER BY institution, started_at DESC
  `);

  return NextResponse.json(latestRuns);
}
