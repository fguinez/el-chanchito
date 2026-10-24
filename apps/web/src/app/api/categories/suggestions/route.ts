import { NextResponse } from "next/server";
import { listCategorySuggestions } from "@/lib/category-suggestions-db";

/**
 * GET /api/categories/suggestions: uncategorized transactions grouped by raw
 * description (trimmed, case-insensitive), each with a suggested category
 * learned from the categorized ones, as `{ suggestions }`. Each one has
 * `description`, `count` (uncategorized rows), `latestDate`,
 * `sampleTransactionId` (its most recent row), `categoryId`, `categoryName`,
 * `confidence` (0 to 1), `matchCount`, `exact` and a Spanish `reason`. Only
 * suggestions above the confidence threshold and not dismissed are listed,
 * most rows first. Nothing is applied: see .../apply, .../dismiss and
 * POST /api/transactions/[id]/category-rule.
 */
export async function GET() {
  try {
    return NextResponse.json({ suggestions: await listCategorySuggestions() });
  } catch (error) {
    console.error("GET /api/categories/suggestions failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}
