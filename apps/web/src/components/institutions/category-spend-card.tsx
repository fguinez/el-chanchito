"use client";

// "Gastos por categoría" on the product and institution pages: the scope's
// spending per category over the card's time window, as a donut or as monthly
// stacked bars (GET /api/categories/spend). A product keeps its own currency;
// an institution's products are summed in CLP. A slice, legend row or bar
// segment opens Gastos filtered to that category within the same scope.

import { useState } from "react";
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
import { useCategorySpend } from "@/components/charts/use-category-spend";
import { categoryExpensesHref, monthWindow } from "@/lib/category-spend";

type View = "total" | "month";

/** One product, or every product of one institution (by slug). */
export type CategorySpendScope = { productId: string } | { institution: string };

const INITIAL_RANGE: XAxisRange = { kind: "days", days: 90 };

export function CategorySpendCard({ scope }: { scope: CategorySpendScope }) {
  const [range, setRange] = useState<XAxisRange>(INITIAL_RANGE);
  const [view, setView] = useState<View>("total");

  // The control offers no "Todo", so every range it sets has a day window.
  const { from, to } = rangeDayWindow(range) ?? rangeDayWindow(INITIAL_RANGE)!;
  const scopeParams: { productId?: string; institution?: string } =
    "productId" in scope
      ? { productId: scope.productId }
      : { institution: scope.institution };
  const params = new URLSearchParams({ from, to, groupBy: "month" });
  if (scopeParams.productId) params.set("productId", scopeParams.productId);
  if (scopeParams.institution) params.set("institution", scopeParams.institution);
  const query = params.toString();
  const { data, error } = useCategorySpend(query);

  const hrefFor = (categoryId: string | null) =>
    categoryExpensesHref({ categoryId, from, to, ...scopeParams });
  // A bar segment narrows to its month, kept inside the card's window.
  const monthHrefFor = (categoryId: string | null, month: string) => {
    const bounds = monthWindow(month);
    return categoryExpensesHref({
      categoryId,
      from: bounds.from < from ? from : bounds.from,
      to: bounds.to > to ? to : bounds.to,
      ...scopeParams,
    });
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Gastos por categoría</CardTitle>
        <CardDescription>
          {"productId" in scope
            ? "Lo gastado con este producto en cada categoría, sin transferencias internas"
            : "Lo gastado con los productos de esta institución en cada categoría, en CLP y sin transferencias internas"}
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
        {data && data.unconvertedCount > 0 && (
          <p className="mt-3 text-xs text-muted-foreground">
            {data.unconvertedCount === 1
              ? "1 gasto en una moneda sin tipo de cambio no se incluye."
              : `${data.unconvertedCount} gastos en monedas sin tipo de cambio no se incluyen.`}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
