// Spending per category, the figure behind every category chart. Pure, so the
// rules are unit-tested apart from the database (GET /api/categories/spend
// loads and converts the rows).
//
// Semantics: internal transfers never count (see countsTowardFlow). A real
// category's spend is the negated net of its rows, so a refund subtracts from
// it, and a category that nets to zero or income drops out. "Sin categoría"
// counts only its expenses: income has no category to offset, so it must not
// shrink the uncategorized slice. Percents are shares of the scope's total.
//
// Flows: beside `amount`/`total` (the spend the donuts and stacked bars draw),
// every category, summary and group carries `expenses` and `income` (gross
// sums of its negative and positive rows, both positive) and `net` (expenses
// minus income: positive is net spending, negative net income). Category
// flows cover "Sin categoría" income too. Summary and group flows run over
// every counted row of the part, so a category that nets to income is missing
// from `categories` but still in its part's flows: a request scoped to one
// `categoryId` reads that category's flows there, whatever their sign.
// Product and institution groups with no spend are left out unless
// `keepIncomeGroups` is set (the route sets it for a `categoryId` scope), in
// which case a group with income alone stays.
//
// Subcategories (V046): a subcategory's rows count toward its parent, so the
// route rolls them up (rollUpToParents) before summarizing and every chart
// shows one slice per top-level category. A parent's detail panel asks for
// the split instead (categoryInfoFor with `splitParentId`): one slice per
// subcategory plus SPLIT_DIRECT_NAME for the rows assigned to the parent
// itself.

import { DEFAULT_CATEGORY_COLOR } from "@/lib/categories";
import { countsTowardFlow } from "@/lib/transactions";

/** The `categoryId` query value that selects uncategorized rows. */
export const UNCATEGORIZED_PARAM = "none";

export const UNCATEGORIZED_NAME = "Sin categoría";
/** Gray of the uncategorized slice. */
export const UNCATEGORIZED_COLOR = "#cbd5e1";

export const SPEND_GROUP_BYS = [
  "none",
  "month",
  "product",
  "institution",
  "category",
] as const;
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
  /** The top-level category this one is a subcategory of (V046). */
  parentId?: string | null;
}

/** A parent's split: the label of the rows assigned to the parent itself
 *  rather than to one of its subcategories. */
export const SPLIT_DIRECT_NAME = "Sin subcategoría";

/** `rows` with every subcategory's rows moved onto its parent, so each
 *  top-level category sums its subcategories too. Ids missing from
 *  `categoryInfo` stay as they are. */
export function rollUpToParents(
  rows: readonly SpendRow[],
  categoryInfo: ReadonlyMap<string, SpendCategoryInfo>
): SpendRow[] {
  return rows.map((row) => {
    const parentId = row.categoryId
      ? categoryInfo.get(row.categoryId)?.parentId
      : null;
    return parentId ? { ...row, categoryId: parentId } : row;
  });
}

/** `hex` (#rrggbb) mixed toward white by `amount` (0 to 1). */
export function lightenColor(hex: string, amount: number): string {
  const channel = (i: number) => {
    const value = parseInt(hex.slice(i, i + 2), 16);
    return Math.round(value + (255 - value) * amount)
      .toString(16)
      .padStart(2, "0");
  };
  return `#${channel(1)}${channel(3)}${channel(5)}`;
}

/**
 * The category lookup the summaries read, from the categories table's rows.
 * A subcategory without a color of its own takes its parent's. With
 * `splitParentId` (a parent's breakdown), that parent is renamed
 * SPLIT_DIRECT_NAME, since only its direct rows keep its id, and its
 * colorless subcategories get lighter shades of its color so the slices stay
 * apart.
 */
export function categoryInfoFor(
  rows: readonly SpendCategoryInfo[],
  { splitParentId }: { splitParentId?: string } = {}
): Map<string, SpendCategoryInfo> {
  const byId = new Map(rows.map((c) => [c.id, c]));
  const info = new Map<string, SpendCategoryInfo>();
  let shade = 0;
  for (const c of [...rows].sort((a, b) => a.name.localeCompare(b.name, "es"))) {
    const parent = c.parentId ? byId.get(c.parentId) : undefined;
    let color = c.color ?? parent?.color ?? null;
    if (splitParentId && c.parentId === splitParentId && c.color == null) {
      const base = parent?.color ?? DEFAULT_CATEGORY_COLOR;
      color = lightenColor(base, 0.2 + 0.15 * (shade++ % 4));
    }
    info.set(c.id, {
      ...c,
      name: c.id === splitParentId ? SPLIT_DIRECT_NAME : c.name,
      color,
    });
  }
  return info;
}

/** Gross flows of a set of counted rows, in the scope's currency. */
export interface SpendFlows {
  /** Sum of the negative rows, as a positive number. */
  expenses: number;
  /** Sum of the positive rows. */
  income: number;
  /** expenses - income: positive is net spending, negative net income. */
  net: number;
}

export interface CategorySpend extends SpendFlows {
  /** Null for "Sin categoría". */
  categoryId: string | null;
  name: string;
  color: string;
  /** Positive spend in the scope's currency. */
  amount: number;
  /** Share of the scope's total, 0 to 100. */
  percent: number;
}

export interface CategorySpendSummary extends SpendFlows {
  /** Sum of `categories`' amounts: spend only (see the header). */
  total: number;
  /** Largest first; "Sin categoría" always last. */
  categories: CategorySpend[];
}

