// Month-over-month figures per category, read off the month groups of
// GET /api/categories/spend?groupBy=month: this month, last month, the
// average of the three full months before this one, and a short series for a
// sparkline. Pure, so the arithmetic is unit-tested apart from the page.

import {
  UNCATEGORIZED_PARAM,
  monthWindow,
  type CategorySpend,
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

/** One category's spend this month next to last month. */
export interface MonthComparison {
  /** Null for "Sin categoría". */
  categoryId: string | null;
  name: string;
  color: string;
  thisMonth: number;
  lastMonth: number;
}

export interface CategoryTrend extends MonthComparison {
  /** Mean of the (up to) AVERAGE_MONTHS full months before this one within
   *  the series, empty months counting as zero. */
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
    // A series shorter than AVERAGE_MONTHS + 1 averages the months it has.
    const covered = Math.min(AVERAGE_MONTHS, months - 1);
    let sum = 0;
    for (let back = 1; back <= covered; back++) sum += at(back);
    trend.average = covered > 0 ? sum / covered : 0;
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
export function topThisMonth<T extends MonthComparison>(
  trends: Iterable<T>,
  count: number
): T[] {
  return [...trends]
    .filter((t) => t.thisMonth > 0)
    .sort((a, b) => b.thisMonth - a.thisMonth || a.name.localeCompare(b.name, "es"))
    .slice(0, count);
}

/** YYYY-MM-DD of a date, in local time. */
function dayOf(date: Date): string {
  return `${monthOf(date)}-${String(date.getDate()).padStart(2, "0")}`;
}

/**
 * Month to date: day 1 of `today`'s month through `today`, and the same span
 * of the month before (through the same day, clamped to that month's
 * length), so an early-month figure is compared like for like.
 */
export function monthToDateWindows(today: Date): {
  current: { from: string; to: string };
  previous: { from: string; to: string };
} {
  const month = monthOf(today);
  const previous = monthWindow(shiftMonth(month, -1));
  const previousLastDay = Number(previous.to.slice(8));
  const day = Math.min(today.getDate(), previousLastDay);
  return {
    current: { from: `${month}-01`, to: dayOf(today) },
    previous: {
      from: previous.from,
      to: `${previous.to.slice(0, 8)}${String(day).padStart(2, "0")}`,
    },
  };
}

/** Every category in either spend list, this month's figure next to last
 *  month's; a category missing from one side spent nothing there. */
export function compareSpend(
  thisMonth: readonly CategorySpend[],
  lastMonth: readonly CategorySpend[]
): MonthComparison[] {
  const rows = new Map<string, MonthComparison>();
  const row = (c: CategorySpend) => {
    const key = trendKey(c.categoryId);
    let found = rows.get(key);
    if (!found) {
      found = {
        categoryId: c.categoryId,
        name: c.name,
        color: c.color,
        thisMonth: 0,
        lastMonth: 0,
      };
      rows.set(key, found);
    }
    return found;
  };
  for (const c of thisMonth) row(c).thisMonth += c.amount;
  for (const c of lastMonth) row(c).lastMonth += c.amount;
  return [...rows.values()];
}
