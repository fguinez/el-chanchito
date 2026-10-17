import { NextRequest, NextResponse } from "next/server";
import { db } from "@/lib/db";
import { failureBody } from "@/lib/api/validation";
import {
  institutions,
  accounts,
  products,
  productSnapshots,
  transactions,
} from "@/lib/db/schema";
import { and, eq, desc } from "drizzle-orm";
import { rateInfoFor, ratesFor, toClp } from "@/lib/rates";
import { getRateBook } from "@/lib/rate-book";
import { isRetiredGhost } from "@/lib/retired-products";
import {
  isUniqueViolation,
  planProductUpdate,
  validateProductPatch,
} from "@/lib/management";
import type { ProductKind, ProductMetrics } from "@/lib/db/schema";
import { formatLocationText } from "@/lib/transaction-location";

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
  /** Balance converted to CLP with the product's current rate (see
   *  `product.exchangeRate`); null when unconvertible. */
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
  /** Source dedup key (the institution's operation id for some sources). */
  externalId: string | null;
  /** "Lugar · Ciudad, País" when a location is stored (V025), else null. */
  location: string | null;
  /** Linked to an internal transfer: left out of Ingresos/Gastos, still part
   *  of the balance. */
  isInternalTransfer: boolean;
}

/** The product this one hangs from (debit card -> checking, línea -> cta. cte.). */
export interface ProductParent {
  id: string;
  institutionSlug: string;
  slug: string;
  name: string;
  kind: ProductKind;
  currency: string;
}

/**
 * GET /api/institutions/[slug]/products/[product] — one product by its
 * institution-unique slug, with its balance-over-time history from
 * `product_snapshots` (most recent first, capped at MAX_HISTORY_POINTS) and
 * its latest MAX_TRANSACTIONS transactions (`transactionsTruncated` says
 * whether older ones were left out). `product.exchangeRate` is the rate its
 * CLP figures use (institution/product override or provider, with staleness;
 * see lib/rates). Returns 404 when the institution or the product is unknown.
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
      productDefaultName: products.defaultName,
      productSlug: products.slug,
      currency: products.currency,
      currentBalance: products.currentBalance,
      balanceAsOf: products.balanceAsOf,
      externalRef: products.externalRef,
      attributes: products.attributes,
      metrics: products.metrics,
      isActive: products.isActive,
      displayOrder: products.displayOrder,
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

  const [snapshots, txns, book, parent] = await Promise.all([
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
        externalId: transactions.externalId,
        isInternalTransfer: transactions.isInternalTransfer,
        locationName: transactions.locationName,
        locationCity: transactions.locationCity,
        locationCountry: transactions.locationCountry,
      })
      .from(transactions)
      .where(eq(transactions.productId, row.productId))
      // createdAt breaks same-day ties so the order is stable across loads.
      .orderBy(
        desc(transactions.transactionDate),
        desc(transactions.createdAt)
      )
      // One extra row tells whether the cap dropped anything.
      .limit(MAX_TRANSACTIONS + 1),
    getRateBook(),
    row.parentProductId ? loadParent(row.parentProductId) : null,
  ]);
  const target = {
    productId: row.productId,
    institutionSlug: row.institutionSlug,
  };
  const rates = ratesFor(book, target);

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
  const transactionsTruncated = txns.length > MAX_TRANSACTIONS;
  const productTransactions: ProductTransaction[] = txns
    .slice(0, MAX_TRANSACTIONS)
    .reverse()
    .map((t) => ({
      id: t.id,
      description: t.description,
      amount: Number(t.amount),
      transactionDate: new Date(t.transactionDate).toISOString().slice(0, 10),
      source: t.source,
      externalId: t.externalId,
      isInternalTransfer: t.isInternalTransfer,
      location: formatLocationText({
        name: t.locationName,
        city: t.locationCity,
        country: t.locationCountry,
      }),
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
      defaultName: row.productDefaultName,
      slug: row.productSlug,
      currency: row.currency,
      currentBalance,
      currentBalanceClp:
        currentBalance != null
          ? toClp(row.currency, currentBalance, rates)
          : null,
      exchangeRate: rateInfoFor(
        book,
        { ...target, currency: row.currency },
        new Date()
      ),
      balanceAsOf: row.balanceAsOf ? row.balanceAsOf.toISOString() : null,
      externalRef: row.externalRef,
      attributes: row.attributes,
      metrics: row.metrics,
      isActive: row.isActive,
      displayOrder: row.displayOrder,
    },
    parent,
    history,
    transactions: productTransactions,
    transactionsTruncated,
  });
}

/** The parent product's identity for the detail page, or null when it no
 *  longer resolves (or is a retired ghost, which the UI treats as gone). */
