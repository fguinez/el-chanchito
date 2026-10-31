import { NextRequest, NextResponse } from "next/server";
import { and, count, desc, eq, sql } from "drizzle-orm";
import { db } from "@/lib/db";
import { products, transactions } from "@/lib/db/schema";
import {
  descriptionMatches,
  displayDescription,
  notDeleted,
} from "@/lib/db/transactions";
import { searchOnlyQuerySchema } from "@/lib/api/schemas";
import { parseSearchParams } from "@/lib/api/validation";
import {
  amountConditionError,
  failureBody,
  normalizeAmountBound,
  normalizeCurrency,
  normalizeMatchDescription,
  normalizeMatchType,
} from "@/lib/aliases";
import type { SQL } from "drizzle-orm";

const PREVIEW_LIMIT = 20;

/**
 * GET /api/aliases/preview?matchDescription=...&matchType=exact|substring
 * (&amountMin=&amountMax=&currency=&q=): the transactions an alias with this
 * condition would relabel ("probar alias"), with the same semantics as
 * `alias_for_transaction` (V035/V036): case-insensitive description match (exact
 * = trimmed whole-string equality, substring = literal strpos) plus the optional
 * amount range on the signed amount, scoped to `currency`. Matching keys on the
 * RAW scraped `description` (what the alias replaces), never the resolved one.
 * `q`, the preview's search box, then narrows the matches to those with every
 * word in the raw or the display description (see lib/transaction-search).
 * Returns `{ matchDescription, matchType, total, transactions }`: `total`
 * counts the (narrowed) matches and `transactions` lists up to 20, newest
 * first, each with its `displayDescription` too.
 */
export async function GET(request: NextRequest) {
  try {
    const params = request.nextUrl.searchParams;
    const matchDescription = normalizeMatchDescription(
      params.get("matchDescription")
    );
    if (!matchDescription.ok) {
      return NextResponse.json(failureBody(matchDescription), {
        status: matchDescription.status,
      });
    }

    const matchType = normalizeMatchType(params.get("matchType"));
    if (!matchType.ok) {
      return NextResponse.json(failureBody(matchType), {
        status: matchType.status,
      });
    }

    const amountMin = normalizeAmountBound(params.get("amountMin"), "amountMin");
    if (!amountMin.ok) {
      return NextResponse.json(failureBody(amountMin), {
        status: amountMin.status,
      });
    }
    const amountMax = normalizeAmountBound(params.get("amountMax"), "amountMax");
    if (!amountMax.ok) {
      return NextResponse.json(failureBody(amountMax), {
        status: amountMax.status,
      });
    }
    const currency = normalizeCurrency(params.get("currency"));
    if (!currency.ok) {
      return NextResponse.json(failureBody(currency), {
        status: currency.status,
      });
    }

    // Same cross-field rule a create would enforce, so the preview reflects what
    // would actually be stored (a bound without a currency is a 400, not silent).
    const conditionError = amountConditionError({
      amountMin: amountMin.value,
      amountMax: amountMax.value,
      currency: currency.value,
    });
    if (conditionError) {
      return NextResponse.json(failureBody(conditionError), {
        status: conditionError.status,
      });
    }
    const search = parseSearchParams(params, searchOnlyQuerySchema);
    if (!search.ok) return search.response;

    // Mirrors alias_for_transaction's predicate on the raw scraped description.
    // A soft-deleted row (V033) is never a match the alias could relabel.
    const predicate =
      matchType.value === "exact"
        ? sql`btrim(lower(${transactions.description})) = btrim(lower(${matchDescription.value}))`
        : sql`strpos(lower(${transactions.description}), lower(${matchDescription.value})) > 0`;
    // The amount/currency filters mirror the function's: amount bounds on the
    // signed amount, currency scoped to products.currency (joined below).
    const conditions: SQL[] = [predicate, notDeleted];
    if (amountMin.value !== null) {
      conditions.push(sql`${transactions.amount} >= ${amountMin.value}`);
    }
    if (amountMax.value !== null) {
      conditions.push(sql`${transactions.amount} <= ${amountMax.value}`);
    }
    if (currency.value !== null) {
      conditions.push(sql`${products.currency} = ${currency.value}`);
    }
    // The search reads the resolved description too (it needs the products
    // join below), but never decides what the alias matches.
    const searched = descriptionMatches(search.data.q);
    if (searched) conditions.push(searched);
    const matches = and(...conditions);

    const [[counts], rows] = await Promise.all([
      // Joins products so the currency filter and the search can reference
      // products.currency; every transaction has a product, so the join never
      // changes the count.
      db
        .select({ total: count() })
        .from(transactions)
        .innerJoin(products, eq(transactions.productId, products.id))
        .where(matches),
      db
        .select({
          id: transactions.id,
          // The RAW scraped text: what is matched and what the alias replaces.
          description: transactions.description,
          // What the row shows today, for the preview's tooltip.
          displayDescription,
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
