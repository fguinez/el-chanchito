"use client";

// Monthly spending stacked by category, in each category's own color. Feed it
// the month groups of GET /api/categories/spend?groupBy=month (every month of
// the window, empty ones included). Clicking a segment calls `onSelect` with
// its category and month, or follows `hrefFor`.

import { useRouter } from "next/navigation";
import { Bar, BarChart, CartesianGrid, Tooltip, XAxis, YAxis } from "recharts";
import { InteractiveChart } from "@/components/charts/interactive-chart";
import { useYAxisRange } from "@/components/charts/y-axis-range";
import { categoryKey } from "@/components/charts/category-donut";
import { formatAmount, formatAxisValue } from "@/lib/utils";
import type { CategorySpend, CategorySpendGroup } from "@/lib/category-spend";

/** YYYY-MM -> "ene 26" (es-CL, short month). */
export function formatSpendMonth(month: string): string {
  const [year, m] = month.split("-").map(Number);
  return new Date(year, m - 1, 1).toLocaleDateString("es-CL", {
    month: "short",
    year: "2-digit",
  });
}

/** Every category of the groups once, largest overall first, "Sin categoría"
 *  last, so the stack order and colors stay the same in every bar. */
function seriesOf(groups: CategorySpendGroup[]): CategorySpend[] {
  const byKey = new Map<string, CategorySpend>();
  for (const group of groups) {
    for (const c of group.categories) {
      const key = categoryKey(c.categoryId);
      const seen = byKey.get(key);
      byKey.set(key, seen ? { ...seen, amount: seen.amount + c.amount } : c);
    }
  }
  return [...byKey.values()].sort((a, b) => {
    if ((a.categoryId == null) !== (b.categoryId == null)) {
      return a.categoryId == null ? 1 : -1;
    }
    return b.amount - a.amount;
  });
}

export function CategoryStackedBars({
  groups,
  currency,
  height = 260,
  hrefFor,
  onSelect,
  emptyMessage = "Sin gastos en este período.",
}: {
  groups: CategorySpendGroup[];
  currency: string;
  height?: number;
  hrefFor?: (categoryId: string | null, month: string) => string;
  onSelect?: (categoryId: string | null, month: string) => void;
  emptyMessage?: string;
}) {
  const router = useRouter();
  const yAxis = useYAxisRange();
  const series = seriesOf(groups);
  if (series.length === 0) {
    return <p className="text-sm text-muted-foreground">{emptyMessage}</p>;
  }

  const data = groups.map((group) => {
    const row: Record<string, number | string> = {
      month: group.key,
      label: formatSpendMonth(group.key),
    };
    for (const c of group.categories) row[categoryKey(c.categoryId)] = c.amount;
    return row;
  });
  const interactive = hrefFor != null || onSelect != null;
  const select = (categoryId: string | null, month: string) => {
    if (onSelect) onSelect(categoryId, month);
    else if (hrefFor) router.push(hrefFor(categoryId, month));
  };

  return (
    <InteractiveChart yControl={yAxis} height={height}>
      <BarChart data={data}>
        <CartesianGrid strokeDasharray="3 3" vertical={false} />
        <XAxis dataKey="label" fontSize={12} />
        <YAxis tickFormatter={formatAxisValue} fontSize={12} {...yAxis.yAxisProps} />
        <Tooltip
          formatter={(value, name) => [formatAmount(currency, Number(value)), name]}
        />
        {series.map((c) => (
          <Bar
            key={categoryKey(c.categoryId)}
            dataKey={categoryKey(c.categoryId)}
            name={c.name}
            stackId="spend"
            fill={c.color}
            isAnimationActive={false}
            className={interactive ? "cursor-pointer" : undefined}
            onClick={
              interactive
                ? (entry) => {
                    const month = (entry as { payload?: { month?: string } }).payload?.month;
                    if (month) select(c.categoryId, month);
                  }
                : undefined
            }
          />
        ))}
      </BarChart>
    </InteractiveChart>
  );
}
