import { NextRequest, NextResponse } from "next/server";
import { asc, desc, sql } from "drizzle-orm";
import { db } from "@/lib/db";
import { descriptionAliases } from "@/lib/db/schema";
import { failureBody, validateAliasInput } from "@/lib/aliases";
import { readJsonBody } from "@/lib/api/validation";

/**
 * GET /api/aliases: every alias in the order it applies (conditional first,
 * then priority desc, oldest first on ties, then id). Returns `{ id, matchDescription, matchType, alias,
 * priority, amountMin, amountMax, currency, createdAt, updatedAt }` rows.
 */
export async function GET() {
  try {
    const rows = await db
      .select()
      .from(descriptionAliases)
      .orderBy(
        desc(
          sql`(${descriptionAliases.amountMin} IS NOT NULL
            OR ${descriptionAliases.amountMax} IS NOT NULL
            OR ${descriptionAliases.currency} IS NOT NULL)`
        ),
        desc(descriptionAliases.priority),
        asc(descriptionAliases.createdAt),
        asc(descriptionAliases.id)
      );
    return NextResponse.json(rows);
  } catch (error) {
    console.error("GET /api/aliases failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}

/** POST /api/aliases: create an alias `{ matchDescription, alias, matchType?,
 *  priority?, amountMin?, amountMax?, currency? }`. An amount bound (V036)
 *  requires a currency. Resolved live, so it relabels matching rows right away
 *  with no backfill. */
export async function POST(request: NextRequest) {
  try {
    const read = await readJsonBody(request);
    if (!read.ok) return read.response;
    const body = read.body;

    const result = validateAliasInput(body);
    if (!result.ok) {
      return NextResponse.json(failureBody(result), { status: result.status });
    }

    const [created] = await db
      .insert(descriptionAliases)
      .values(result.value)
      .returning();
    return NextResponse.json(created, { status: 201 });
  } catch (error) {
    console.error("POST /api/aliases failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}
