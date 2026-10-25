"use client";

// "Gastos por categoría" on the product page: the product's spending per
// category over the card's time window, as a donut or as monthly stacked
// bars, in the product's own currency (GET /api/categories/spend). A slice,
// legend row or bar segment opens Gastos filtered to that category.

import { useEffect, useState } from "react";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { TimeRangeControl } from "@/components/charts/time-range-control";
import { rangeDayWindow, type XAxisRange } from "@/components/charts/x-axis-range";
import { CategoryDonut } from "@/components/charts/category-donut";
import { CategoryStackedBars } from "@/components/charts/category-stacked-bars";
import {
  categoryExpensesHref,
  monthWindow,
  type CategorySpendResponse,
} from "@/lib/category-spend";

type View = "total" | "month";

const INITIAL_RANGE: XAxisRange = { kind: "days", days: 90 };

export function CategorySpendCard({ productId }: { productId: string }) {
  const [range, setRange] = useState<XAxisRange>(INITIAL_RANGE);
  const [view, setView] = useState<View>("total");
  // Each result remembers the query it answers, so a new window shows
  // loading (or its own error) instead of the previous window's figures.
  const [result, setResult] = useState<
    { query: string; data: CategorySpendResponse | null; error: boolean } | null
  >(null);

  // The control offers no "Todo", so every range it sets has a day window.
  const { from, to } = rangeDayWindow(range) ?? rangeDayWindow(INITIAL_RANGE)!;
  const query = new URLSearchParams({ from, to, productId, groupBy: "month" }).toString();
  const current = result?.query === query ? result : null;
  const data = current?.data ?? null;
  const error = current?.error ?? false;

  useEffect(() => {
    let cancelled = false;
    fetch(`/api/categories/spend?${query}`)
      .then((res) => {
        if (!res.ok) throw new Error("failed");
        return res.json();
      })
      .then((body: CategorySpendResponse) => {
        if (!cancelled) setResult({ query, data: body, error: false });
      })
      .catch(() => {
        if (!cancelled) setResult({ query, data: null, error: true });
      });
    return () => {
      cancelled = true;
    };
  }, [query]);

  const hrefFor = (categoryId: string | null) =>
    categoryExpensesHref({ categoryId, from, to });
  // A bar segment narrows to its month, kept inside the card's window.
  const monthHrefFor = (categoryId: string | null, month: string) => {
    const bounds = monthWindow(month);
    return categoryExpensesHref({
      categoryId,
      from: bounds.from < from ? from : bounds.from,
      to: bounds.to > to ? to : bounds.to,
    });
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Gastos por categoría</CardTitle>
        <CardDescription>
          Lo gastado con este producto en cada categoría, sin transferencias internas
        </CardDescription>
        <CardAction className="flex flex-wrap items-center justify-end gap-2">
          <div className="flex items-center gap-1" role="group" aria-label="Vista">
            <Button
              variant={view === "total" ? "secondary" : "ghost"}
              size="xs"
              onClick={() => setView("total")}
            >
              Total
            </Button>
            <Button
              variant={view === "month" ? "secondary" : "ghost"}
              size="xs"
              onClick={() => setView("month")}
            >
              Por mes
            </Button>
          </div>
          <TimeRangeControl control={{ range, setRange }} />
        </CardAction>
      </CardHeader>
      <CardContent>
        {error ? (
          <p className="text-sm text-muted-foreground">
            No se pudieron cargar los gastos por categoría.
          </p>
        ) : !data ? (
          <p className="text-sm text-muted-foreground">Cargando...</p>
        ) : view === "total" ? (
          <CategoryDonut
            categories={data.categories}
            total={data.total}
            currency={data.currency}
            hrefFor={hrefFor}
          />
        ) : (
          <CategoryStackedBars
            groups={data.groups ?? []}
            currency={data.currency}
            hrefFor={monthHrefFor}
          />
        )}
      </CardContent>
    </Card>
  );
}
