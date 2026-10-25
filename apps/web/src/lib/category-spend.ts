// Spending per category, the figure behind every category chart. Pure, so the
// rules are unit-tested apart from the database (GET /api/categories/spend
// loads and converts the rows).
//
// Semantics: internal transfers never count (see countsTowardFlow). A real
// category's spend is the negated net of its rows, so a refund subtracts from
// it, and a category that nets to zero or income drops out. "Sin categoría"
// counts only its expenses: income has no category to offset, so it must not
// shrink the uncategorized slice. Percents are shares of the scope's total.

import { DEFAULT_CATEGORY_COLOR } from "@/lib/categories";
import { countsTowardFlow } from "@/lib/transactions";

/** The `categoryId` query value that selects uncategorized rows. */
export const UNCATEGORIZED_PARAM = "none";

export const UNCATEGORIZED_NAME = "Sin categoría";
/** Gray of the uncategorized slice. */
export const UNCATEGORIZED_COLOR = "#cbd5e1";

export const SPEND_GROUP_BYS = ["none", "month", "product", "institution"] as const;
export type SpendGroupBy = (typeof SPEND_GROUP_BYS)[number];

/** A signed amount already in the scope's currency, or a partial sum of rows
 *  that share every field (and the sign of their amounts). */
export interface SpendRow {
  categoryId: string | null;
  amount: number;
  /** YYYY-MM. */
  month: string;
  productId: string;
  institutionSlug: string;
  isInternalTransfer?: boolean;
}

export interface SpendCategoryInfo {
  id: string;
  name: string;
  color: string | null;
}

export interface CategorySpend {
  /** Null for "Sin categoría". */
  categoryId: string | null;
  name: string;
  color: string;
  /** Positive spend in the scope's currency. */
  amount: number;
  /** Share of the scope's total, 0 to 100. */
  percent: number;
}

export interface CategorySpendSummary {
  total: number;
  /** Largest first; "Sin categoría" always last. */
  categories: CategorySpend[];
}

export interface CategorySpendGroup extends CategorySpendSummary {
  /** YYYY-MM, product id or institution slug. */
  key: string;
}

/** GET /api/categories/spend. */
export interface CategorySpendResponse extends CategorySpendSummary {
  /** The query's bounds: an inclusive transaction_date window (null = open
   *  side) and the budget month (YYYY-MM-01), as given. */
  from: string | null;
  to: string | null;
  month: string | null;
  /** The product's own currency for a product scope, else CLP. */
  currency: string;
  groupBy: SpendGroupBy;
  /** Present unless `groupBy` is "none"; `label` is the YYYY-MM month, the
   *  product's display name or the institution's name. */
  groups?: (CategorySpendGroup & { label: string })[];
  /** Expense rows (amount < 0) left out because their currency has no CLP
   *  rate; unconvertible income never counts as spend, so it is not here. */
  unconvertedCount: number;
}

/** Spend per category over `rows`, ranked, with shares of the total. */
export function summarizeSpend(
  rows: readonly SpendRow[],
  categoryInfo: ReadonlyMap<string, SpendCategoryInfo>
): CategorySpendSummary {
  const net = new Map<string, number>();
  let uncategorized = 0;
  for (const row of rows) {
    if (!countsTowardFlow(row)) continue;
    if (row.categoryId == null) {
      if (row.amount < 0) uncategorized -= row.amount;
      continue;
    }
    net.set(row.categoryId, (net.get(row.categoryId) ?? 0) - row.amount);
  }

  const ranked: Omit<CategorySpend, "percent">[] = [];
  for (const [id, amount] of net) {
    if (!(amount > 0)) continue;
    const info = categoryInfo.get(id);
    ranked.push({
      categoryId: id,
      name: info?.name ?? "Categoría",
      color: info?.color ?? DEFAULT_CATEGORY_COLOR,
      amount,
    });
  }
  ranked.sort((a, b) => b.amount - a.amount || a.name.localeCompare(b.name, "es"));
  if (uncategorized > 0) {
    ranked.push({
      categoryId: null,
      name: UNCATEGORIZED_NAME,
      color: UNCATEGORIZED_COLOR,
      amount: uncategorized,
    });
  }

  const total = ranked.reduce((sum, c) => sum + c.amount, 0);
  return {
    total,
    categories: ranked.map((c) => ({
      ...c,
      percent: total > 0 ? (c.amount / total) * 100 : 0,
    })),
  };
}

