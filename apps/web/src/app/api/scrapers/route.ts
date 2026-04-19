import { NextResponse } from "next/server";
import { db } from "@/lib/db";
import { scraperRuns } from "@/lib/db/schema";
import { desc, sql } from "drizzle-orm";

/** GET /api/scrapers — get the latest run per scraper */
export async function GET() {
  // Get the most recent run for each scraper using a lateral join equivalent
  const latestRuns = await db.execute(sql`
    SELECT DISTINCT ON (scraper_name)
      id, scraper_name, started_at, finished_at, status,
      transactions_imported, error_message
    FROM scraper_runs
    ORDER BY scraper_name, started_at DESC
  `);

  return NextResponse.json(latestRuns);
}
