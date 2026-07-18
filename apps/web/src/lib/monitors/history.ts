// Monitor history replayed from product snapshots, mirroring the
// carry-forward pattern of /api/wealth: group snapshots per calendar date,
// walk the range chronologically carrying each product's latest observation
// forward, and evaluate the monitor (left side + thresholds) on every day
// with that day's date, so DAY_OF_MONTH ramps reset month by month.
//
// Known v1 approximation (same as /api/wealth): only current rates are
// available, so past days convert at today's prices.

import type { ProductMetrics } from "@chanchito/product-model";
import type { ClpRates } from "@/lib/rates";
import { evaluateMonitor, type ProductInfo } from "./evaluate";
import type {
  MonitorDefinition,
  MonitorStatus,
  ThresholdEvaluation,
} from "./types";

/** One product_snapshots row, as queried (metrics may be a legacy `{}`). */
export type SnapshotRow = {
  productId: string;
  balance: number;
  metrics: ProductMetrics | Record<string, never> | null;
  asOf: Date;
};

export type HistoryPoint = {
  /** Calendar date, YYYY-MM-DD. */
  date: string;
  value: number | null;
  status: MonitorStatus;
  margin: number | null;
  thresholds: ThresholdEvaluation[];
};

export type ReplayOptions = {
  snapshots: SnapshotRow[];
  /** Current product rows; per-day balance/metrics are overlaid from snapshots. */
  products: Map<string, ProductInfo>;
  rates: ClpRates;
  /** YYYY-MM-DD inclusive; defaults to the first snapshot date. */
  from?: string;
  /** YYYY-MM-DD inclusive; defaults to the last snapshot date. */
  to?: string;
};

/** A snapshot's typed metrics, or null for rows that predate them (`{}`). */
function snapshotMetrics(
  metrics: ProductMetrics | Record<string, never> | null
): ProductMetrics | null {
  return metrics != null && "kind" in metrics
    ? (metrics as ProductMetrics)
    : null;
}

const DAY_MS = 24 * 60 * 60 * 1000;

/** UTC-midnight milliseconds for a YYYY-MM-DD string. Day stepping happens in
 *  UTC because UTC days are always exactly 24h; local-midnight stepping can
 *  drop or mislabel a day when DST shifts at local midnight (America/Santiago). */
function toUtcMs(dateStr: string): number {
  const [year, month, day] = dateStr.split("-").map(Number);
  return Date.UTC(year, month - 1, day);
}

/** Noon-local Date for a YYYY-MM-DD day key: DST jumps happen around midnight,
 *  never at noon, so the LOCAL parts read by DAY_OF_MONTH/DAYS_IN_MONTH (see
 *  evaluate.ts) always match the day key used to group snapshots. */
function toEvalDate(dateStr: string): Date {
  const [year, month, day] = dateStr.split("-").map(Number);
  return new Date(year, month - 1, day, 12);
}

/** YYYY-MM-DD from a Date's local parts (the replay window's day unit). */
export function formatLocalDate(date: Date): string {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

/**
 * Replay a monitor over its snapshot history: one point per calendar day in
 * [from, to]. Days before a referenced product has any snapshot yield
 * `value: null` (status no_data); thresholds are still recomputed per day.
 */
export function replayHistory(
  def: MonitorDefinition,
  opts: ReplayOptions
): HistoryPoint[] {
  // Group per calendar date, keeping rows in asOf order so "latest per
  // product within a day" wins.
  const sorted = [...opts.snapshots].sort(
    (a, b) => a.asOf.getTime() - b.asOf.getTime()
  );
  const byDate = new Map<string, SnapshotRow[]>();
  for (const row of sorted) {
    const dateStr = row.asOf.toISOString().slice(0, 10);
    if (!byDate.has(dateStr)) byDate.set(dateStr, []);
    byDate.get(dateStr)!.push(row);
  }
  const snapshotDates = [...byDate.keys()].sort();

  const from = opts.from ?? snapshotDates[0];
  const to = opts.to ?? snapshotDates[snapshotDates.length - 1];
  if (from == null || to == null || from > to) return [];

  const carried = new Map<
    string,
    { balance: number; metrics: ProductMetrics | null; asOf: Date }
  >();
  const applyDay = (dateStr: string) => {
    for (const row of byDate.get(dateStr) ?? []) {
      carried.set(row.productId, {
        balance: row.balance,
        metrics: snapshotMetrics(row.metrics),
        asOf: row.asOf,
      });
    }
  };

  // Warm the carry map with observations that predate the window, so a range
  // starting mid-history doesn't open with spurious no-data days.
  for (const dateStr of snapshotDates) {
    if (dateStr < from) applyDay(dateStr);
  }

  const points: HistoryPoint[] = [];
  const endMs = toUtcMs(to);
  for (let dayMs = toUtcMs(from); dayMs <= endMs; dayMs += DAY_MS) {
    const dateStr = new Date(dayMs).toISOString().slice(0, 10);
    applyDay(dateStr);

    // Each product as it looked on this day: the current row's identity
    // (kind, currency, isActive) with the carried observation overlaid.
    // Products with no observation yet stay absent, so refs to them are
    // no-data rather than leaking today's balance into the past.
    const dayProducts = new Map<string, ProductInfo>();
    for (const [productId, obs] of carried) {
      const base = opts.products.get(productId);
      if (!base) continue;
      dayProducts.set(productId, {
        ...base,
        currentBalance: obs.balance,
        metrics: obs.metrics,
        balanceAsOf: obs.asOf,
      });
    }

    const evaluation = evaluateMonitor(def, {
      date: toEvalDate(dateStr),
      products: dayProducts,
      rates: opts.rates,
      currency: def.currency,
    });

    points.push({
      date: dateStr,
      value: evaluation.value,
      status: evaluation.status,
      margin: evaluation.margin,
      thresholds: evaluation.thresholds,
    });
  }
  return points;
}
