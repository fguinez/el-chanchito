"use client";

import { useMemo, useState } from "react";
import { useCategorySpend } from "@/components/charts/use-category-spend";
import {
  categoryTrends,
  monthOf,
  trailingMonthsWindow,
  type CategoryTrend,
} from "@/lib/category-trends";

/**
 * Every category's trend over the `months` months ending with the current
 * one, CLP and every product, from one GET /api/categories/spend?groupBy=month
 * (see lib/category-trends). `trends` is null while loading or on error.
 */
export function useCategoryTrends(
  months: number,
  refreshKey = 0
): {
  trends: Map<string, CategoryTrend> | null;
  currentMonth: string;
  error: boolean;
} {
  // Fixed at mount, so a page left open past midnight keeps one window.
  const [currentMonth] = useState(() => monthOf(new Date()));
  const { from, to } = trailingMonthsWindow(currentMonth, months);
  const query = new URLSearchParams({ from, to, groupBy: "month" }).toString();
  const { data, error } = useCategorySpend(query, refreshKey);
  const trends = useMemo(
    () => (data ? categoryTrends(data.groups ?? [], currentMonth, months) : null),
    [data, currentMonth, months]
  );
  return { trends, currentMonth, error };
}
