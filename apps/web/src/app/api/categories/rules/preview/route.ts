import { NextRequest, NextResponse } from "next/server";
import { count, desc, sql } from "drizzle-orm";
import { db } from "@/lib/db";
import { transactions } from "@/lib/db/schema";
import { normalizeKeyword } from "@/lib/categories";

const PREVIEW_LIMIT = 20;

/**
 * GET /api/categories/rules/preview?keyword=...: what a rule with this keyword
 * would match ("probar regla"), with the same normalization and literal
 * substring semantics as `category_for_description` (V020). Returns
 * `{ keyword, total, uncategorized, transactions }`: `total` counts every
 * matching transaction, `uncategorized` the matches a backfill could still
 * categorize (no category, not manual), and `transactions` lists up to 20
 * matches, newest first.
 */
export async function GET(request: NextRequest) {
  try {
    const keyword = normalizeKeyword(
      request.nextUrl.searchParams.get("keyword")
    );
    if (!keyword.ok) {
      return NextResponse.json(
        { error: keyword.error, field: keyword.field },
        { status: keyword.status }
      );
    }

    const matches = sql`strpos(lower(${transactions.description}), lower(${keyword.value})) > 0`;
    const uncategorized = sql<number>`count(*) FILTER (
      WHERE ${transactions.categoryId} IS NULL
        AND NOT ${transactions.isManuallyCategorized}
    )`;

    const [[counts], rows] = await Promise.all([
      db
        .select({
          total: count(),
          uncategorized: uncategorized.mapWith(Number),
        })
        .from(transactions)
        .where(matches),
      db
        .select({
          id: transactions.id,
          description: transactions.description,
          amount: transactions.amount,
          transactionDate: transactions.transactionDate,
          categoryId: transactions.categoryId,
          isManuallyCategorized: transactions.isManuallyCategorized,
        })
        .from(transactions)
        .where(matches)
        .orderBy(
          desc(transactions.transactionDate),
          desc(transactions.createdAt),
          desc(transactions.id)
        )
        .limit(PREVIEW_LIMIT),
    ]);

    return NextResponse.json({
      keyword: keyword.value,
      total: counts.total,
      uncategorized: counts.uncategorized,
      transactions: rows,
    });
  } catch (error) {
    console.error("GET /api/categories/rules/preview failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}
