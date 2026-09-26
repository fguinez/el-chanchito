// Wealth series behind /api/wealth: legacy wealth_snapshots totals merged
// with points derived from product_snapshots, plus the rules for backdating
// legacy history. Pure (no db) so it unit-tests in isolation; the route
// queries both tables and hands the rows over.

import type { ProductKind, ProductMetrics } from "@chanchito/product-model";
import { calcWealthMetrics } from "./budget-engine";
import { formatLocalDate } from "./monitors/history";
import { assetClp, debtClp } from "./networth";
import type { ClpRates } from "./rates";

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

/** A legacy snapshot write, validated and rounded for the INTEGER columns. */
export type LegacySnapshotInput = Omit<LegacyWealthRow, "id">;

type ValidationFailure = { ok: false; status: 400 | 409; error: string };

export type LegacySnapshotValidation =
  | { ok: true; value: LegacySnapshotInput }
  | ValidationFailure;

const INTEGER_MIN = -2147483648;
const INTEGER_MAX = 2147483647;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const COMPONENT_FIELDS = [
  "fintualBalance",
  "mercadopagoBalance",
  "banchileSavings",
] as const;

function fail(status: 400 | 409, error: string): ValidationFailure {
  return { ok: false, status, error };
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** A real calendar date in YYYY-MM-DD form (2026-02-30 is not). */
function isCalendarDate(value: unknown): value is string {
  if (typeof value !== "string" || !DATE_RE.test(value)) return false;
  const [year, month, day] = value.split("-").map(Number);
  const date = new Date(Date.UTC(year, month - 1, day));
  return (
    date.getUTCFullYear() === year &&
    date.getUTCMonth() === month - 1 &&
    date.getUTCDate() === day
  );
}

/** A JSON amount rounded to an integer that fits the INTEGER column. */
function toInteger(
  field: string,
  raw: unknown,
  nonNegative: boolean
): { ok: true; value: number } | ValidationFailure {
  if (typeof raw !== "number" || !Number.isFinite(raw)) {
    return fail(400, `Field '${field}' must be a number`);
  }
  if (nonNegative && raw < 0) {
    return fail(400, `Field '${field}' must not be negative`);
  }
  const value = Math.round(raw);
  if (value < INTEGER_MIN || value > INTEGER_MAX) {
    return fail(400, `Field '${field}' does not fit an integer column`);
  }
  return { ok: true, value };
}

/**
 * Validate a POST /api/wealth body against the backdating cutoff (see
 * legacyEntryCutoff): 400 for a malformed body, 409 for a date the derived
 * series owns. `deuda` defaults to 0; the component columns and `notes` to
 * null.
 */
export function validateLegacySnapshot(
  body: unknown,
  cutoff: string
): LegacySnapshotValidation {
  if (!isPlainObject(body)) return fail(400, "Body must be a JSON object");

  const { snapshotDate, notes } = body;
  if (!isCalendarDate(snapshotDate)) {
    return fail(
      400,
      "Field 'snapshotDate' must be a calendar date (YYYY-MM-DD)"
    );
  }

  const patrimonio = toInteger("patrimonio", body.patrimonio, true);
  if (!patrimonio.ok) return patrimonio;
  const deuda = toInteger("deuda", body.deuda ?? 0, true);
  if (!deuda.ok) return deuda;

  const components: Record<(typeof COMPONENT_FIELDS)[number], number | null> =
    { fintualBalance: null, mercadopagoBalance: null, banchileSavings: null };
  for (const field of COMPONENT_FIELDS) {
    if (body[field] == null) continue;
    const amount = toInteger(field, body[field], false);
    if (!amount.ok) return amount;
    components[field] = amount.value;
  }

  if (notes != null && typeof notes !== "string") {
    return fail(400, "Field 'notes' must be a string or null");
  }

  if (snapshotDate >= cutoff) {
    return fail(
      409,
      `Legacy snapshots can only backdate history before ${cutoff}; from then on wealth is derived from product balances`
    );
  }

  return {
    ok: true,
    value: {
      snapshotDate,
      patrimonio: patrimonio.value,
      deuda: deuda.value,
      ...components,
      notes: notes ?? null,
    },
  };
}

/** A snapshot's typed metrics, or null for rows that predate them (`{}`). */
function snapshotMetrics(
  metrics: ProductMetrics | Record<string, never>
): ProductMetrics | null {
  return "kind" in metrics ? (metrics as ProductMetrics) : null;
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
 * Foreign/crypto balances are converted to CLP with current Buda tickers (see
 * lib/networth). Note: only *current* rates are available, so historical
 * points are valued at today's prices; acceptable while the computed series
 * is short, and storing per-date rates would be the fix once history
 * accumulates.
 */
export function buildWealthSeries(
  legacy: LegacyWealthRow[],
  snapshots: WealthSnapshotRow[],
  rates: ClpRates
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

    for (const p of latestByProduct.values()) {
      patrimonio += assetClp(p.kind, p.balance, p.currency, rates) ?? 0;
      deuda += debtClp(p.kind, p.balance, p.metrics, p.currency, rates) ?? 0;
      // These component columns are CLP-denominated products (informational).
      // Fintual emits one product per goal, so its column sums across them.
      if (p.slug === "fintual" && p.kind === "investment")
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
        date: new Date(row.snapshotDate),
      },
      prev
        ? {
            patrimonio: prev.patrimonio,
            deuda: prev.deuda,
            date: new Date(prev.snapshotDate),
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
