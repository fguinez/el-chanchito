// DB-aware loaders for the monitors API: the product catalog the expression
// engine resolves references against, and the snapshot rows history replay
// consumes. This is the only monitors module that touches the database; the
// engine and the validators stay pure and unit-testable.

import { asc, eq, inArray } from "drizzle-orm";
import { db } from "@/lib/db";
import {
  accounts,
  institutions,
  products,
  productSnapshots,
  type ProductMetrics,
} from "@/lib/db/schema";
import type { ProductInfo } from "./evaluate";
import type { SnapshotRow } from "./history";

export type LoadedProductCatalog = {
  byId: Map<string, ProductInfo>;
  /** Keyed by `${institutionSlug}:${productSlug}`. */
  bySlug: Map<string, ProductInfo>;
};

/** Typed metrics, or null for legacy `{}` payloads (same narrowing as
 *  /api/wealth: a payload without `kind` predates typed observations). */
function narrowMetrics(
  metrics: ProductMetrics | null
): ProductMetrics | null {
  return metrics != null && "kind" in metrics
    ? (metrics as ProductMetrics)
    : null;
}

/**
 * Every product (inactive ones included: the engine turns those into no-data,
 * and serializers need their identity for broken-reference reporting), joined
 * to its account and institution for the display-form slug pair.
 */
export async function loadProductCatalog(): Promise<LoadedProductCatalog> {
  const rows = await db
    .select({
      id: products.id,
      kind: products.kind,
      currency: products.currency,
      isActive: products.isActive,
      metrics: products.metrics,
      balanceAsOf: products.balanceAsOf,
      slug: products.slug,
      institutionSlug: institutions.slug,
      name: products.name,
    })
    .from(products)
    .innerJoin(accounts, eq(products.accountId, accounts.id))
    .innerJoin(institutions, eq(accounts.institutionId, institutions.id));

  const byId = new Map<string, ProductInfo>();
  const bySlug = new Map<string, ProductInfo>();
  for (const row of rows) {
    const info: ProductInfo = {
      id: row.id,
      kind: row.kind,
      currency: row.currency,
      isActive: row.isActive,
      metrics: narrowMetrics(row.metrics),
      balanceAsOf: row.balanceAsOf,
      slug: row.slug,
      institutionSlug: row.institutionSlug,
      name: row.name,
    };
    byId.set(info.id, info);
    bySlug.set(`${info.institutionSlug}:${info.slug}`, info);
  }
  return { byId, bySlug };
}

/**
 * All snapshot rows for the given products, oldest first, ready for
 * replayHistory. Loading the full history (no date cutoff) keeps the replay's
 * pre-window warm-up correct even for rarely-scraped products, and matches
 * /api/wealth, which already loads every snapshot; per-product snapshot
 * counts are small (one row per metrics change).
 */
export async function loadSnapshotsForProducts(
  productIds: string[]
): Promise<SnapshotRow[]> {
  if (productIds.length === 0) return [];
  const rows = await db
    .select({
      productId: productSnapshots.productId,
      metrics: productSnapshots.metrics,
      asOf: productSnapshots.asOf,
    })
    .from(productSnapshots)
    .where(inArray(productSnapshots.productId, productIds))
    .orderBy(asc(productSnapshots.asOf));

  return rows.map((row) => ({
    productId: row.productId,
    metrics: row.metrics,
    asOf: row.asOf,
  }));
}
