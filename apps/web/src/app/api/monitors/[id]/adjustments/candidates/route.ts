import { NextRequest, NextResponse } from "next/server";
import { and, desc, eq, gte, inArray, lt } from "drizzle-orm";
import { db } from "@/lib/db";
import {
  accounts,
  institutions,
  monitorAdjustments,
  monitors,
  products,
  transactions,
} from "@/lib/db/schema";
import { displayDescription, notDeleted } from "@/lib/db/transactions";
import { adjustmentCandidatesQuerySchema } from "@/lib/api/schemas";
import { parseSearchParams } from "@/lib/api/validation";
import { formatLocalDate } from "@/lib/monitors/dates";
import { monitorProductIds } from "@/lib/monitors/effects";
import { UUID_RE } from "@/lib/monitors/validate";
import { displayProductName } from "@/lib/product-names";
import { normalizeText } from "@/lib/transfer-suggestions";

// Next 16: dynamic segment params arrive as a Promise on the context arg.
type Context = { params: Promise<{ id: string }> };

export type AdjustmentCandidate = {
  id: string;
  description: string;
  transactionDate: string;
  amount: number;
  productId: string;
  productName: string;
  productCurrency: string;
  institutionSlug: string;
  institutionName: string;
  /** Already a variación of this monitor. */
  linked: boolean;
};

/** First day of the month after a YYYY-MM month, as YYYY-MM-DD. */
function nextMonthStart(month: string): string {
  const [year, monthNumber] = month.split("-").map(Number);
  return formatLocalDate(new Date(year, monthNumber, 1));
}

/**
 * GET /api/monitors/[id]/adjustments/candidates: transactions that can become
 * variaciones of this monitor, i.e. visible transactions of the products its
 * left expression or a threshold references, in `?month=YYYY-MM` (default: the current local
 * month), newest first, at most `?limit=` (default 50). `?q=` keeps those
 * whose display description or scraped text contains it, ignoring case and
 * accents (a search without letters also matches the amount's digits).
 * Already linked ones come flagged `linked`. `productIds` lists the
 * referenced products, so an empty one means the monitor reads none.
 */
export async function GET(request: NextRequest, { params }: Context) {
  try {
    const { id } = await params;
    if (!UUID_RE.test(id)) {
      return NextResponse.json({ error: "Invalid id" }, { status: 400 });
    }
    const query = parseSearchParams(
      request.nextUrl.searchParams,
      adjustmentCandidatesQuerySchema
    );
    if (!query.ok) return query.response;
    const month = query.data.month ?? formatLocalDate(new Date()).slice(0, 7);

    const [monitor] = await db
      .select({ expression: monitors.expression, thresholds: monitors.thresholds })
      .from(monitors)
      .where(eq(monitors.id, id));
    if (!monitor) {
      return NextResponse.json({ error: "Not found" }, { status: 404 });
    }

    const productIds = monitorProductIds(monitor);
    if (productIds.length === 0) {
      return NextResponse.json({ month, productIds, candidates: [] });
    }

    const [rows, links] = await Promise.all([
      db
        .select({
          id: transactions.id,
          description: displayDescription,
          rawDescription: transactions.description,
          transactionDate: transactions.transactionDate,
          amount: transactions.amount,
          productId: transactions.productId,
          productName: products.name,
          productKind: products.kind,
          productCurrency: products.currency,
          institutionSlug: institutions.slug,
          institutionName: institutions.name,
        })
        .from(transactions)
        .innerJoin(products, eq(transactions.productId, products.id))
        .innerJoin(accounts, eq(products.accountId, accounts.id))
        .innerJoin(institutions, eq(accounts.institutionId, institutions.id))
        .where(
          and(
            notDeleted,
            inArray(transactions.productId, productIds),
            gte(transactions.transactionDate, `${month}-01`),
            lt(transactions.transactionDate, nextMonthStart(month))
          )
        )
        .orderBy(desc(transactions.transactionDate), desc(transactions.createdAt)),
      db
        .select({ transactionId: monitorAdjustments.transactionId })
        .from(monitorAdjustments)
        .where(eq(monitorAdjustments.monitorId, id)),
    ]);

    // A month of a few products' rows is small: search in memory, where
    // accents fold without a Postgres extension.
    const q = query.data.q ?? "";
    const needle = normalizeText(q);
    // A search without letters ("2.500", "-15000") also matches amounts.
    const digits = /\p{L}/u.test(q) ? "" : q.replace(/\D/g, "");
    const linked = new Set(links.map((link) => link.transactionId));
    const candidates: AdjustmentCandidate[] = [];
    for (const row of rows) {
      if (candidates.length >= query.data.limit) break;
      if (
        needle !== "" &&
        !normalizeText(row.description).includes(needle) &&
        !normalizeText(row.rawDescription).includes(needle) &&
        !(
          digits !== "" &&
          String(Math.abs(row.amount)).replace(/\D/g, "").includes(digits)
        )
      ) {
        continue;
      }
      candidates.push({
        id: row.id,
        description: row.description,
        transactionDate: row.transactionDate,
        amount: row.amount,
        productId: row.productId,
        productName: displayProductName(
          {
            name: row.productName,
            kind: row.productKind,
            currency: row.productCurrency,
          },
          row.institutionName
        ),
        productCurrency: row.productCurrency,
        institutionSlug: row.institutionSlug,
        institutionName: row.institutionName,
        linked: linked.has(row.id),
      });
    }
    return NextResponse.json({ month, productIds, candidates });
  } catch (error) {
    console.error("GET /api/monitors/[id]/adjustments/candidates failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}
