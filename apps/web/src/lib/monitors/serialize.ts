// API serialization helpers for monitors: enrich rows with display-form
// expressions, replay trailing history windows into sparklines, and describe
// each product reference for the detail view. Pure (no db, no next imports);
// the routes fetch, these shape.

import { METRIC_FIELDS } from "@chanchito/product-model";
import type { ClpRates } from "@/lib/rates";
import {
  ExprError,
  collectRefs,
  parseExpression,
  serializeExpression,
  type ProductCatalog,
  type RefExpr,
} from "./expr";
import type { ProductInfo } from "./evaluate";
import {
  formatLocalDate,
  replayHistory,
  type HistoryPoint,
  type SnapshotRow,
} from "./history";
import type {
  MonitorDefinition,
  MonitorDisplay,
  MonitorEvaluation,
  MonitorThreshold,
} from "./types";

/** The columns enrichment reads; both DB rows (with id and timestamps, all
 *  spread through) and id-less preview payloads satisfy it. */
type MonitorRowCore = {
  currency: string;
  expression: string;
  thresholds: MonitorThreshold[];
  display: MonitorDisplay;
};

/** Stored (uuid-ref) source rendered in display form. A corrupt stored
 *  expression falls back to its raw source so the breakage stays visible
 *  instead of failing the whole response. */
function toDisplayExpression(source: string, catalog: ProductCatalog): string {
  try {
    return serializeExpression(parseExpression(source), "display", catalog);
  } catch (e) {
    if (e instanceof ExprError) return source;
    throw e;
  }
}

export type SparklinePoint = {
  /** Calendar date, YYYY-MM-DD. */
  date: string;
  value: number | null;
  /** The alert threshold's value that day; null when its inputs are no-data. */
  alertThreshold: number | null;
};

/**
 * The API shape of a monitor: every row column plus `displayExpression` on
 * the monitor and on each threshold, and optionally the current `evaluation`
 * and a `sparkline` (list view, line charts only).
 */
export function enrichMonitor<Row extends MonitorRowCore>(
  row: Row,
  catalog: ProductCatalog,
  evaluation?: MonitorEvaluation,
  sparkline?: SparklinePoint[]
) {
  return {
    ...row,
    displayExpression: toDisplayExpression(row.expression, catalog),
    thresholds: row.thresholds.map((t) => ({
      ...t,
      displayExpression: toDisplayExpression(t.expression, catalog),
    })),
    ...(evaluation !== undefined && { evaluation }),
    ...(sparkline !== undefined && { sparkline }),
  };
}

/** Distinct (product, field) references across the left expression and every
 *  threshold, in source order. Unbound refs and unparseable stored
 *  expressions contribute nothing (they surface as no-data / broken refs). */
export function collectMonitorRefs(def: MonitorDefinition): RefExpr[] {
  const sources = [def.expression, ...def.thresholds.map((t) => t.expression)];
  const seen = new Set<string>();
  const refs: RefExpr[] = [];
  for (const source of sources) {
    let refsInSource: RefExpr[];
    try {
      refsInSource = collectRefs(parseExpression(source));
    } catch (e) {
      if (e instanceof ExprError) continue;
      throw e;
    }
    for (const ref of refsInSource) {
      if (ref.productId == null) continue;
      const key = `${ref.productId}:${ref.field}`;
      if (seen.has(key)) continue;
      seen.add(key);
      refs.push(ref);
    }
  }
  return refs;
}

/** Product ids referenced anywhere in the monitor (for snapshot queries). */
export function referencedProductIds(def: MonitorDefinition): string[] {
  return [...new Set(collectMonitorRefs(def).map((ref) => ref.productId!))];
}

export type ReplayWindowOptions = {
  snapshots: SnapshotRow[];
  products: Map<string, ProductInfo>;
  rates: ClpRates;
  now: Date;
};

/** Replay the trailing `days`-day window ending at `now` (inclusive). */
export function replayWindow(
  def: MonitorDefinition,
  opts: ReplayWindowOptions,
  days: number
): HistoryPoint[] {
  const from = new Date(opts.now);
  from.setDate(from.getDate() - (days - 1));
  return replayHistory(def, {
    snapshots: opts.snapshots,
    products: opts.products,
    rates: opts.rates,
    from: formatLocalDate(from),
    to: formatLocalDate(opts.now),
  });
}

/** Last 30 days of `{ date, value, alertThreshold }` for the list view. */
export function buildSparkline(
  def: MonitorDefinition,
  opts: ReplayWindowOptions
): SparklinePoint[] {
  return replayWindow(def, opts, 30).map((point) => ({
    date: point.date,
    value: point.value,
    alertThreshold:
      point.thresholds.find((t) => t.severity === "alert")?.value ?? null,
  }));
}

export type MonitorReference = {
  productId: string;
  institutionSlug: string | null;
  productSlug: string | null;
  field: string;
  name: string | null;
  currency: string | null;
  /** The ref's field value in the product's own currency (unconverted). */
  latestValue: number | null;
  balanceAsOf: string | null;
  /** Product gone from the catalog, field invalid for its kind, or inactive. */
  broken: boolean;
};

/** One row per distinct reference, for the detail view's references table. */
export function buildReferences(
  def: MonitorDefinition,
  products: Map<string, ProductInfo>
): MonitorReference[] {
  return collectMonitorRefs(def).map((ref) => {
    const productId = ref.productId!;
    const product = products.get(productId);
    if (!product) {
      return {
        productId,
        institutionSlug: null,
        productSlug: null,
        field: ref.field,
        name: null,
        currency: null,
        latestValue: null,
        balanceAsOf: null,
        broken: true,
      };
    }

    const fieldValid =
      ref.field === "current_balance" ||
      ref.field in METRIC_FIELDS[product.kind];

    let latestValue: number | null = null;
    if (fieldValid) {
      if (ref.field === "current_balance") {
        latestValue = product.currentBalance;
      } else if (product.metrics != null) {
        const raw = (product.metrics as unknown as Record<string, unknown>)[
          ref.field
        ];
        latestValue = typeof raw === "number" ? raw : null;
      }
    }

    return {
      productId,
      institutionSlug: product.institutionSlug,
      productSlug: product.slug,
      field: ref.field,
      name: product.name,
      currency: product.currency,
      latestValue,
      balanceAsOf: product.balanceAsOf
        ? product.balanceAsOf.toISOString()
        : null,
      broken: !fieldValid || !product.isActive,
    };
  });
}
