import { NextResponse } from "next/server";
import { db } from "@/lib/db";
import { sql } from "drizzle-orm";

/** GET /api/scrapers — get the latest run per (method, institution) pair */
export async function GET() {
  const latestRuns = await db.execute(sql`
    SELECT DISTINCT ON (method, institution)
      id, method, institution, started_at, finished_at, status,
      transactions_imported, error_message
    FROM scraper_runs
    ORDER BY method, institution, started_at DESC
  `);

  return NextResponse.json(latestRuns);
}
