"use client";

// Side panel with one category's spending, open while the URL carries
// `?category=<id|none>` (the same pattern as the `?txn=` transaction panel):
// 12 monthly bars, the split by product and by institution over a chosen
// window, and its latest transactions, each opening the transaction panel on
// top without closing this one. Needs a Suspense boundary (useSearchParams).

import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableRow } from "@/components/ui/table";
import { CategoryStackedBars } from "@/components/charts/category-stacked-bars";
import { useCategorySpend } from "@/components/charts/use-category-spend";
import { useSelectedTransaction } from "@/components/transactions/use-selected-transaction";
import { hrefWithSearchParam } from "@/lib/transaction-selection";
import {
  CATEGORY_PARAM,
  categoryExpensesHref,
  monthWindow,
  UNCATEGORIZED_COLOR,
  UNCATEGORIZED_NAME,
  UNCATEGORIZED_PARAM,
  type CategorySpendResponse,
} from "@/lib/category-spend";
import { monthOf, trailingMonthsWindow } from "@/lib/category-trends";
import { formatAmount, formatPercent, formatPlainDateEs } from "@/lib/utils";
import { CategoryIcon } from "./CategoryIcon";
import { CategorySwatch, DEFAULT_CATEGORY_COLOR, type ApiCategory } from "./shared";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Months of the monthly bars. */
const BAR_MONTHS = 12;
/** Windows the product/institution split and the list can cover, in months. */
const WINDOW_OPTIONS = [3, 6, 12] as const;
type WindowMonths = (typeof WINDOW_OPTIONS)[number];
/** Transactions listed before the "Ver todas en Gastos" link. */
const TXN_CAP = 20;

interface ListedTransaction {
  id: string;
  description: string;
  amount: number;
  transactionDate: string;
  currency: string;
  isInternalTransfer: boolean;
}

