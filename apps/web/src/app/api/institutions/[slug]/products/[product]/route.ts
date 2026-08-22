import { NextResponse } from "next/server";
import { db } from "@/lib/db";
import {
  institutions,
  accounts,
  products,
  productSnapshots,
  transactions,
} from "@/lib/db/schema";
import { and, eq, desc } from "drizzle-orm";
import { getClpRates, toClp } from "@/lib/rates";
import { isRetiredGhost } from "@/lib/retired-products";
import type { ProductMetrics } from "@/lib/db/schema";

// Next 16: dynamic segment params arrive as a Promise on the context arg.
type Context = { params: Promise<{ slug: string; product: string }> };

/** Cap the returned history so a long-lived product can't bloat the page. */
const MAX_HISTORY_POINTS = 500;
/** Cap the returned transactions (most recent kept). */
const MAX_TRANSACTIONS = 500;

export interface ProductHistoryPoint {
  /** ISO timestamp of the observation. */
  asOf: string;
  /** Balance in the product's own currency. */
  balance: number;
  /** Balance converted to CLP with current rates; null when unconvertible. */
  balanceClp: number | null;
  /** Full typed metrics payload at `asOf` (empty object when unknown). */
  metrics: ProductMetrics | Record<string, never>;
}

export interface ProductTransaction {
  id: string;
  description: string;
  /** Signed amount in the product's currency: negative = expense, positive = income. */
  amount: number;
  /** Plain YYYY-MM-DD. */
  transactionDate: string;
  source: string;
}

/**
 * GET /api/institutions/[slug]/products/[product] — one product by its
 * institution-unique slug, with its balance-over-time history from
 * `product_snapshots` (most recent first, capped at MAX_HISTORY_POINTS).
 * Returns 404 when the institution or the product is unknown.
 */
export async function GET(_request: Request, { params }: Context) {
  const { slug, product } = await params;

  const [row] = await db
    .select({
      institutionId: institutions.id,
      institutionSlug: institutions.slug,
      institutionName: institutions.name,
      institutionKind: institutions.kind,
      institutionCountry: institutions.country,
      institutionUrl: institutions.url,
      accountId: accounts.id,
      accountName: accounts.name,
      productId: products.id,
      parentProductId: products.parentProductId,
      kind: products.kind,
      productName: products.name,
      productSlug: products.slug,
      currency: products.currency,
      currentBalance: products.currentBalance,
      balanceAsOf: products.balanceAsOf,
      externalRef: products.externalRef,
      attributes: products.attributes,
      metrics: products.metrics,
      isActive: products.isActive,
    })
    .from(products)
    .innerJoin(accounts, eq(products.accountId, accounts.id))
    .innerJoin(institutions, eq(accounts.institutionId, institutions.id))
    .where(and(eq(institutions.slug, slug), eq(products.slug, product)))
    .limit(1);

  // A retired roll-up ghost is kept in the database but has no data left; the
  // UI treats it as nonexistent (see lib/retired-products), so its direct URL
  // gets the same 404 as an unknown slug instead of an empty dashboard.
  if (!row || isRetiredGhost(row)) {
    return NextResponse.json({ error: "Not found" }, { status: 404 });
  }

  const [snapshots, txns, rates] = await Promise.all([
    db
      .select({
        balance: productSnapshots.balance,
        metrics: productSnapshots.metrics,
        asOf: productSnapshots.asOf,
      })
      .from(productSnapshots)
      .where(eq(productSnapshots.productId, row.productId))
      .orderBy(desc(productSnapshots.asOf))
      .limit(MAX_HISTORY_POINTS),
    db
      .select({
        id: transactions.id,
        description: transactions.description,
        amount: transactions.amount,
        transactionDate: transactions.transactionDate,
        source: transactions.source,
      })
      .from(transactions)
      .where(eq(transactions.productId, row.productId))
      .orderBy(desc(transactions.transactionDate))
      .limit(MAX_TRANSACTIONS),
    getClpRates(),
  ]);

  const history: ProductHistoryPoint[] = snapshots
    // Most recent first makes the cap drop the oldest observations.
    .slice()
    .reverse()
    .map((s) => {
      const balance = Number(s.balance);
      return {
        asOf: s.asOf.toISOString(),
        balance,
        balanceClp: toClp(row.currency, balance, rates),
        metrics: s.metrics,
      };
    });

  // Most recent first makes the cap drop the oldest transactions; returned
  // ascending so the client can walk the sequence forward.
  const productTransactions: ProductTransaction[] = txns
    .slice()
    .reverse()
    .map((t) => ({
      id: t.id,
      description: t.description,
      amount: Number(t.amount),
      transactionDate: new Date(t.transactionDate).toISOString().slice(0, 10),
      source: t.source,
    }));

  const currentBalance =
    row.currentBalance != null ? Number(row.currentBalance) : null;

  return NextResponse.json({
    institution: {
      id: row.institutionId,
      slug: row.institutionSlug,
      name: row.institutionName,
      kind: row.institutionKind,
      country: row.institutionCountry,
      url: row.institutionUrl,
    },
    product: {
      id: row.productId,
      accountId: row.accountId,
      accountName: row.accountName,
      parentProductId: row.parentProductId,
      kind: row.kind,
      name: row.productName,
      slug: row.productSlug,
      currency: row.currency,
      currentBalance,
      currentBalanceClp:
        currentBalance != null
          ? toClp(row.currency, currentBalance, rates)
          : null,
      balanceAsOf: row.balanceAsOf ? row.balanceAsOf.toISOString() : null,
      externalRef: row.externalRef,
      attributes: row.attributes,
      metrics: row.metrics,
      isActive: row.isActive,
    },
    history,
    transactions: productTransactions,
  });
}
