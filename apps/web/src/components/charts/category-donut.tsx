"use client";

// Spending per category as a donut plus a ranked legend (share and amount),
// in each category's own color. A slice and its legend row link to the same
// place when `hrefFor` is given, or call `onSelect` instead; with neither the
// chart is read-only. Feed it a summary from GET /api/categories/spend.

import Link from "next/link";
import { useRouter } from "next/navigation";
import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";
import { cn, formatAmount, formatPercent } from "@/lib/utils";
import type { CategorySpend } from "@/lib/category-spend";

/** Stable key of a category, "none" for "Sin categoría". */
export function categoryKey(categoryId: string | null): string {
  return categoryId ?? "none";
}

export function CategoryDonut({
  categories,
  total,
  currency,
  hrefFor,
  onSelect,
  selectedId,
  emptyMessage = "Sin gastos en este período.",
  className,
}: {
  categories: CategorySpend[];
  total: number;
  currency: string;
  hrefFor?: (categoryId: string | null) => string;
  onSelect?: (categoryId: string | null) => void;
  /** Highlights one category (null = "Sin categoría"); undefined = none. */
  selectedId?: string | null;
  emptyMessage?: string;
  className?: string;
}) {
  const router = useRouter();
  if (categories.length === 0) {
    return <p className="text-sm text-muted-foreground">{emptyMessage}</p>;
  }

  const interactive = hrefFor != null || onSelect != null;
  const select = (categoryId: string | null) => {
    if (onSelect) onSelect(categoryId);
    else if (hrefFor) router.push(hrefFor(categoryId));
  };
  const dimmed = (categoryId: string | null) =>
    selectedId !== undefined && selectedId !== categoryId;

  return (
    <div
      className={cn(
        "flex flex-col items-center gap-6 sm:flex-row sm:items-start",
        className
      )}
    >
      <div className="relative h-52 w-52 shrink-0">
        <ResponsiveContainer width="100%" height="100%">
          <PieChart>
            <Pie
              data={categories}
              dataKey="amount"
              nameKey="name"
              innerRadius={64}
              outerRadius={92}
              paddingAngle={categories.length > 1 ? 1 : 0}
              strokeWidth={0}
              isAnimationActive={false}
              onClick={
                interactive
                  ? (_, index) => select(categories[index].categoryId)
                  : undefined
              }
              className={interactive ? "cursor-pointer" : undefined}
            >
              {categories.map((c) => (
                <Cell
                  key={categoryKey(c.categoryId)}
                  fill={c.color}
                  fillOpacity={dimmed(c.categoryId) ? 0.3 : 1}
                />
              ))}
            </Pie>
            <Tooltip
              formatter={(value, name) => [
                formatAmount(currency, Number(value)),
                name,
              ]}
            />
          </PieChart>
        </ResponsiveContainer>
        <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
          <span className="text-lg font-semibold tabular-nums">
            {formatAmount(currency, total)}
          </span>
          <span className="text-xs text-muted-foreground">gastado</span>
        </div>
      </div>

      <ol className="w-full min-w-0 flex-1 space-y-0.5 text-sm">
        {categories.map((c) => {
          const content = (
            <>
              <span
                aria-hidden
                className="size-2.5 shrink-0 rounded-sm"
                style={{ backgroundColor: c.color }}
              />
              <span className="min-w-0 flex-1 truncate">{c.name}</span>
              <span className="w-14 text-right tabular-nums text-muted-foreground">
                {formatPercent(c.percent)}
              </span>
              <span className="w-28 text-right font-medium tabular-nums">
                {formatAmount(currency, c.amount)}
              </span>
            </>
          );
          const rowClass = cn(
            "flex items-center gap-2 rounded-md px-2 py-1",
            interactive && "hover:bg-muted",
            dimmed(c.categoryId) && "opacity-50",
            selectedId === c.categoryId && "bg-muted"
          );
          return (
            <li key={categoryKey(c.categoryId)}>
              {hrefFor && !onSelect ? (
                <Link href={hrefFor(c.categoryId)} className={rowClass}>
                  {content}
                </Link>
              ) : onSelect ? (
                <button
                  type="button"
                  onClick={() => onSelect(c.categoryId)}
                  className={cn(rowClass, "w-full text-left")}
                >
                  {content}
                </button>
              ) : (
                <div className={rowClass}>{content}</div>
              )}
            </li>
          );
        })}
      </ol>
    </div>
  );
}
