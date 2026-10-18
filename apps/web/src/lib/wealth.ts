// Wealth series behind /api/wealth: legacy wealth_snapshots totals merged
// with points derived from product_snapshots, plus the rules for backdating
// legacy history. Pure (no db) so it unit-tests in isolation; the route
// queries both tables and hands the rows over.

import type { ProductKind, ProductMetrics } from "@chanchito/product-model";
import { calcWealthMetrics } from "./budget-engine";
import { formatLocalDate } from "./monitors/history";
import { assetClp, debtClp } from "./networth";
import { ratesFor, type RateBook } from "./rates";

/** One wealth_snapshots row: a legacy total for a pre-migration date. */
export type LegacyWealthRow = {
  id: string;
  /** Calendar date, YYYY-MM-DD. */
  snapshotDate: string;
  patrimonio: number;
  deuda: number;
  fintualBalance: number | null;
  mercadopagoBalance: number | null;
  banchileSavings: number | null;
  notes: string | null;
};

/** One product_snapshots row joined with its product and institution. */
export type WealthSnapshotRow = {
  productId: string;
  /** Numeric column: drizzle returns it as a string. */
  balance: number | string;
  metrics: ProductMetrics | Record<string, never>;
  asOf: Date;
  source: string;
  kind: ProductKind;
  currency: string;
  slug: string;
};

export type WealthPoint = {
  id: string;
  snapshotDate: string;
  patrimonio: number;
  deuda: number;
  fintualBalance: number | null;
  mercadopagoBalance: number | null;
  banchileSavings: number | null;
  notes: string | null;
  source: "legacy" | "computed";
};

/** A series point with the metrics derived against the previous point. */
export type WealthSeriesPoint = WealthPoint & {
  ahorro: number;
  periodSavings: number | null;
  monthsBetween: number | null;
  monthlyRate: number | null;
};

/** product_snapshots source of the V009 backfill, which decomposed the three
 *  legacy component columns into per-product rows on legacy dates. */
export const BACKFILL_SOURCE = "wealth_snapshot";

/**
 * Local day (YYYY-MM-DD) of the first real product observation after
 * `lastLegacyDate` (null or "" for no legacy rows), or null when there is none
 * yet. Earlier observations are hidden by legacy totals anyway, and backfill
 * rows are not observations, so neither starts the derived series.
 */
export function derivedSeriesStart(
  snapshots: { asOf: Date; source: string }[],
  lastLegacyDate: string | null
): string | null {
  let start: string | null = null;
  for (const row of snapshots) {
    if (row.source === BACKFILL_SOURCE) continue;
    const day = formatLocalDate(row.asOf);
    if (lastLegacyDate && day <= lastLegacyDate) continue;
    if (start === null || day < start) start = day;
  }
  return start;
}

/** First date a legacy snapshot can no longer take: the derived series'
 *  start or today, whichever comes first. Before it, a backdated snapshot
 *  can never hide a computed point. */
export function legacyEntryCutoff(
  derivedStart: string | null,
  now: Date
): string {
  const today = formatLocalDate(now);
  return derivedStart !== null && derivedStart < today ? derivedStart : today;
}

/** Why a legacy snapshot cannot take `snapshotDate` (a 409), or null when it
 *  may: only dates before `cutoff` (see legacyEntryCutoff) are backdatable. */
export function legacyDateError(
  snapshotDate: string,
  cutoff: string
): string | null {
  if (snapshotDate < cutoff) return null;
  return `Legacy snapshots can only backdate history before ${cutoff}; from then on wealth is derived from product balances`;
}

/** A snapshot's typed metrics, or null for rows that predate them (`{}`). */
function snapshotMetrics(
  metrics: ProductMetrics | Record<string, never>
): ProductMetrics | null {
  return "kind" in metrics ? (metrics as ProductMetrics) : null;
}

/** Local-midnight Date for a YYYY-MM-DD day key. calcWealthMetrics reads
 *  local month fields, and `new Date("YYYY-MM-DD")` is UTC midnight: the
 *  previous local day west of UTC, so a 1st-of-month counted as the month
 *  before. */
function localDate(day: string): Date {
  const [year, month, date] = day.split("-").map(Number);
  return new Date(year, month - 1, date);
}

/**
 * Wealth series with derived metrics, in date order.
 *
 * Pre-migration dates come from legacy `wealth_snapshots` totals (which may
 * include components that never became products). Later dates are computed
 * from `product_snapshots`, carrying each product's latest observation forward
 * per date: patrimonio = Σ asset value, deuda = Σ owed, both in CLP. Debt
 * derives from each snapshot's *own* metrics (the limit/owed as observed on
 * that date), not from today's product row.
 *
 * Foreign/crypto balances are converted to CLP with each product's current
 * rate: its institution's own when scraped, else the provider's (see
 * lib/rates and lib/networth). Note: only *current* rates are available, so
 * historical points are valued at today's prices; acceptable while the
 * computed series is short, and storing per-date rates would be the fix once
 * history accumulates.
 */