function groupKey(row: SpendRow, groupBy: Exclude<SpendGroupBy, "none">): string {
  if (groupBy === "month") return row.month;
  if (groupBy === "product") return row.productId;
  return row.institutionSlug;
}

/** Every YYYY-MM from `from`'s month to `to`'s, both YYYY-MM-DD. */
export function monthsBetween(from: string, to: string): string[] {
  const months: string[] = [];
  let [year, month] = from.slice(0, 7).split("-").map(Number);
  const last = to.slice(0, 7);
  for (;;) {
    const key = `${year}-${String(month).padStart(2, "0")}`;
    if (key > last) break;
    months.push(key);
    month += 1;
    if (month > 12) {
      month = 1;
      year += 1;
    }
  }
  return months;
}

/**
 * `rows` split by month, product or institution, each part summarized on its
 * own (shares are of the part's total). Month groups run oldest first and
 * cover every month of `window` when given, empty ones included, so a bar
 * chart keeps its gaps; product and institution groups run largest first and
 * leave out those with no spend.
 */
export function groupSpend(
  rows: readonly SpendRow[],
  categoryInfo: ReadonlyMap<string, SpendCategoryInfo>,
  groupBy: Exclude<SpendGroupBy, "none">,
  window?: { from: string; to: string }
): CategorySpendGroup[] {
  const parts = new Map<string, SpendRow[]>();
  if (groupBy === "month" && window) {
    for (const month of monthsBetween(window.from, window.to)) parts.set(month, []);
  }
  for (const row of rows) {
    const key = groupKey(row, groupBy);
    const part = parts.get(key);
    if (part) part.push(row);
    else parts.set(key, [row]);
  }

  const groups = [...parts].map(([key, part]) => ({
    key,
    ...summarizeSpend(part, categoryInfo),
  }));
  if (groupBy === "month") return groups.sort((a, b) => a.key.localeCompare(b.key));
  return groups
    .filter((g) => g.total > 0)
    .sort((a, b) => b.total - a.total || a.key.localeCompare(b.key));
}

/** The first and last YYYY-MM-DD of a YYYY-MM month. */
export function monthWindow(month: string): { from: string; to: string } {
  const [year, m] = month.split("-").map(Number);
  const lastDay = new Date(Date.UTC(year, m, 0)).getUTCDate();
  return { from: `${month}-01`, to: `${month}-${String(lastDay).padStart(2, "0")}` };
}

/** The Gastos page filtered to one category (null = "Sin categoría") over a
 *  day window, optionally within one product or institution (see
 *  lib/expenses-filters for how the page reads it). */
export function categoryExpensesHref({
  categoryId,
  from,
  to,
  productId,
  institution,
}: {
  categoryId: string | null;
  from: string;
  to: string;
  productId?: string;
  institution?: string;
}): string {
  const params = new URLSearchParams({
    category: categoryId ?? UNCATEGORIZED_PARAM,
    from,
    to,
  });
  if (productId) params.set("product", productId);
  if (institution) params.set("institution", institution);
  return `/expenses?${params}`;
}

/** Query param holding the category open in the Categorías detail panel: a
 *  category id, or UNCATEGORIZED_PARAM for "Sin categoría". */
export const CATEGORY_PARAM = "category";

/** The Categorías page with one category's detail panel open. */
export function categoryDetailHref(categoryId: string | null): string {
  const params = new URLSearchParams({
    [CATEGORY_PARAM]: categoryId ?? UNCATEGORIZED_PARAM,
  });
  return `/categories?${params}`;
}