export interface CategorySpendGroup extends CategorySpendSummary {
  /** YYYY-MM, product id, institution slug, or category id
   *  (UNCATEGORIZED_PARAM for "Sin categoría"). */
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
   *  product's display name, the institution's name or the category's name
   *  (with its color, for category groups). */
  groups?: (CategorySpendGroup & { label: string; color?: string })[];
  /** Expense rows (amount < 0) left out because their currency has no CLP
   *  rate; unconvertible income never counts as spend, so it is not here. */
  unconvertedCount: number;
  /** Income rows (amount > 0) left out of every flow for the same reason. */
  unconvertedIncomeCount: number;
}

function flowsOf(expenses: number, income: number): SpendFlows {
  return { expenses, income, net: expenses - income };
}

/** Spend per category over `rows`, ranked, with shares of the total. */
export function summarizeSpend(
  rows: readonly SpendRow[],
  categoryInfo: ReadonlyMap<string, SpendCategoryInfo>
): CategorySpendSummary {
  // Gross expenses and income per category id ("" = uncategorized).
  const gross = new Map<string, { expenses: number; income: number }>();
  let expenses = 0;
  let income = 0;
  for (const row of rows) {
    if (!countsTowardFlow(row)) continue;
    const key = row.categoryId ?? "";
    const entry = gross.get(key) ?? { expenses: 0, income: 0 };
    if (row.amount < 0) {
      entry.expenses -= row.amount;
      expenses -= row.amount;
    } else {
      entry.income += row.amount;
      income += row.amount;
    }
    gross.set(key, entry);
  }

  const ranked: Omit<CategorySpend, "percent">[] = [];
  let uncategorized: Omit<CategorySpend, "percent"> | null = null;
  for (const [key, entry] of gross) {
    const flows = flowsOf(entry.expenses, entry.income);
    if (key === "") {
      if (entry.expenses > 0) {
        uncategorized = {
          categoryId: null,
          name: UNCATEGORIZED_NAME,
          color: UNCATEGORIZED_COLOR,
          amount: entry.expenses,
          ...flows,
        };
      }
      continue;
    }
    if (!(flows.net > 0)) continue;
    const info = categoryInfo.get(key);
    ranked.push({
      categoryId: key,
      name: info?.name ?? "Categoría",
      color: info?.color ?? DEFAULT_CATEGORY_COLOR,
      amount: flows.net,
      ...flows,
    });
  }
  ranked.sort((a, b) => b.amount - a.amount || a.name.localeCompare(b.name, "es"));
  if (uncategorized) ranked.push(uncategorized);

  const total = ranked.reduce((sum, c) => sum + c.amount, 0);
  return {
    total,
    ...flowsOf(expenses, income),
    categories: ranked.map((c) => ({
      ...c,
      percent: total > 0 ? (c.amount / total) * 100 : 0,
    })),
  };
}

function groupKey(row: SpendRow, groupBy: Exclude<SpendGroupBy, "none">): string {
  if (groupBy === "month") return row.month;
  if (groupBy === "product") return row.productId;
  if (groupBy === "category") return row.categoryId ?? UNCATEGORIZED_PARAM;
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
 * chart keeps its gaps; product, institution and category groups run largest
 * first and leave out those with no spend, unless `keepIncomeGroups` keeps those with
 * income (ranked after the spenders, by income).
 */
export function groupSpend(
  rows: readonly SpendRow[],
  categoryInfo: ReadonlyMap<string, SpendCategoryInfo>,
  groupBy: Exclude<SpendGroupBy, "none">,
  window?: { from: string; to: string },
  { keepIncomeGroups = false }: { keepIncomeGroups?: boolean } = {}
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
    .filter((g) => g.total > 0 || (keepIncomeGroups && g.income > 0))
    .sort(
      (a, b) =>
        b.total - a.total || b.income - a.income || a.key.localeCompare(b.key)
    );
}

/** The first and last YYYY-MM-DD of a YYYY-MM month. */
export function monthWindow(month: string): { from: string; to: string } {
  const [year, m] = month.split("-").map(Number);
  const lastDay = new Date(Date.UTC(year, m, 0)).getUTCDate();
  return { from: `${month}-01`, to: `${month}-${String(lastDay).padStart(2, "0")}` };
}

/** The Gastos page filtered to one category (null = "Sin categoría") over a
 *  day window, optionally within one product or institution and with a
 *  description search (see lib/expenses-filters for how the page reads it). */
export function categoryExpensesHref({
  categoryId,
  from,
  to,
  productId,
  institution,
  q,
}: {
  categoryId: string | null;
  from: string;
  to: string;
  productId?: string;
  institution?: string;
  q?: string;
}): string {
  const params = new URLSearchParams({
    category: categoryId ?? UNCATEGORIZED_PARAM,
    from,
    to,
  });
  if (productId) params.set("product", productId);
  if (institution) params.set("institution", institution);
  if (q?.trim()) params.set("q", q.trim());
  return `/expenses?${params}`;
}

/** Query param holding the category open in the Categorías detail panel: a
 *  category id, or UNCATEGORIZED_PARAM for "Sin categoría". */
export const CATEGORY_PARAM = "category";

/** The detail panel's period: a preset id or `YYYY-MM..YYYY-MM`. */
export const PERIOD_PARAM = "period";

/** The Categorías page with one category's detail panel open, optionally
 *  over a given period. */
export function categoryDetailHref(categoryId: string | null, period?: string): string {
  const params = new URLSearchParams({
    [CATEGORY_PARAM]: categoryId ?? UNCATEGORIZED_PARAM,
  });
  if (period) params.set(PERIOD_PARAM, period);
  return `/categories?${params}`;
}
