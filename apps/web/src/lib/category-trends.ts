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

/** One month of a category (or of the total) in the category x month table. */
export interface SpendMatrixCell {
  /** YYYY-MM. */
  month: string;
  amount: number;
  /** Percent change from the month before; null when that month spent
   *  nothing (or is unknown), or for the month in progress. */
  change: number | null;
}

export interface SpendMatrixRow {
  /** Null for "Sin categoría". */
  categoryId: string | null;
  name: string;
  color: string;
  /** One per shown month, oldest first. */
  cells: SpendMatrixCell[];
  /** Sum over the shown months. */
  total: number;
  /** Mean over the shown full months (the one in progress left out, empty
   *  months counting as zero); null when no full month is shown. */
  average: number | null;
}

export interface SpendMatrix {
  months: string[];
  /** Largest total first; "Sin categoría" always last. */
  rows: SpendMatrixRow[];
  /** Every category together, shaped like a row. */
  totals: Omit<SpendMatrixRow, "categoryId" | "name" | "color">;
}

/**
 * The category x month table of the Gastos > Categorías tab, from the month
 * groups of GET /api/categories/spend?groupBy=month. `months` are the
 * columns, oldest first; a group for the month before the first one, when
 * present, gives the first column its change. `inProgress` (the current
 * month) gets no change and stays out of the average, so a partial month
 * never reads as a drop.
 */
export function categoryMonthMatrix(
  groups: readonly CategorySpendGroup[],
  months: readonly string[],
  inProgress?: string
): SpendMatrix {
  const byMonth = new Map(groups.map((g) => [g.key, g]));
  // `key` is a trendKey; without one, every category together.
  const spendIn = (month: string, key?: string): number => {
    const group = byMonth.get(month);
    if (!group) return 0;
    if (key === undefined) return group.total;
    return group.categories
      .filter((c) => trendKey(c.categoryId) === key)
      .reduce((sum, c) => sum + c.amount, 0);
  };
  const fullMonths = months.filter((m) => m !== inProgress).length;
  const line = (key?: string) => {
    const cells = months.map((month) => {
      const amount = spendIn(month, key);
      return {
        month,
        amount,
        change:
          month === inProgress
            ? null
            : percentChange(amount, spendIn(shiftMonth(month, -1), key)),
      };
    });
    const total = cells.reduce((sum, c) => sum + c.amount, 0);
    const fullTotal = cells
      .filter((c) => c.month !== inProgress)
      .reduce((sum, c) => sum + c.amount, 0);
    return {
      cells,
      total,
      average: fullMonths > 0 ? fullTotal / fullMonths : null,
    };
  };

  const shown = new Set(months);
  const categories = new Map<
    string,
    { categoryId: string | null; name: string; color: string }
  >();
  for (const group of groups) {
    if (!shown.has(group.key)) continue;
    for (const c of group.categories) {
      if (!categories.has(trendKey(c.categoryId))) {
        categories.set(trendKey(c.categoryId), {
          categoryId: c.categoryId,
          name: c.name,
          color: c.color,
        });
      }
    }
  }
  const rows = [...categories.entries()]
    .map(([key, info]) => ({ ...info, ...line(key) }))
    .filter((row) => row.total > 0)
    .sort((a, b) => {
      if ((a.categoryId === null) !== (b.categoryId === null)) {
        return a.categoryId === null ? 1 : -1;
      }
      return b.total - a.total || a.name.localeCompare(b.name, "es");
    });
  return { months: [...months], rows, totals: line() };
}
