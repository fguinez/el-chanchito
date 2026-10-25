"use client";

// The spending columns of the Categorías table (this month, last month, the
// 3-month average and a 6-month sparkline), the "Sin categoría" row, and the
// row props that open a category's detail panel (?category=).

import { useCallback, type MouseEvent } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { TableCell, TableHead, TableRow } from "@/components/ui/table";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { Sparkline } from "@/components/charts/sparkline";
import { formatCLP } from "@/lib/utils";
import {
  categoryDetailHref,
  UNCATEGORIZED_COLOR,
  UNCATEGORIZED_NAME,
} from "@/lib/category-spend";
import {
  AVERAGE_MONTHS,
  trendKey,
  type CategoryTrend,
} from "@/lib/category-trends";
import { CategorySwatch, DEFAULT_CATEGORY_COLOR } from "./shared";

/** Months the sparkline (and the one spend request behind the table) spans. */
export const TREND_MONTHS = 6;

export type TrendSortKey = "este_mes" | "mes_anterior" | "promedio";

/** The sort value of a trend column; zero when the category spent nothing. */
export function trendSortValue(
  trend: CategoryTrend | undefined,
  key: TrendSortKey
): number {
  if (!trend) return 0;
  if (key === "este_mes") return trend.thisMonth;
  if (key === "mes_anterior") return trend.lastMonth;
  return trend.average;
}

export function CategoryTrendHeads({
  sortProps,
}: {
  sortProps: (key: TrendSortKey) => {
    columnKey: string;
    active: boolean;
    direction: "asc" | "desc" | undefined;
    onSort: (key: string) => void;
  };
}) {
  return (
    <>
      <SortableTableHead label="Este mes" align="right" {...sortProps("este_mes")} />
      <SortableTableHead
        label="Mes anterior"
        align="right"
        {...sortProps("mes_anterior")}
      />
      <SortableTableHead
        label={`Prom. ${AVERAGE_MONTHS} meses`}
        align="right"
        {...sortProps("promedio")}
      />
      <TableHead className="text-right">{TREND_MONTHS} meses</TableHead>
    </>
  );
}

/** The four spend cells of one category; `trends` is null while loading (or
 *  after an error), and a category missing from it spent nothing. */
export function CategoryTrendCells({
  trends,
  categoryId,
  color,
}: {
  trends: Map<string, CategoryTrend> | null;
  categoryId: string | null;
  color: string | null;
}) {
  if (!trends) {
    return (
      <>
        {[0, 1, 2, 3].map((i) => (
          <TableCell key={i} className="text-right text-muted-foreground">
            ...
          </TableCell>
        ))}
      </>
    );
  }
  const trend = trends.get(trendKey(categoryId));
  const amount = (value: number) => (
    <TableCell
      className={`text-right tabular-nums ${value > 0 ? "" : "text-muted-foreground"}`}
    >
      {formatCLP(Math.round(value))}
    </TableCell>
  );
  const series = trend?.series ?? new Array<number>(TREND_MONTHS).fill(0);
  return (
    <>
      {amount(trend?.thisMonth ?? 0)}
      {amount(trend?.lastMonth ?? 0)}
      {amount(trend?.average ?? 0)}
      <TableCell className="text-right">
        <Sparkline
          values={series}
          color={color ?? DEFAULT_CATEGORY_COLOR}
          label={`Gasto de los últimos ${TREND_MONTHS} meses: ${series
            .map((v) => formatCLP(Math.round(v)))
            .join(", ")}`}
        />
      </TableCell>
    </>
  );
}

/** Row props that open `categoryId`'s detail panel on a click anywhere but
 *  the row's controls; keyboard users reach it through the name's link
 *  (CategoryDetailLink), so the row is not a second tab stop. */
export function useOpenCategoryRow() {
  const router = useRouter();
  return useCallback(
    (categoryId: string | null) => {
      return {
        className: "cursor-pointer",
        onClick: (e: MouseEvent<HTMLTableRowElement>) => {
          if ((e.target as HTMLElement).closest("button, input, a, select")) return;
          router.replace(categoryDetailHref(categoryId), { scroll: false });
        },
      };
    },
    [router]
  );
}

/** A category's name as a link to its detail panel. */
export function CategoryDetailLink({
  categoryId,
  children,
}: {
  categoryId: string | null;
  children: React.ReactNode;
}) {
  return (
    <Link
      href={categoryDetailHref(categoryId)}
      replace
      scroll={false}
      className="rounded-sm hover:underline focus-visible:outline-2 focus-visible:outline-ring"
    >
      {children}
    </Link>
  );
}

/** "Sin categoría" under the real categories, when it spent anything in the
 *  table's window. `trailingCells` fills the remaining columns. */
export function UncategorizedRow({
  trends,
  trailingCells,
}: {
  trends: Map<string, CategoryTrend> | null;
  trailingCells: number;
}) {
  const openRow = useOpenCategoryRow();
  if (!trends?.has(trendKey(null))) return null;
  return (
    <TableRow {...openRow(null)}>
      <TableCell className="font-medium">
        <CategoryDetailLink categoryId={null}>
          <span className="inline-flex items-center gap-2 text-muted-foreground">
            <CategorySwatch color={UNCATEGORIZED_COLOR} />
            {UNCATEGORIZED_NAME}
          </span>
        </CategoryDetailLink>
      </TableCell>
      <CategoryTrendCells
        trends={trends}
        categoryId={null}
        color={UNCATEGORIZED_COLOR}
      />
      {Array.from({ length: trailingCells }, (_, i) => (
        <TableCell key={i} />
      ))}
    </TableRow>
  );
}
