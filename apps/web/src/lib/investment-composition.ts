// Value split behind the product page's "Composición del valor" donut, kept
// free of React so it can be unit tested.

import type { InvestmentMetrics, StockMetrics } from "@chanchito/product-model";

export interface InvestmentComposition {
  /** What the value grew from (always positive): the net amount deposited
   *  into a goal, or what a position's units still held cost. */
  base: number;
  /** Gain (positive) or loss (negative) on top of `base`. */
  result: number;
  /** `result` as a percent number of `base`. */
  pct: number;
  /** Current value. */
  nav: number;
  /** A position held in units (a stock), so `base` is a cost. */
  byUnits: boolean;
}

/** The donut's two slices, or null when they can't be drawn: a figure is
 *  missing or the base isn't positive. A position held in units is split by
 *  the cost of what it still holds, since its net invested goes negative
 *  once profitable sales returned more than went in. */
export function investmentComposition(
  m: InvestmentMetrics | StockMetrics
): InvestmentComposition | null {
  if (m.kind === "stock" && m.units != null) {
    if (m.unrealized_profit == null) return null;
    const base = m.nav - m.unrealized_profit;
    if (!(base > 0)) return null;
    const result = m.unrealized_profit;
    return {
      base,
      result,
      pct: m.unrealized_profit_pct ?? (result / base) * 100,
      nav: m.nav,
      byUnits: true,
    };
  }
  if (m.deposited == null || m.profit == null) return null;
  if (!(m.deposited > 0)) return null;
  return {
    base: m.deposited,
    result: m.profit,
    pct: (m.profit / m.deposited) * 100,
    nav: m.nav ?? m.deposited + m.profit,
    byUnits: false,
  };
}
