// Monitor adjustments ("variaciones", V040): the API shape of a linked
// transaction with its effect on the thresholds, the per-day grouping the
// monitor page renders, its search (across every month, grouped by month),
// and small request helpers the routes share. Pure and engine-free, so the
// client card can import it; the effects are computed in effects.ts, and how
// a variación moves the thresholds lives in evaluate.ts (thresholdShift,
// adjustmentOnDate) and undo.ts.

import { matchesTransactionSearch } from "@/lib/transaction-search";
import type { MonitorAdjustment } from "./types";
import type { ValidationResult } from "./validate";

const MONTH_RE = /^\d{4}-(0[1-9]|1[0-2])$/;

/** The optional `?month=YYYY-MM` list filter; null when absent. */
export function parseMonthParam(
  raw: string | null
): ValidationResult<string | null> {
  if (raw == null) return { ok: true, value: null };
  if (!MONTH_RE.test(raw)) {
    return {
      ok: false,
      status: 400,
      error: "Invalid 'month', expected YYYY-MM",
      field: "month",
    };
  }
  return { ok: true, value: raw };
}

/** Whether a failed write broke a foreign key (Postgres 23503), e.g. the
 *  monitor was deleted meanwhile. Drizzle wraps the driver error in `cause`. */
export function isForeignKeyViolation(error: unknown): boolean {
  return hasPgCode(error, "23503");
}

/** Whether a failed write broke a unique constraint (Postgres 23505). */
export function isUniqueViolation(error: unknown): boolean {
  return hasPgCode(error, "23505");
}

function hasPgCode(error: unknown, code: string): boolean {
  for (let e: unknown = error, depth = 0; e != null && depth < 3; depth++) {
    if (typeof e !== "object") return false;
    if ((e as { code?: unknown }).code === code) return true;
    e = (e as { cause?: unknown }).cause;
  }
  return false;
}

/** A variación as loaded: the link plus what the card shows of its
 *  transaction (display description, product) and whether it was deleted. */
export type MonitorAdjustmentRow = MonitorAdjustment & {
  id: string;
  monitorId: string;
  createdAt: Date | string;
  updatedAt: Date | string;
  /** What to show: the custom description or alias, else the scraped text. */
  description: string;
  /** The scraped (or manual) text, which the card's search also matches. */
  rawDescription: string;
  /** Display name (see lib/product-names). */
  productName: string;
  productCurrency: string;
  institutionSlug: string;
  institutionName: string;
  deleted: boolean;
};

/** The API shape: the row plus its effect on the alert threshold right now
 *  (see effects.ts), in the monitor currency. */
export type ApiMonitorAdjustment = MonitorAdjustmentRow & {
  /** Whether the monitor (left side or a threshold) reads a field undoing
   *  it moves. */
  affects: boolean;
  /** This variación alone; null when deleted or when the monitor has no
   *  data to compute it. */
  effect: number | null;
  /** All non-deleted variaciones of the same month dated up to this one's
   *  day, together: what the alert threshold carries from this day on (null
   *  as above). */
  monthToDateEffect: number | null;
};

type GroupableAdjustment = {
  transactionDate: string;
  deleted: boolean;
  monthToDateEffect: number | null;
};

export type AdjustmentDay<A extends GroupableAdjustment> = {
  transactionDate: string;
  /** That day's variaciones, in the order given. */
  entries: A[];
  /** What the day added to the month's shift; null if it has no data. */
  dayTotal: number | null;
  /** The month's shift through this day: what every threshold carries from
   *  this day until the next variación day (see adjustmentOnDate). */
  runningTotal: number | null;
};

/** One month's (YYYY-MM) variaciones grouped per transaction day, oldest day
 *  first; totals come from the server's monthToDateEffect. A day with only
 *  deleted transactions carries the previous day's total. */
export function groupAdjustmentsByDay<A extends GroupableAdjustment>(
  adjustments: readonly A[],
  month: string
): AdjustmentDay<A>[] {
  const byDay = new Map<string, A[]>();
  for (const adjustment of adjustments) {
    if (!adjustment.transactionDate.startsWith(`${month}-`)) continue;
    const entries = byDay.get(adjustment.transactionDate) ?? [];
    entries.push(adjustment);
    byDay.set(adjustment.transactionDate, entries);
  }

  let previous: number | null = 0;
  return [...byDay.keys()].sort().map((transactionDate) => {
    const entries = byDay.get(transactionDate)!;
    const live = entries.find((a) => !a.deleted);
    const runningTotal = live ? live.monthToDateEffect : previous;
    const dayTotal =
      runningTotal == null || previous == null
        ? null
        : Number((runningTotal - previous).toFixed(8)) || 0;
    previous = runningTotal;
    return { transactionDate, entries, dayTotal, runningTotal };
  });
}

/** The variaciones a search keeps (see lib/transaction-search: each word in
 *  the display or the raw description), grouped by month (YYYY-MM), newest
 *  month first. Each month lists its entries in the month view's order
 *  (groupAdjustmentsByDay: oldest day first, a day's entries in the order
 *  given), so turning the search on or off never reorders a month's rows. */
export function searchAdjustmentsByMonth<
  A extends { transactionDate: string; description: string; rawDescription: string },
>(adjustments: readonly A[], tokens: readonly string[]): { month: string; entries: A[] }[] {
  const byMonth = new Map<string, A[]>();
  for (const adjustment of adjustments) {
    if (
      !matchesTransactionSearch(
        tokens,
        adjustment.description,
        adjustment.rawDescription
      )
    ) {
      continue;
    }
    const month = adjustment.transactionDate.slice(0, 7);
    const entries = byMonth.get(month) ?? [];
    entries.push(adjustment);
    byMonth.set(month, entries);
  }
  return [...byMonth.keys()]
    .sort()
    .reverse()
    .map((month) => ({
      month,
      // A stable sort: same-day entries keep the order given.
      entries: byMonth
        .get(month)!
        .sort((a, b) =>
          a.transactionDate < b.transactionDate
            ? -1
            : a.transactionDate > b.transactionDate
              ? 1
              : 0
        ),
    }));
}
