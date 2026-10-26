"use client";

// Análisis: monthly spending by category over a trailing window, as stacked
// bars and as a category x month table with the month-over-month change.
// One GET /api/categories/spend?groupBy=month feeds both.

import { useMemo, useState } from "react";
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
import {
  Table,
  TableBody,
  TableCell,
  TableFooter,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  CategoryStackedBars,
  formatSpendMonth,
} from "@/components/charts/category-stacked-bars";
import { useCategorySpend } from "@/components/charts/use-category-spend";
import {
  categoryDetailHref,
  categoryExpensesHref,
  monthWindow,
} from "@/lib/category-spend";
import {
  categoryMonthMatrix,
  monthOf,
  trailingMonths,
  trailingMonthsWindow,
  trendKey,
  type SpendMatrixCell,
  type SpendMatrixRow,
} from "@/lib/category-trends";
import { cn, formatCLP, formatPercent } from "@/lib/utils";

const RANGE_OPTIONS = [6, 12, 24] as const;
type RangeMonths = (typeof RANGE_OPTIONS)[number];

export default function AnalysisPage() {
  // Fixed at mount, so a page left open past midnight keeps one window.
  const [currentMonth] = useState(() => monthOf(new Date()));
  const [range, setRange] = useState<RangeMonths>(12);

  const months = useMemo(
    () => trailingMonths(currentMonth, range),
    [currentMonth, range]
  );
  // One month more than shown, so the first column has a change too.
  const query = new URLSearchParams({
    ...trailingMonthsWindow(currentMonth, range + 1),
    groupBy: "month",
  }).toString();
  const spend = useCategorySpend(query);

  const groups = spend.data?.groups;
  const shownGroups = useMemo(
    () => groups?.filter((g) => months.includes(g.key)) ?? [],
    [groups, months]
  );
  const matrix = useMemo(
    () => (groups ? categoryMonthMatrix(groups, months, currentMonth) : null),
    [groups, months, currentMonth]
  );
  const expensesHref = (categoryId: string | null, month: string) =>
    categoryExpensesHref({ categoryId, ...monthWindow(month) });

  return (
    <div className="space-y-6">
      <p className="max-w-2xl text-sm text-muted-foreground">
        Gasto mensual por categoría en CLP, sin transferencias internas. Elige
        una barra o un monto para ver sus transacciones.
      </p>

      <Card>
        <CardHeader>
          <CardTitle>Gasto mensual por categoría</CardTitle>
          <CardDescription>
            Últimos {range} meses, incluido el mes en curso.
          </CardDescription>
          <CardAction>
            <div className="flex items-center gap-1" role="group" aria-label="Período">
              {RANGE_OPTIONS.map((option) => (
                <Button
                  key={option}
                  variant={range === option ? "secondary" : "ghost"}
                  size="xs"
                  aria-pressed={range === option}
                  onClick={() => setRange(option)}
                >
                  {option} meses
                </Button>
              ))}
            </div>
          </CardAction>
        </CardHeader>
        <CardContent>
          {spend.error ? (
            <ErrorNote />
          ) : !spend.data ? (
            <Loading />
          ) : (
            <>
              <CategoryStackedBars
                groups={shownGroups}
                currency={spend.data.currency}
                height={320}
                hrefFor={expensesHref}
                emptyMessage={`Sin gastos en los últimos ${range} meses.`}
              />
              {spend.data.unconvertedCount > 0 && (
                <p className="mt-3 text-xs text-muted-foreground">
                  {spend.data.unconvertedCount === 1
                    ? "1 gasto en una moneda sin tipo de cambio no se incluye."
                    : `${spend.data.unconvertedCount} gastos en monedas sin tipo de cambio no se incluyen.`}
                </p>
              )}
            </>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Categorías por mes</CardTitle>
          <CardDescription>
            Cada monto con su cambio respecto al mes anterior (rojo si gastaste
            más). El promedio deja fuera el mes en curso.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {spend.error ? (
            <ErrorNote />
          ) : !matrix ? (
            <Loading />
          ) : matrix.rows.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              Sin gastos en los últimos {range} meses.
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow className="hover:bg-transparent">
                  <TableHead className={STICKY}>Categoría</TableHead>
                  {matrix.months.map((month) => (
                    <TableHead key={month} className="text-right">
                      {formatSpendMonth(month)}
                      {month === currentMonth && (
                        <span className="block text-xs font-normal text-muted-foreground">
                          en curso
                        </span>
                      )}
                    </TableHead>
                  ))}
                  <TableHead className="text-right">Total</TableHead>
                  <TableHead className="text-right">Promedio</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {matrix.rows.map((row) => (
                  <MatrixRow
                    key={trendKey(row.categoryId)}
                    row={row}
                    expensesHref={expensesHref}
                  />
                ))}
              </TableBody>
              <TableFooter className="bg-muted">
                <TableRow className="hover:bg-transparent">
                  <TableCell className={cn(STICKY, "bg-muted font-medium")}>
                    Total
                  </TableCell>
                  {matrix.totals.cells.map((cell) => (
                    <TableCell key={cell.month} className="text-right align-top">
                      <Amount cell={cell} />
                    </TableCell>
                  ))}
                  <Summary total={matrix.totals.total} average={matrix.totals.average} />
                </TableRow>
              </TableFooter>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

/** The category column stays in view while the months scroll sideways. */
const STICKY = "sticky left-0 z-10 bg-card";

function MatrixRow({
  row,
  expensesHref,
}: {
  row: SpendMatrixRow;
  expensesHref: (categoryId: string | null, month: string) => string;
}) {
  return (
    <TableRow className="hover:bg-transparent">
      <TableCell className={cn(STICKY, "max-w-48 font-medium")}>
        <Link
          href={categoryDetailHref(row.categoryId)}
          className={cn(
            "inline-flex min-w-0 items-center gap-2 rounded-sm hover:underline focus-visible:outline-2 focus-visible:outline-ring",
            row.categoryId === null && "text-muted-foreground"
          )}
        >
          <span
            aria-hidden
            className="size-2.5 shrink-0 rounded-sm"
            style={{ backgroundColor: row.color }}
          />
          <span className="truncate">{row.name}</span>
        </Link>
      </TableCell>
      {row.cells.map((cell) => (
        <TableCell key={cell.month} className="text-right align-top">
          {cell.amount > 0 ? (
            <Link
              href={expensesHref(row.categoryId, cell.month)}
              className="rounded-sm hover:underline focus-visible:outline-2 focus-visible:outline-ring"
            >
              <Amount cell={cell} />
            </Link>
          ) : (
            <Amount cell={cell} />
          )}
        </TableCell>
      ))}
      <Summary total={row.total} average={row.average} />
    </TableRow>
  );
}

/** A month's amount over its change from the month before. */
function Amount({ cell }: { cell: SpendMatrixCell }) {
  return (
    <span className="inline-flex flex-col items-end">
      <span
        className={cn(
          "tabular-nums",
          cell.amount > 0 ? "" : "text-muted-foreground"
        )}
      >
        {cell.amount > 0 ? formatCLP(Math.round(cell.amount)) : "-"}
      </span>
      <Change change={cell.change} />
    </span>
  );
}

/** The change from the month before; more spending reads red, less green. */
function Change({ change }: { change: number | null }) {
  if (change === null) return null;
  const up = change > 0;
  const Icon = up ? ArrowUp : ArrowDown;
  const direction = change === 0 ? "Sin cambio" : up ? "Sube" : "Baja";
  return (
    <span
      className={cn(
        "inline-flex items-center gap-0.5 text-xs tabular-nums",
        change === 0
          ? "text-muted-foreground"
          : up
            ? "text-red-600"
            : "text-green-600"
      )}
    >
      {change !== 0 && <Icon className="size-3" aria-hidden />}
      <span className="sr-only">{direction} </span>
      {formatPercent(Math.abs(change))}
    </span>
  );
}

function Summary({ total, average }: { total: number; average: number | null }) {
  return (
    <>
      <TableCell className="text-right align-top font-medium tabular-nums">
        {formatCLP(Math.round(total))}
      </TableCell>
      <TableCell className="text-right align-top tabular-nums text-muted-foreground">
        {average === null ? "-" : formatCLP(Math.round(average))}
      </TableCell>
    </>
  );
}

function Loading() {
  return <p className="text-sm text-muted-foreground">Cargando...</p>;
}

function ErrorNote() {
  return (
    <p className="text-sm text-muted-foreground">
      No se pudieron cargar los gastos por categoría.
    </p>
  );
}
