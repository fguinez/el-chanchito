"use client";

// Inicio's "Gastos por categoría": the five categories that spent the most
// this month next to last month, with the change (red when spending went up).
// Each row opens the category's detail panel on Categorías.

import Link from "next/link";
import { ArrowDown, ArrowUp } from "lucide-react";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { useCategoryTrends } from "@/components/charts/use-category-trends";
import { categoryDetailHref } from "@/lib/category-spend";
import {
  percentChange,
  topThisMonth,
  type CategoryTrend,
} from "@/lib/category-trends";
import { cn, formatCLP, formatPercent } from "@/lib/utils";

const TOP_COUNT = 5;

export function CategoryTrendsCard() {
  // This month and last month are all the card compares.
  const { trends, error } = useCategoryTrends(2);
  const top = trends ? topThisMonth(trends.values(), TOP_COUNT) : null;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Gastos por categoría</CardTitle>
        <CardDescription>
          Las {TOP_COUNT} categorías con más gasto este mes, contra el mes
          anterior completo (CLP, sin transferencias internas).
        </CardDescription>
        <CardAction>
          <Button asChild variant="outline" size="sm">
            <Link href="/categories">Ver todas</Link>
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent>
        {error ? (
          <p className="text-sm text-muted-foreground">
            No se pudieron cargar los gastos por categoría.
          </p>
        ) : !top ? (
          <p className="text-sm text-muted-foreground">Cargando...</p>
        ) : top.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            Todavía no hay gastos este mes.
          </p>
        ) : (
          <ol className="space-y-0.5 text-sm">
            <li
              aria-hidden
              className="flex items-center gap-2 px-2 pb-1 text-xs text-muted-foreground"
            >
              <span className="flex-1">Categoría</span>
              <span className="w-28 text-right">Este mes</span>
              <span className="hidden w-28 text-right sm:inline">
                Mes anterior
              </span>
              <span className="w-20 text-right">Cambio</span>
            </li>
            {top.map((trend) => (
              <li key={trend.categoryId ?? "none"}>
                <Link
                  href={categoryDetailHref(trend.categoryId)}
                  className="flex items-center gap-2 rounded-md px-2 py-1 hover:bg-muted"
                >
                  <span
                    aria-hidden
                    className="size-2.5 shrink-0 rounded-sm"
                    style={{ backgroundColor: trend.color }}
                  />
                  <span className="min-w-0 flex-1 truncate">{trend.name}</span>
                  <span className="w-28 text-right font-medium tabular-nums">
                    {formatCLP(Math.round(trend.thisMonth))}
                  </span>
                  <span className="hidden w-28 text-right tabular-nums text-muted-foreground sm:inline">
                    {formatCLP(Math.round(trend.lastMonth))}
                  </span>
                  <Change trend={trend} />
                </Link>
              </li>
            ))}
          </ol>
        )}
      </CardContent>
    </Card>
  );
}

/** The change from last month; more spending reads red, less reads green. */
function Change({ trend }: { trend: CategoryTrend }) {
  const change = percentChange(trend.thisMonth, trend.lastMonth);
  if (change === null) {
    return (
      <span className="w-20 text-right text-xs text-muted-foreground">Nuevo</span>
    );
  }
  const up = change > 0;
  const Icon = up ? ArrowUp : ArrowDown;
  return (
    <span
      className={cn(
        "inline-flex w-20 items-center justify-end gap-0.5 tabular-nums",
        change === 0
          ? "text-muted-foreground"
          : up
            ? "text-red-600"
            : "text-green-600"
      )}
      aria-label={`${up ? "Sube" : "Baja"} ${formatPercent(Math.abs(change))} respecto al mes anterior`}
    >
      {change !== 0 && <Icon className="size-3" aria-hidden />}
      {formatPercent(Math.abs(change))}
    </span>
  );
}
