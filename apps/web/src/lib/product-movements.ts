// Balance waterfall reconstruction behind the product page's "Movimientos"
// card, kept free of React so it can be unit tested.

import { formatDayEs } from "@/lib/utils";

export interface MovementLike {
  id: string;
  description: string;
  /** Signed amount in the product's currency: negative = expense, positive = income. */
  amount: number;
  /** Plain YYYY-MM-DD. */
  transactionDate: string;
}

export const WATERFALL_MAX_BARS = 90;

export interface WaterfallDatum {
  key: string;
  /** Chart slot (index within the capped, ascending list) so same-day
   *  transactions get their own bar instead of overlapping. */
  i: number;
  date: string;
  description: string;
  amount: number;
  before: number;
  after: number;
  base: number;
  delta: number;
}

/** Range the reconstructed balance must stay inside to be believable. */
export interface BalanceBounds {
  min?: number;
  max?: number;
}

/** One bar per movement, from the balance before to the balance after, walking
 *  the known transactions backwards from the current balance.
 *
 *  The walk is only as good as the movement history: a credit card exposes just
 *  the current and last billed periods, so charges older than that survive in
 *  the DB while the payments that cleared them never do. Walking past that point
 *  drifts (a card would show more cupo disponible than its own limit), so the
 *  walk stops as soon as the reconstructed balance leaves `bounds` and the chart
 *  keeps only the stretch it can still reconstruct honestly. */
export function buildWaterfallData(
  transactions: MovementLike[],
  currentBalance: number,
  bounds: BalanceBounds = {}
): { rows: WaterfallDatum[]; truncated: boolean } {
  const ascending = [...transactions].sort((a, b) =>
    a.transactionDate.localeCompare(b.transactionDate)
  );
  const recent = ascending.slice(-WATERFALL_MAX_BARS);
  const out: WaterfallDatum[] = [];
  let truncated = false;
  let bal = currentBalance;
  for (let i = recent.length - 1; i >= 0; i--) {
    const t = recent[i];
    const after = bal;
    const before = after - t.amount;
    if (
      (bounds.min != null && before < bounds.min) ||
      (bounds.max != null && before > bounds.max)
    ) {
      truncated = true;
      break;
    }
    out.push({
      key: t.id,
      i: 0, // slot assigned below, once the kept range is known
      date: formatDayEs(t.transactionDate),
      description: t.description,
      amount: t.amount,
      before,
      after,
      base: Math.min(before, after),
      delta: Math.abs(after - before),
    });
    bal = before;
  }
  const rows = out.reverse().map((row, i) => ({ ...row, i }));
  return { rows, truncated: truncated && rows.length < recent.length };
}
