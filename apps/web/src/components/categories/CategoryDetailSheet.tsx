"use client";

// Side panel with one category's spending, open while the URL carries
// `?category=<id|none>` (the same pattern as the `?txn=` transaction panel):
// 12 monthly bars, the split by product and by institution over a chosen
// window, and its latest transactions (searchable), each opening the
// transaction panel on top without closing this one. Needs a Suspense
// boundary (useSearchParams).
//
// A category with income in the 12 months gets a Gastos / Ingresos / Neto
// toggle (Neto by default) over the bars and the splits; one without it shows
// expenses alone. Neto is expenses minus income: net spending in
// the category's color above zero, net income in green below.

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
import { SearchInput } from "@/components/ui/search-input";
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
  type CategorySpendGroup,
  type CategorySpendResponse,
  type SpendFlows,
} from "@/lib/category-spend";
import { monthOf, trailingMonthsWindow } from "@/lib/category-trends";
import { useDebouncedValue } from "@/lib/use-debounced-value";
import {
  cn,
  formatAmount,
  formatPercent,
  formatPlainDateEs,
} from "@/lib/utils";
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

type Measure = "expenses" | "income" | "net";
const MEASURES: { value: Measure; label: string }[] = [
  { value: "expenses", label: "Gastos" },
  { value: "income", label: "Ingresos" },
  { value: "net", label: "Neto" },
];
const INCOME_COLOR = "#16a34a";
/** Bar series key of income (never a category id, so it never collides). */
const INCOME_SERIES = "__income";
/** What an empty chart or split has none of, per measure. */
const MEASURE_NOUNS: Record<Measure, string> = {
  expenses: "gastos",
  income: "ingresos",
  net: "movimientos",
};

/** A flow in the measure: gross expenses, gross income or signed net. */
function measureValue(flows: SpendFlows, measure: Measure): number {
  return measure === "expenses"
    ? flows.expenses
    : measure === "income"
      ? flows.income
      : flows.net;
}

/** The month groups as one series in the measure, for CategoryStackedBars.
 *  Spending (gross or net) keeps the category's key and color, income (gross
 *  or net) gets INCOME_SERIES in green, so switching measures tweens the bars
 *  instead of remounting them. Net income is negative, drawn below zero. */
function measureGroups(
  groups: readonly CategorySpendGroup[],
  measure: Measure,
  categoryId: string | null,
  name: string,
  color: string
): CategorySpendGroup[] {
  return groups.map((group) => {
    const value = measureValue(group, measure);
    const spending = measure === "expenses" || (measure === "net" && value > 0);
    const flows = { expenses: group.expenses, income: group.income, net: group.net };
    const series = {
      categoryId: spending ? categoryId : INCOME_SERIES,
      name:
        measure === "expenses"
          ? "Gastos"
          : measure === "income"
            ? "Ingresos"
            : spending
              ? "Gasto neto"
              : "Ingreso neto",
      color: spending ? color : INCOME_COLOR,
      amount: value,
      percent: 100,
      ...flows,
    };
    return {
      key: group.key,
      total: Math.abs(value),
      ...flows,
      categories: value !== 0 ? [series] : [],
    };
  });
}

