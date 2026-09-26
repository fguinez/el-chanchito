// Shared pieces of the Categorías page: typed mirrors of the /api/categories
// response shapes plus the category label and small form helpers.

import { cn } from "@/lib/utils";
import { RULE_PRIORITY_MAX, RULE_PRIORITY_MIN } from "@/lib/categories";
import { CategoryIcon } from "./CategoryIcon";

export interface ApiCategoryRule {
  id: string;
  keyword: string;
  categoryId: string;
  priority: number;
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
  transactionCount: number;
}

export interface RulePreviewTransaction {
  id: string;
  description: string;
  amount: number;
  transactionDate: string;
  categoryId: string | null;
  isManuallyCategorized: boolean;
}

export interface RulePreviewResult {
  keyword: string;
  total: number;
  uncategorized: number;
  transactions: RulePreviewTransaction[];
}

// Native <select> styled to match the Input primitive (as in MonitorForm).
export const SELECT_CLASS =
  "h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-xs transition-[color,box-shadow] outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-input/30";

export const COLOR_INPUT_CLASS =
  "h-9 w-12 shrink-0 cursor-pointer rounded-md border border-input bg-transparent p-1 shadow-xs disabled:cursor-not-allowed disabled:opacity-50";

/** Shown in the color input while a category has no color of its own. */
export const DEFAULT_CATEGORY_COLOR = "#94a3b8";

export const NAME_CLASH_MESSAGE = "Ya existe una categoría con ese nombre";

/** The `error` of a failed API response; `byStatus` swaps in a Spanish
 *  message for statuses the UI can explain better. */
export async function readApiError(
  res: Response,
  fallback: string,
  byStatus: Partial<Record<number, string>> = {}
): Promise<string> {
  if (byStatus[res.status]) return byStatus[res.status]!;
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

/** Swatch, icon, and name of a category. */
export function CategoryLabel({
  category,
}: {
  category: Pick<ApiCategory, "name" | "color" | "icon">;
}) {
  return (
    <span className="inline-flex items-center gap-2">
      <CategorySwatch color={category.color} />
      <CategoryIcon
        icon={category.icon}
        className="size-4 shrink-0 text-muted-foreground"
      />
      <span>{category.name}</span>
    </span>
  );
}