export function buildWealthSeries(
  legacy: LegacyWealthRow[],
  snapshots: WealthSnapshotRow[],
  book: RateBook
): WealthSeriesPoint[] {
  // Chronological inputs: the last legacy row sets the boundary and, within
  // a day, the latest observation per product wins.
  const legacyRows = [...legacy].sort((a, b) =>
    a.snapshotDate.localeCompare(b.snapshotDate)
  );
  const balanceRows = [...snapshots].sort(
    (a, b) => a.asOf.getTime() - b.asOf.getTime()
  );

  // Legacy totals are authoritative up to their last date; the backfilled
  // history rows on those dates only cover 3 components and would undercount.
  const lastLegacyDate =
    legacyRows.length > 0 ? legacyRows[legacyRows.length - 1].snapshotDate : "";

  // Group history rows per local calendar day (the unit of the legacy
  // snapshot_date column; an evening scrape belongs to today, not to
  // tomorrow's UTC date), then walk chronologically carrying the latest
  // balance per product.
  const byDate = new Map<string, WealthSnapshotRow[]>();
  for (const row of balanceRows) {
    const dateStr = formatLocalDate(row.asOf);
    if (!byDate.has(dateStr)) byDate.set(dateStr, []);
    byDate.get(dateStr)!.push(row);
  }

  const latestByProduct = new Map<
    string,
    {
      balance: number;
      metrics: ProductMetrics | null;
      kind: ProductKind;
      currency: string;
      slug: string;
    }
  >();
  const computed: WealthPoint[] = [];

  for (const [dateStr, rows] of [...byDate.entries()].sort(([a], [b]) =>
    a.localeCompare(b)
  )) {
    for (const row of rows) {
      latestByProduct.set(row.productId, {
        balance: Number(row.balance),
        metrics: snapshotMetrics(row.metrics),
        kind: row.kind,
        currency: row.currency,
        slug: row.slug,
      });
    }

    if (dateStr <= lastLegacyDate) continue;
    // Backfill rows still carry forward, but a day with nothing else is no
    // observation (the same rule derivedSeriesStart applies).
    if (rows.every((row) => row.source === BACKFILL_SOURCE)) continue;

    let patrimonio = 0;
    let deuda = 0;
    let fintualBalance: number | null = null;
    let mercadopagoBalance: number | null = null;
    let banchileSavings: number | null = null;

    for (const [productId, p] of latestByProduct) {
      const rates = ratesFor(book, { productId, institutionSlug: p.slug });
      patrimonio += assetClp(p.kind, p.balance, p.currency, rates) ?? 0;
      deuda += debtClp(p.kind, p.balance, p.metrics, p.currency, rates) ?? 0;
      // These component columns are CLP-denominated products (informational).
      // Fintual emits one product per goal, so its column sums across them;
      // its USD stock positions count in patrimonio only.
      if (p.slug === "fintual" && p.kind === "investment" && p.currency === "CLP")
        fintualBalance = (fintualBalance ?? 0) + p.balance;
      if (p.slug === "mercadopago" && p.kind === "wallet")
        mercadopagoBalance = p.balance;
      if (p.slug === "banchile" && p.kind === "savings")
        banchileSavings = p.balance;
    }

    computed.push({
      id: `computed-${dateStr}`,
      snapshotDate: dateStr,
      patrimonio: Math.round(patrimonio),
      deuda: Math.round(deuda),
      fintualBalance,
      mercadopagoBalance,
      banchileSavings,
      notes: null,
      source: "computed",
    });
  }

  const merged: WealthPoint[] = [
    ...legacyRows.map((row) => ({
      id: row.id,
      snapshotDate: row.snapshotDate,
      patrimonio: row.patrimonio,
      deuda: row.deuda,
      fintualBalance: row.fintualBalance,
      mercadopagoBalance: row.mercadopagoBalance,
      banchileSavings: row.banchileSavings,
      notes: row.notes,
      source: "legacy" as const,
    })),
    ...computed,
  ].sort((a, b) => a.snapshotDate.localeCompare(b.snapshotDate));

  return merged.map((row, i) => {
    const prev = i > 0 ? merged[i - 1] : null;
    const metrics = calcWealthMetrics(
      {
        patrimonio: row.patrimonio,
        deuda: row.deuda,
        date: localDate(row.snapshotDate),
      },
      prev
        ? {
            patrimonio: prev.patrimonio,
            deuda: prev.deuda,
            date: localDate(prev.snapshotDate),
          }
        : null
    );

    return {
      ...row,
      ahorro: metrics.ahorro,
      periodSavings: metrics.periodSavings,
      monthsBetween: metrics.monthsBetween,
      monthlyRate: metrics.monthlyRate,
    };
  });
}