/** A net figure signed for display: net income as "+X". */
function formatNet(currency: string, net: number): string {
  return net < 0 ? `+${formatAmount(currency, -net)}` : formatAmount(currency, net);
}

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
  // Null until the user picks one, so the default follows the data.
  const [chosenMeasure, setChosenMeasure] = useState<Measure | null>(null);

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
  // The list's search: local to this category (the panel remounts per
  // category), queried once typing pauses.
  const [search, setSearch] = useState("");
  const q = useDebouncedValue(search.trim());
  const txnsQuery = new URLSearchParams({
    ...period,
    categoryId: categoryParam,
    limit: String(TXN_CAP + 1),
  });
  if (q) txnsQuery.set("q", q);
  const txns = useCategoryTransactions(txnsQuery.toString(), refreshKey);

  // Income anywhere in the 12 months turns on the toggle; without it the
  // panel shows expenses alone.
  const hasIncome = (barsSpend.data?.income ?? 0) > 0;
  const measure: Measure = hasIncome ? (chosenMeasure ?? "net") : "expenses";
  const currency = barsSpend.data?.currency ?? "CLP";
  const thisMonth = barsSpend.data?.groups?.find((g) => g.key === currentMonth);
  const monthNet = thisMonth?.net ?? 0;
  const monthHrefFor = (_: string | null, month: string) =>
    categoryExpensesHref({ categoryId, ...monthWindow(month) });
  const figure = (value: string, className?: string) => (
    <span className={cn("font-medium tabular-nums text-foreground", className)}>
      {value}
    </span>
  );

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
          {barsSpend.error ? (
            figure("sin datos")
          ) : !barsSpend.data ? (
            figure("...")
          ) : (
            <>
              gastos {figure(formatAmount(currency, thisMonth?.expenses ?? 0))}
              {hasIncome && (
                <>
                  , ingresos{" "}
                  {figure(formatAmount(currency, thisMonth?.income ?? 0), "text-green-600")}
                  , neto{" "}
                  {figure(
                    formatNet(currency, monthNet),
                    monthNet < 0 ? "text-green-600" : undefined
                  )}
                </>
              )}
            </>
          )}
          . En CLP, sin transferencias internas.
        </SheetDescription>
      </SheetHeader>

      <div className="flex-1 space-y-6 overflow-y-auto p-4">
        <section className="space-y-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 className="text-sm font-semibold">Últimos {BAR_MONTHS} meses</h3>
            {hasIncome && (
              <div className="flex items-center gap-1" role="group" aria-label="Medida">
                {MEASURES.map((m) => (
                  <Button
                    key={m.value}
                    variant={measure === m.value ? "secondary" : "ghost"}
                    size="xs"
                    aria-pressed={measure === m.value}
                    onClick={() => setChosenMeasure(m.value)}
                  >
                    {m.label}
                  </Button>
                ))}
              </div>
            )}
          </div>
          {barsSpend.error ? (
            <ErrorNote />
          ) : !barsSpend.data ? (
            <Loading />
          ) : (
            <CategoryStackedBars
              groups={measureGroups(
                barsSpend.data.groups ?? [],
                measure,
                categoryId,
                name,
                color
              )}
              currency={currency}
              height={180}
              hrefFor={monthHrefFor}
              emptyMessage={`Sin ${MEASURE_NOUNS[measure]} en los últimos ${BAR_MONTHS} meses.`}
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

        <SpendSplit
          title="Por producto"
          spend={byProduct}
          color={color}
          measure={measure}
        />
        <SpendSplit
          title="Por institución"
          spend={byInstitution}
          color={color}
          measure={measure}
        />

        <section className="space-y-2">
          <h3 className="text-sm font-semibold">Transacciones</h3>
          <SearchInput
            value={search}
            onValueChange={setSearch}
            loading={search.trim() !== q || txns.stale}
          />
          {txns.error ? (
            <p className="text-sm text-muted-foreground">
              No se pudieron cargar las transacciones.
            </p>
          ) : !txns.rows ? (
            <Loading />
          ) : txns.rows.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              {txns.q
                ? `Sin resultados para «${txns.q}» en este período.`
                : "Sin transacciones en este período."}
            </p>
          ) : (
            <Table
              aria-busy={txns.stale}
              className={cn(
                "transition-opacity duration-200",
                txns.stale && "opacity-60"
              )}
            >
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
              <Link href={categoryExpensesHref({ categoryId, ...period, q })}>
                {txns.rows.length > TXN_CAP ? "Ver todas en Gastos" : "Ver en Gastos"}
              </Link>
            </Button>
          )}
        </section>
      </div>
    </>
  );
}

/** The category's flows split by product or institution in the measure:
 *  share and amount per group for Gastos and Ingresos; for Neto, signed
 *  amounts (net income as a green "+X") with bars sized by magnitude. */
function SpendSplit({
  title,
  spend,
  color,
  measure,
}: {
  title: string;
  spend: { data: CategorySpendResponse | null; error: boolean };
  color: string;
  measure: Measure;
}) {
  const groups = (spend.data?.groups ?? [])
    .map((g) => ({ ...g, value: measureValue(g, measure) }))
    .filter((g) => g.value !== 0)
    .sort((a, b) => b.value - a.value || a.label.localeCompare(b.label, "es"));
  // Shares of the listed groups' magnitudes.
  const sum = groups.reduce((s, g) => s + Math.abs(g.value), 0);
  return (
    <section className="space-y-2">
      <h3 className="text-sm font-semibold">{title}</h3>
      {spend.error ? (
        <ErrorNote />
      ) : !spend.data ? (
        <Loading />
      ) : groups.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          Sin {MEASURE_NOUNS[measure]} en este período.
        </p>
      ) : (
        <ol className="space-y-2 text-sm">
          {groups.map((g) => {
            const percent = sum > 0 ? (Math.abs(g.value) / sum) * 100 : 0;
            const incoming = measure === "income" || g.value < 0;
            const currency = spend.data!.currency;
            return (
              <li key={g.key} className="space-y-1">
                <div className="flex items-center gap-2">
                  <span className="min-w-0 flex-1 truncate">{g.label}</span>
                  {measure !== "net" && (
                    <span className="w-14 text-right tabular-nums text-muted-foreground">
                      {formatPercent(percent)}
                    </span>
                  )}
                  <span
                    className={cn(
                      "w-28 text-right font-medium tabular-nums",
                      measure === "net" && incoming && "text-green-600"
                    )}
                  >
                    {measure === "net"
                      ? formatNet(currency, g.value)
                      : formatAmount(currency, g.value)}
                  </span>
                </div>
                <div className="h-1.5 rounded-full bg-muted">
                  <div
                    className="h-full rounded-full"
                    style={{
                      width: `${percent}%`,
                      backgroundColor: incoming ? INCOME_COLOR : color,
                    }}
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
 *  answers (like useCategorySpend); `rows` is null while the first one loads.
 *  A new query (a search, another window) keeps the last rows, `stale`, until
 *  its own arrive; `q` is the search the shown rows answer. */
function useCategoryTransactions(
  query: string,
  refreshKey: number
): {
  rows: ListedTransaction[] | null;
  error: boolean;
  stale: boolean;
  q: string | null;
} {
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

  if (result?.query === query) {
    const q = new URLSearchParams(query).get("q");
    return { rows: result.rows, error: result.error, stale: false, q };
  }
  if (result?.rows) {
    const q = new URLSearchParams(result.query).get("q");
    return { rows: result.rows, error: false, stale: true, q };
  }
  return { rows: null, error: false, stale: false, q: null };
}