export function CategoryDetailSheet({
  categories,
  refreshKey,
}: {
  categories: ApiCategory[];
  /** Bumped when the page reloads (after a transaction edit too), so the
   *  panel's figures refetch. */
  refreshKey: number;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const param = searchParams.get(CATEGORY_PARAM);
  // The page's own TransactionDetailSheet handles `?txn=`; one here too would
  // open a second, stacked sheet for the same transaction.
  const { rowProps } = useSelectedTransaction();

  // While closing, keep the last category so the exit animation does not
  // flash an empty panel.
  const [shown, setShown] = useState(param);
  if (param !== null && param !== shown) setShown(param);

  const close = () =>
    router.replace(
      hrefWithSearchParam(pathname, searchParams.toString(), CATEGORY_PARAM, null),
      { scroll: false }
    );

  return (
    <Sheet
      open={param !== null}
      onOpenChange={(open) => {
        if (!open) close();
      }}
    >
      <SheetContent className="gap-0 sm:max-w-xl">
        {shown !== null && (
          <CategoryDetailBody
            key={shown}
            param={shown}
            categories={categories}
            refreshKey={refreshKey}
            rowProps={rowProps}
          />
        )}
      </SheetContent>
    </Sheet>
  );
}

function CategoryDetailBody({
  param,
  categories,
  refreshKey,
  rowProps,
}: {
  param: string;
  categories: ApiCategory[];
  refreshKey: number;
  rowProps: ReturnType<typeof useSelectedTransaction>["rowProps"];
}) {
  const uncategorized = param === UNCATEGORIZED_PARAM;
  const category = uncategorized
    ? null
    : categories.find((c) => c.id === param.toLowerCase());

  if (!uncategorized && !category) {
    return (
      <SheetHeader>
        <SheetTitle>Categoría no encontrada</SheetTitle>
        <SheetDescription>
          {UUID.test(param)
            ? "Puede que haya sido eliminada."
            : "El enlace no corresponde a ninguna categoría."}
        </SheetDescription>
      </SheetHeader>
    );
  }
  return (
    <CategoryDetail
      categoryId={category?.id ?? null}
      name={category?.name ?? UNCATEGORIZED_NAME}
      color={
        category ? (category.color ?? DEFAULT_CATEGORY_COLOR) : UNCATEGORIZED_COLOR
      }
      icon={category?.icon ?? null}
      refreshKey={refreshKey}
      rowProps={rowProps}
    />
  );
}

function CategoryDetail({
  categoryId,
  name,
  color,
  icon,
  refreshKey,
  rowProps,
}: {
  categoryId: string | null;
  name: string;
  color: string;
  icon: string | null;
  refreshKey: number;
  rowProps: ReturnType<typeof useSelectedTransaction>["rowProps"];
}) {
  const categoryParam = categoryId ?? UNCATEGORIZED_PARAM;
  // Fixed at mount, so a panel left open past midnight keeps one window.
  const [currentMonth] = useState(() => monthOf(new Date()));
  const [windowMonths, setWindowMonths] = useState<WindowMonths>(12);

  const bars = trailingMonthsWindow(currentMonth, BAR_MONTHS);
  const barsSpend = useCategorySpend(
    new URLSearchParams({
      ...bars,
      categoryId: categoryParam,
      groupBy: "month",
    }).toString(),
    refreshKey
  );
  const period = trailingMonthsWindow(currentMonth, windowMonths);
  const splitQuery = (groupBy: string) =>
    new URLSearchParams({ ...period, categoryId: categoryParam, groupBy }).toString();
  const byProduct = useCategorySpend(splitQuery("product"), refreshKey);
  const byInstitution = useCategorySpend(splitQuery("institution"), refreshKey);
  const txns = useCategoryTransactions(
    new URLSearchParams({
      ...period,
      categoryId: categoryParam,
      limit: String(TXN_CAP + 1),
    }).toString(),
    refreshKey
  );

  const thisMonth = barsSpend.data?.groups?.find((g) => g.key === currentMonth)?.total;
  const monthHrefFor = (_: string | null, month: string) =>
    categoryExpensesHref({ categoryId, ...monthWindow(month) });

  return (
    <>
      <SheetHeader className="border-b">
        <SheetTitle className="flex items-center gap-2">
          <CategorySwatch color={color} />
          <CategoryIcon icon={icon} className="size-4 shrink-0 text-muted-foreground" />
          {name}
        </SheetTitle>
        <SheetDescription>
          Este mes:{" "}
          <span className="font-medium text-foreground tabular-nums">
            {barsSpend.error
              ? "sin datos"
              : !barsSpend.data
                ? "..."
                : formatAmount(barsSpend.data.currency, thisMonth ?? 0)}
          </span>
          . Gasto en CLP sin transferencias internas.
        </SheetDescription>
      </SheetHeader>

      <div className="flex-1 space-y-6 overflow-y-auto p-4">
        <section className="space-y-2">
          <h3 className="text-sm font-semibold">Últimos {BAR_MONTHS} meses</h3>
          {barsSpend.error ? (
            <ErrorNote />
          ) : !barsSpend.data ? (
            <Loading />
          ) : (
            <CategoryStackedBars
              groups={barsSpend.data.groups ?? []}
              currency={barsSpend.data.currency}
              height={180}
              hrefFor={monthHrefFor}
              emptyMessage={`Sin gastos en los últimos ${BAR_MONTHS} meses.`}
            />
          )}
        </section>

        <div className="flex flex-wrap items-center justify-between gap-2">
          <h3 className="text-sm font-semibold">Detalle</h3>
          <div className="flex items-center gap-1" role="group" aria-label="Período">
            {WINDOW_OPTIONS.map((months) => (
              <Button
                key={months}
                variant={windowMonths === months ? "secondary" : "ghost"}
                size="xs"
                onClick={() => setWindowMonths(months)}
              >
                {months} meses
              </Button>
            ))}
          </div>
        </div>

        <SpendSplit title="Por producto" spend={byProduct} color={color} />
        <SpendSplit title="Por institución" spend={byInstitution} color={color} />

        <section className="space-y-2">
          <h3 className="text-sm font-semibold">Transacciones</h3>
          {txns.error ? (
            <p className="text-sm text-muted-foreground">
              No se pudieron cargar las transacciones.
            </p>
          ) : !txns.rows ? (
            <Loading />
          ) : txns.rows.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              Sin transacciones en este período.
            </p>
          ) : (
            <Table>
              <TableBody>
                {txns.rows.slice(0, TXN_CAP).map((txn) => (
                  <TableRow key={txn.id} {...rowProps(txn.id)}>
                    <TableCell className="text-sm text-muted-foreground">
                      {formatPlainDateEs(txn.transactionDate)}
                    </TableCell>
                    <TableCell className="max-w-56 truncate">
                      {txn.description}
                      {txn.isInternalTransfer && (
                        <Badge variant="secondary" className="ml-2 text-xs">
                          Transferencia interna
                        </Badge>
                      )}
                    </TableCell>
                    <TableCell
                      className={`text-right font-medium tabular-nums ${
                        txn.isInternalTransfer
                          ? "text-muted-foreground"
                          : txn.amount < 0
                            ? "text-red-600"
                            : "text-green-600"
                      }`}
                    >
                      {txn.amount < 0 ? "-" : "+"}
                      {formatAmount(txn.currency, Math.abs(txn.amount))}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
          {txns.rows && txns.rows.length > 0 && (
            <Button asChild variant="outline" size="sm">
              <Link href={categoryExpensesHref({ categoryId, ...period })}>
                {txns.rows.length > TXN_CAP ? "Ver todas en Gastos" : "Ver en Gastos"}
              </Link>
            </Button>
          )}
        </section>
      </div>
    </>
  );
}

/** Spend split by product or institution: share and amount per group. */
function SpendSplit({
  title,
  spend,
  color,
}: {
  title: string;
  spend: { data: CategorySpendResponse | null; error: boolean };
  color: string;
}) {
  const groups = spend.data?.groups ?? [];
  // Shares of the listed groups: a group whose refunds outweigh its spend is
  // left out, so the overall total could be smaller than their sum.
  const sum = groups.reduce((s, g) => s + g.total, 0);
  return (
    <section className="space-y-2">
      <h3 className="text-sm font-semibold">{title}</h3>
      {spend.error ? (
        <ErrorNote />
      ) : !spend.data ? (
        <Loading />
      ) : groups.length === 0 ? (
        <p className="text-sm text-muted-foreground">Sin gastos en este período.</p>
      ) : (
        <ol className="space-y-2 text-sm">
          {groups.map((g) => {
            const percent = sum > 0 ? (g.total / sum) * 100 : 0;
            return (
              <li key={g.key} className="space-y-1">
                <div className="flex items-center gap-2">
                  <span className="min-w-0 flex-1 truncate">{g.label}</span>
                  <span className="w-14 text-right tabular-nums text-muted-foreground">
                    {formatPercent(percent)}
                  </span>
                  <span className="w-28 text-right font-medium tabular-nums">
                    {formatAmount(spend.data!.currency, g.total)}
                  </span>
                </div>
                <div className="h-1.5 rounded-full bg-muted">
                  <div
                    className="h-full rounded-full"
                    style={{ width: `${percent}%`, backgroundColor: color }}
                  />
                </div>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}

function Loading() {
  return <p className="text-sm text-muted-foreground">Cargando...</p>;
}

function ErrorNote() {
  return (
    <p className="text-sm text-muted-foreground">No se pudieron cargar los gastos.</p>
  );
}

/** GET /api/transactions for `query`, remembering the query each result
 *  answers (like useCategorySpend); `rows` is null while loading. */
function useCategoryTransactions(
  query: string,
  refreshKey: number
): { rows: ListedTransaction[] | null; error: boolean } {
  const [result, setResult] = useState<{
    query: string;
    rows: ListedTransaction[] | null;
    error: boolean;
  } | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    fetch(`/api/transactions?${query}`, { signal: controller.signal })
      .then((res) => {
        if (!res.ok) throw new Error(`GET /api/transactions ${res.status}`);
        return res.json();
      })
      .then((rows: ListedTransaction[]) => {
        if (!controller.signal.aborted) setResult({ query, rows, error: false });
      })
      .catch(() => {
        if (!controller.signal.aborted) setResult({ query, rows: null, error: true });
      });
    return () => controller.abort();
  }, [query, refreshKey]);

  const current = result?.query === query ? result : null;
  return { rows: current?.rows ?? null, error: current?.error ?? false };
}
