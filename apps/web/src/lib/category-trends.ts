// Month-over-month figures per category, read off the month groups of
// GET /api/categories/spend?groupBy=month: this month, last month, the
// average of the three full months before this one, and a short series for a
// sparkline. Pure, so the arithmetic is unit-tested apart from the page.

import {
  UNCATEGORIZED_PARAM,
  monthWindow,
  type CategorySpendGroup,
} from "@/lib/category-spend";

/** YYYY-MM of a date, in local time. */
export function monthOf(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}`;
}

/** The YYYY-MM `delta` months after `month` (negative goes back). */
export function shiftMonth(month: string, delta: number): string {
  const [year, m] = month.split("-").map(Number);
  const index = year * 12 + (m - 1) + delta;
  return `${Math.floor(index / 12)}-${String((index % 12) + 1).padStart(2, "0")}`;
}

/** The `count` months ending with `last`, oldest first. */
export function trailingMonths(last: string, count: number): string[] {
  return Array.from({ length: count }, (_, i) => shiftMonth(last, i - count + 1));
}

/** The inclusive YYYY-MM-DD window of the `count` months ending with `last`. */
export function trailingMonthsWindow(
  last: string,
  count: number
): { from: string; to: string } {
  return {
    from: monthWindow(shiftMonth(last, 1 - count)).from,
    to: monthWindow(last).to,
  };
}

/** How many full months before the current one the average covers. */
export const AVERAGE_MONTHS = 3;

export interface CategoryTrend {
  /** Null for "Sin categoría". */
  categoryId: string | null;
  name: string;
  color: string;
  thisMonth: number;
  lastMonth: number;
  /** Mean of the AVERAGE_MONTHS full months before this one, empty months
   *  counting as zero. */
  average: number;
  /** Spend per month of the requested series, oldest first, ending with
   *  this month. */
  series: number[];
}

/** The key a trend map uses: the category id, or "none" for uncategorized. */
export function trendKey(categoryId: string | null): string {
  return categoryId ?? UNCATEGORIZED_PARAM;
}

/**
 * Every category that spent anything in the `months` months ending with
 * `currentMonth`, keyed by trendKey. A month missing from `groups`, or a
 * category missing from a month, counts as zero spend.
 */
export function categoryTrends(
  groups: readonly CategorySpendGroup[],
  currentMonth: string,
  months: number
): Map<string, CategoryTrend> {
  const window = trailingMonths(currentMonth, months);
  const byMonth = new Map(groups.map((g) => [g.key, g]));
  const trends = new Map<string, CategoryTrend>();
  window.forEach((month, index) => {
    for (const c of byMonth.get(month)?.categories ?? []) {
      const key = trendKey(c.categoryId);
      let trend = trends.get(key);
      if (!trend) {
        trend = {
          categoryId: c.categoryId,
          name: c.name,
          color: c.color,
          thisMonth: 0,
          lastMonth: 0,
          average: 0,
          series: new Array<number>(months).fill(0),
        };
        trends.set(key, trend);
      }
      trend.series[index] += c.amount;
    }
  });
  for (const trend of trends.values()) {
    const at = (monthsBack: number) => trend.series[months - 1 - monthsBack] ?? 0;
    trend.thisMonth = at(0);
    trend.lastMonth = at(1);
    let sum = 0;
    for (let back = 1; back <= AVERAGE_MONTHS; back++) sum += at(back);
    trend.average = sum / AVERAGE_MONTHS;
  }
  return trends;
}

/** Percent change from `previous` to `current`; null when there is nothing
 *  to compare against (no spend before). */
export function percentChange(current: number, previous: number): number | null {
  if (!(previous > 0)) return null;
  return ((current - previous) / previous) * 100;
}

/** The `count` categories that spent the most this month, largest first. */
export function topThisMonth(
  trends: Iterable<CategoryTrend>,
  count: number
): CategoryTrend[] {
  return [...trends]
    .filter((t) => t.thisMonth > 0)
    .sort((a, b) => b.thisMonth - a.thisMonth || a.name.localeCompare(b.name, "es"))
    .slice(0, count);
}