async function loadParent(parentId: string): Promise<ProductParent | null> {
  const [parent] = await db
    .select({
      id: products.id,
      institutionSlug: institutions.slug,
      slug: products.slug,
      name: products.name,
      kind: products.kind,
      currency: products.currency,
      isActive: products.isActive,
      currentBalance: products.currentBalance,
    })
    .from(products)
    .innerJoin(accounts, eq(products.accountId, accounts.id))
    .innerJoin(institutions, eq(accounts.institutionId, institutions.id))
    .where(eq(products.id, parentId))
    .limit(1);
  if (!parent || isRetiredGhost(parent)) return null;
  return {
    id: parent.id,
    institutionSlug: parent.institutionSlug,
    slug: parent.slug,
    name: parent.name,
    kind: parent.kind,
    currency: parent.currency,
  };
}

/**
 * PATCH /api/institutions/[slug]/products/[product]: edit a product's display
 * metadata: `name`, `slug`, `parentProductId`, `isActive`, `displayOrder`
 * (see lib/management for the rules). `name: null` resets the name to the
 * product's `defaultName` (the scrapers' name, which it then follows again on
 * later scrapes); a blank string is a 400. A rename never touches the slug; a new
 * slug moves the product's page URL, while monitors keep working since they
 * reference products by uuid. Returns the product's new identity; 400 for a
 * bad body, 404 for an unknown or retired product, 409 for a taken slug or a
 * deactivation that would hide the product.
 */
export async function PATCH(request: NextRequest, { params }: Context) {
  try {
    const { slug, product } = await params;

    let body: unknown;
    try {
      body = await request.json();
    } catch {
      return NextResponse.json({ error: "Invalid JSON body" }, { status: 400 });
    }
    const patch = validateProductPatch(body);
    if (!patch.ok) {
      return NextResponse.json(failureBody(patch), { status: patch.status });
    }

    const result = await db.transaction(async (tx) => {
      // Locking the institution serializes edits across its products, so two
      // concurrent PATCHes can't claim one slug or link two products into a
      // loop; locking the product keeps its balance (the deactivation rule)
      // steady against a scrape landing mid-edit. NO KEY UPDATE does both
      // without blocking the scraper's inserts that reference these rows.
      const [institution] = await tx
        .select({ id: institutions.id })
        .from(institutions)
        .where(eq(institutions.slug, slug))
        .for("no key update");
      if (!institution) return null;

      const [row] = await tx
        .select({
          id: products.id,
          slug: products.slug,
          defaultName: products.defaultName,
          parentProductId: products.parentProductId,
          isActive: products.isActive,
          currentBalance: products.currentBalance,
        })
        .from(products)
        .innerJoin(accounts, eq(products.accountId, accounts.id))
        .where(
          and(
            eq(accounts.institutionId, institution.id),
            eq(products.slug, product)
          )
        )
        .limit(1)
        .for("no key update", { of: products });
      if (!row || isRetiredGhost(row)) return null;

      // Every product of the institution: slugs are unique across all of its
      // accounts, and parents may live under any of them.
      const institutionProducts = await tx
        .select({
          id: products.id,
          slug: products.slug,
          parentProductId: products.parentProductId,
          isActive: products.isActive,
          currentBalance: products.currentBalance,
        })
        .from(products)
        .innerJoin(accounts, eq(products.accountId, accounts.id))
        .where(eq(accounts.institutionId, institution.id));

      const plan = planProductUpdate(row, patch.value, institutionProducts);
      if (!plan.ok) return plan;

      const [updated] = await tx
        .update(products)
        .set({ ...plan.value, updatedAt: new Date() })
        .where(eq(products.id, row.id))
        .returning({
          id: products.id,
          slug: products.slug,
          name: products.name,
          defaultName: products.defaultName,
          parentProductId: products.parentProductId,
          isActive: products.isActive,
          displayOrder: products.displayOrder,
        });
      return { ok: true as const, value: updated };
    });

    if (!result) {
      return NextResponse.json({ error: "Not found" }, { status: 404 });
    }
    if (!result.ok) {
      return NextResponse.json(failureBody(result), { status: result.status });
    }
    return NextResponse.json({
      institutionSlug: slug,
      product: result.value,
    });
  } catch (error) {
    // The (account_id, slug) index backstops the institution-wide check
    // against the scraper writer minting the same slug mid-edit.
    if (isUniqueViolation(error, "uq_products_account_slug")) {
      return NextResponse.json(
        {
          error: "That slug is already used by another product of this institution",
          field: "slug",
        },
        { status: 409 }
      );
    }
    console.error(
      "PATCH /api/institutions/[slug]/products/[product] failed:",
      error
    );
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}
