// Shared pieces of the Categorías page: typed mirrors of the /api/categories
// response shapes plus the category label and small form helpers.

import { cn } from "@/lib/utils";
import {
  RULE_PRIORITY_MAX,
  RULE_PRIORITY_MIN,
  type RuleMatchType,
} from "@/lib/categories";
import { CATEGORY_PATH_SEPARATOR } from "@/lib/category-tree";
import { CategoryIcon } from "./CategoryIcon";

export interface ApiCategoryRule {
  id: string;
  keyword: string;
  categoryId: string;
  priority: number;
  matchType: RuleMatchType;
  createdAt: string;
}

export interface ApiCategory {
  id: string;
  name: string;
  parentId: string | null;
  color: string | null;
  icon: string | null;
  createdAt: string;
  rules: ApiCategoryRule[];
  /** Rows assigned to the category, its subcategories included for a
   *  top-level category (V046). */
  transactionCount: number;
  /** Rows assigned to the category itself. */
  directTransactionCount: number;
}

export interface RulePreviewTransaction {
  id: string;
  /** The raw scraped text the rule matches. */
  description: string;
  /** What the row shows today (custom description or alias, else `description`). */
  displayDescription: string;
  amount: number;
  currency: string;
  transactionDate: string;
  categoryId: string | null;
  isManuallyCategorized: boolean;
}

export interface RulePreviewResult {
  keyword: string;
  matchType: RuleMatchType;
  total: number;
  uncategorized: number;
  transactions: RulePreviewTransaction[];
}

// Native <select> styled to match the Input primitive (as in MonitorForm).
export const SELECT_CLASS =
  "h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-xs transition-[color,box-shadow] outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-input/30";

export const COLOR_INPUT_CLASS =
  "h-9 w-12 shrink-0 cursor-pointer rounded-md border border-input bg-transparent p-1 shadow-xs disabled:cursor-not-allowed disabled:opacity-50";

export { DEFAULT_CATEGORY_COLOR } from "@/lib/categories";

/** Spanish labels of the rule match types (V041). */
export const MATCH_TYPE_LABELS: Record<RuleMatchType, string> = {
  contains: "Contiene",
  exact: "Exacta",
};

export const NAME_CLASH_MESSAGE = "Ya existe una categoría con ese nombre";

/** The message for a failed API response: the `byStatus` entry for its
 *  status if any, the server's `error` for a validation failure (400), and
 *  the Spanish `fallback` otherwise. */
export async function readApiError(
  res: Response,
  fallback: string,
  byStatus: Partial<Record<number, string>> = {}
): Promise<string> {
  if (byStatus[res.status]) return byStatus[res.status]!;
  if (res.status !== 400) return fallback;
  try {
    const data = await res.json();
    if (typeof data?.error === "string") return data.error;
  } catch {
    // Not JSON: use the fallback.
  }
  return fallback;
}

export const PRIORITY_ERROR = `La prioridad debe ser un número entero entre ${RULE_PRIORITY_MIN} y ${RULE_PRIORITY_MAX}`;

/** The priority typed in a rule form, or null when it is not an integer in
 *  the allowed range. */
export function parsePriority(raw: string): number | null {
  const value = Number(raw);
  return raw.trim() !== "" &&
    Number.isInteger(value) &&
    value >= RULE_PRIORITY_MIN &&
    value <= RULE_PRIORITY_MAX
    ? value
    : null;
}

/** "1 regla" / "3 reglas", with an es-CL formatted count. */
export function plural(n: number, one: string, many: string): string {
  return `${n.toLocaleString("es-CL")} ${n === 1 ? one : many}`;
}

export function CategorySwatch({
  color,
  className,
}: {
  color: string | null;
  className?: string;
}) {
  return (
    <span
      aria-hidden
      className={cn(
        "inline-block size-3 shrink-0 rounded-full border",
        className
      )}
      style={color ? { backgroundColor: color, borderColor: color } : undefined}
    />
  );
}

/** Swatch, icon, and name of a category. With `byId`, a subcategory reads
 *  "Padre / Sub" (the parent muted) and shows its parent's color when it has
 *  none of its own. */
export function CategoryLabel({
  category,
  byId,
}: {
  category: Pick<ApiCategory, "name" | "color" | "icon"> & {
    parentId?: string | null;
  };
  byId?: ReadonlyMap<string, Pick<ApiCategory, "name" | "color">>;
}) {
  const parent =
    byId && category.parentId ? byId.get(category.parentId) : undefined;
  return (
    <span className="inline-flex items-center gap-2">
      <CategorySwatch color={category.color ?? parent?.color ?? null} />
      <CategoryIcon
        icon={category.icon}
        className="size-4 shrink-0 text-muted-foreground"
      />
      <span>
        {parent && (
          <span className="text-muted-foreground">
            {parent.name}
            {CATEGORY_PATH_SEPARATOR}
          </span>
        )}
        {category.name}
      </span>
    </span>
  );
}
