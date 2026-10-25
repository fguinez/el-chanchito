import { NextRequest, NextResponse } from "next/server";
import { and, count, desc, eq, sql } from "drizzle-orm";
import { db } from "@/lib/db";
import { products, transactions } from "@/lib/db/schema";
import { notDeleted } from "@/lib/db/transactions";
import {
  RULE_MATCH_TYPES,
  failureBody,
  normalizeKeyword,
  type RuleMatchType,
} from "@/lib/categories";

const PREVIEW_LIMIT = 20;

/**
 * GET /api/categories/rules/preview?keyword=...&matchType=...: what a rule with
 * this keyword would match ("probar regla"), with the same normalization and
 * semantics as `category_for_description`: `matchType` "contains" (default) is
 * the literal substring of V021, "exact" the trimmed, case-insensitive equality
 * of V041. Returns `{ keyword, matchType, total, uncategorized, transactions }`: `total` counts every
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
      return NextResponse.json(failureBody(keyword), { status: keyword.status });
    }
    const rawMatchType =
      request.nextUrl.searchParams.get("matchType") ?? "contains";
    if (!(RULE_MATCH_TYPES as readonly string[]).includes(rawMatchType)) {
      return NextResponse.json(
        failureBody({
          error: `Field 'matchType' must be one of: ${RULE_MATCH_TYPES.join(", ")}`,
          field: "matchType",
        }),
        { status: 400 }
      );
    }
    const matchType = rawMatchType as RuleMatchType;

    // A soft-deleted row (V033) is not a match a rule could ever categorize.
    // The rule matches the raw scraped `description`, not the custom override.
    const matches = and(
      matchType === "exact"
        ? sql`lower(btrim(${transactions.description})) = lower(${keyword.value})`
        : sql`strpos(lower(${transactions.description}), lower(${keyword.value})) > 0`,
      notDeleted
    );
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
          // `amount` is in this currency (V023): USD rows keep their cents.
          currency: products.currency,
          transactionDate: transactions.transactionDate,
          categoryId: transactions.categoryId,
          isManuallyCategorized: transactions.isManuallyCategorized,
        })
        .from(transactions)
        .innerJoin(products, eq(transactions.productId, products.id))
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
      matchType,
      total: counts.total,
      uncategorized: counts.uncategorized,
      transactions: rows,
    });
  } catch (error) {
    console.error("GET /api/categories/rules/preview failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}
