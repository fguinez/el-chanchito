import { NextRequest, NextResponse } from "next/server";
import { and, count, desc, eq, sql } from "drizzle-orm";
import { db } from "@/lib/db";
import { products, transactions } from "@/lib/db/schema";
import { notDeleted } from "@/lib/db/transactions";
import {
  failureBody,
  normalizeMatchDescription,
  normalizeMatchType,
} from "@/lib/aliases";

const PREVIEW_LIMIT = 20;

/**
 * GET /api/aliases/preview?matchDescription=...&matchType=exact|substring: the
 * transactions an alias with this match would relabel ("probar alias"), with the
 * same case-insensitive semantics as `alias_for_transaction` (V035): exact =
 * trimmed whole-string equality, substring = literal strpos. Matching keys on
 * the RAW scraped `description` (what the alias replaces), never the resolved
 * one. Returns `{ matchDescription, matchType, total, transactions }`;
 * `transactions` lists up to 20 matches, newest first.
 */
export async function GET(request: NextRequest) {
  try {
    const matchDescription = normalizeMatchDescription(
      request.nextUrl.searchParams.get("matchDescription")
    );
    if (!matchDescription.ok) {
      return NextResponse.json(failureBody(matchDescription), {
        status: matchDescription.status,
      });
    }

    const matchType = normalizeMatchType(
      request.nextUrl.searchParams.get("matchType")
    );
    if (!matchType.ok) {
      return NextResponse.json(failureBody(matchType), {
        status: matchType.status,
      });
    }

    // Mirrors alias_for_transaction's predicate on the raw scraped description.
    // A soft-deleted row (V033) is never a match the alias could relabel.
    const predicate =
      matchType.value === "exact"
        ? sql`btrim(lower(${transactions.description})) = btrim(lower(${matchDescription.value}))`
        : sql`strpos(lower(${transactions.description}), lower(${matchDescription.value})) > 0`;
    const matches = and(predicate, notDeleted);

    const [[counts], rows] = await Promise.all([
      db
        .select({ total: count() })
        .from(transactions)
        .where(matches),
      db
        .select({
          id: transactions.id,
          // The RAW scraped text: what is matched and what the alias replaces.
          description: transactions.description,
          amount: transactions.amount,
          // `amount` is in this currency (V023): USD rows keep their cents.
          currency: products.currency,
          transactionDate: transactions.transactionDate,
          customDescription: transactions.customDescription,
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
      matchDescription: matchDescription.value,
      matchType: matchType.value,
      total: counts.total,
      transactions: rows,
    });
  } catch (error) {
    console.error("GET /api/aliases/preview failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}
