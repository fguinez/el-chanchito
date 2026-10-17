import { NextResponse } from "next/server";
import { db } from "@/lib/db";
import { sql } from "drizzle-orm";

/**
 * GET /api/scrapers: the latest run per institution, whatever its method, with
 * the institution's display name (`institution_name`, null when the slug has no
 * `institutions` row). Collapsing methods keeps a retired scraper (e.g. the old
 * `fintself` Banco de Chile one) from lingering next to its replacement.
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
