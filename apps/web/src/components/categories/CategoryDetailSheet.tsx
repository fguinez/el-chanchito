"use client";

// Side panel with one category's spending, open while the URL carries
// `?category=<id|none>` (the same pattern as the `?txn=` transaction panel):
// a chosen period (a preset or a custom month range) with its totals, monthly
// bars, the split by product and by institution, and its latest transactions (searchable), each opening the
// transaction panel on top without closing this one. Needs a Suspense
// boundary (useSearchParams).
//
// A category with income in the bars' months gets a Gastos / Ingresos / Neto
// toggle (Neto by default) over the bars and the splits; one without it shows
// expenses alone. Neto is expenses minus income: net spending in
// the category's color above zero, net income in green below.
//
// Subcategories (V046): a parent's figures include its subcategories, and a
// "Por subcategoría" split lists each one (plus the rows assigned to the
// parent itself), each opening its own panel; a subcategory with nothing in
// the period is listed at $0, so a new one is reachable from here too. The
// parent's transaction list tags each subcategory row with its name. A subcategory's panel is the
// same one scoped to its rows, with a "Padre › Sub" breadcrumb back.

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
import { Input } from "@/components/ui/input";
import { SearchInput } from "@/components/ui/search-input";
import { Table, TableBody, TableCell, TableRow } from "@/components/ui/table";
import {
  CategoryStackedBars,
  formatSpendMonth,
} from "@/components/charts/category-stacked-bars";
import { useCategorySpend } from "@/components/charts/use-category-spend";
import { useSelectedTransaction } from "@/components/transactions/use-selected-transaction";
import { hrefWithSearchParam } from "@/lib/transaction-selection";
import {
  CATEGORY_PARAM,
  PERIOD_PARAM,
  categoryDetailHref,
  categoryExpensesHref,
  monthWindow,
  UNCATEGORIZED_COLOR,
  UNCATEGORIZED_NAME,
  UNCATEGORIZED_PARAM,
  type CategorySpendGroup,
  type CategorySpendResponse,
  type SpendFlows,
} from "@/lib/category-spend";
import { monthOf, shiftMonth, trailingMonthsWindow } from "@/lib/category-trends";
import { useDebouncedValue } from "@/lib/use-debounced-value";
import {
  cn,
  formatAmount,
  formatPercent,
  formatPlainDateEs,
} from "@/lib/utils";
import { CategoryIcon } from "./CategoryIcon";
import { ChevronRight } from "lucide-react";
import { CategorySwatch, DEFAULT_CATEGORY_COLOR, type ApiCategory } from "./shared";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Fewest months the bars show: a shorter period is drawn with the months
 *  before it, for context. */
const MIN_BAR_MONTHS = 6;

/** An inclusive YYYY-MM range. */
interface MonthRange {
  from: string;
  to: string;
}
type PresetId = "this-month" | "last-month" | "3m" | "6m" | "12m" | "ytd";
const PRESETS: { id: PresetId; label: string; range: (current: string) => MonthRange }[] = [
  { id: "this-month", label: "Este mes", range: (c) => ({ from: c, to: c }) },
  {
    id: "last-month",
    label: "Mes pasado",
    range: (c) => ({ from: shiftMonth(c, -1), to: shiftMonth(c, -1) }),
  },
  { id: "3m", label: "3 meses", range: (c) => ({ from: shiftMonth(c, -2), to: c }) },
  { id: "6m", label: "6 meses", range: (c) => ({ from: shiftMonth(c, -5), to: c }) },
  { id: "12m", label: "12 meses", range: (c) => ({ from: shiftMonth(c, -11), to: c }) },
  { id: "ytd", label: "Este año", range: (c) => ({ from: `${c.slice(0, 4)}-01`, to: c }) },
];

/** The panel's period: a preset id, or a custom range as `YYYY-MM..YYYY-MM`. */
type PanelPeriod = PresetId | MonthRange;
const PERIOD_STORAGE_KEY = "category-panel-period";
const MONTH = /^\d{4}-(0[1-9]|1[0-2])$/;

function parsePeriod(value: string | null): PanelPeriod | null {
  if (!value) return null;
  if (PRESETS.some((p) => p.id === value)) return value as PresetId;
  const [from, to] = value.split("..");
  return MONTH.test(from ?? "") && MONTH.test(to ?? "") && from <= to
    ? { from, to }
    : null;
}

function serializePeriod(period: PanelPeriod): string {
  return typeof period === "string" ? period : `${period.from}..${period.to}`;
}

function readStoredPeriod(): PanelPeriod | null {
  try {
    return parsePeriod(window.localStorage.getItem(PERIOD_STORAGE_KEY));
  } catch {
    return null;
  }
}

/** The period from `?period=`, else the last one picked in this browser, else
 *  12 months. Picking one writes both, so it carries across categories,
 *  reloads and reopening the panel. */
function usePanelPeriod() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const fromUrl = parsePeriod(searchParams.get(PERIOD_PARAM));
  const period: PanelPeriod = fromUrl ?? "12m";
  const setPeriod = (next: PanelPeriod) => {
    const value = serializePeriod(next);
    try {
      window.localStorage.setItem(PERIOD_STORAGE_KEY, value);
    } catch {
      // Storage blocked: the URL still carries it.
    }
    router.replace(
      hrefWithSearchParam(pathname, searchParams.toString(), PERIOD_PARAM, value),
      { scroll: false }
    );
  };
  // Read storage after mount (the server has none), once per panel.
  useEffect(() => {
    if (fromUrl) return;
    const stored = readStoredPeriod();
    if (stored) setPeriod(stored);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return { period, setPeriod };
}

/** Months from `from` to `to`, inclusive. */
function monthSpan({ from, to }: MonthRange): number {
  const index = (m: string) => Number(m.slice(0, 4)) * 12 + Number(m.slice(5, 7));
  return index(to) - index(from) + 1;
}

/** "ene 26 – mar 26", or one month alone. */
function rangeLabel(range: MonthRange): string {
  return range.from === range.to
    ? formatSpendMonth(range.from)
    : `${formatSpendMonth(range.from)} – ${formatSpendMonth(range.to)}`;
}
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
  categoryId: string | null;
}

/** A subcategory as a parent's panel lists it. */
interface PanelSubcategory {
  id: string;
  name: string;
  /** Its own color, else the parent's. */
  color: string;
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
      hrefWithSearchParam(
        pathname,
        hrefWithSearchParam("", searchParams.toString(), PERIOD_PARAM, null).slice(1),
        CATEGORY_PARAM,
        null
      ),
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
  const parent = category?.parentId
    ? categories.find((c) => c.id === category.parentId)
    : undefined;
  const color = category
    ? (category.color ?? parent?.color ?? DEFAULT_CATEGORY_COLOR)
    : UNCATEGORIZED_COLOR;
  const subcategories: PanelSubcategory[] = category
    ? categories
        .filter((c) => c.parentId === category.id)
        .sort((a, b) => a.name.localeCompare(b.name, "es"))
        .map((c) => ({ id: c.id, name: c.name, color: c.color ?? color }))
    : [];
  return (
    <CategoryDetail
      categoryId={category?.id ?? null}
      name={category?.name ?? UNCATEGORIZED_NAME}
      color={color}
      icon={category?.icon ?? null}
      parent={parent ? { id: parent.id, name: parent.name } : null}
      subcategories={subcategories}
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
  parent,
  subcategories,
  refreshKey,
  rowProps,
}: {
  categoryId: string | null;
  name: string;
  color: string;
  icon: string | null;
  /** The parent of a subcategory, for the breadcrumb. */
  parent: { id: string; name: string } | null;
  /** A parent's subcategories, by name: the "Por subcategoría" split and
   *  the tags on the transaction list. Empty for any other category. */
  subcategories: PanelSubcategory[];
  refreshKey: number;
  rowProps: ReturnType<typeof useSelectedTransaction>["rowProps"];
}) {
  const categoryParam = categoryId ?? UNCATEGORIZED_PARAM;
  const hasChildren = subcategories.length > 0;
  const subcategoryNames = new Map(subcategories.map((s) => [s.id, s.name]));
  // Fixed at mount, so a panel left open past midnight keeps one window.
  const [currentMonth] = useState(() => monthOf(new Date()));
  const { period: chosenPeriod, setPeriod } = usePanelPeriod();
  const preset: PresetId | "custom" =
    typeof chosenPeriod === "string" ? chosenPeriod : "custom";
  const customRange: MonthRange =
    typeof chosenPeriod === "string"
      ? PRESETS.find((p) => p.id === chosenPeriod)!.range(currentMonth)
      : chosenPeriod;
  const setPreset = (id: PresetId) => setPeriod(id);
  const setCustomRange = (update: (r: MonthRange) => MonthRange) =>
    setPeriod(update(customRange));
  const range =
    preset === "custom"
      ? customRange
      : PRESETS.find((p) => p.id === preset)!.range(currentMonth);
  const barMonths = Math.max(monthSpan(range), MIN_BAR_MONTHS);
  // Null until the user picks one, so the default follows the data.
  const [chosenMeasure, setChosenMeasure] = useState<Measure | null>(null);

  const bars = trailingMonthsWindow(range.to, barMonths);
  const barsSpend = useCategorySpend(
    new URLSearchParams({
      ...bars,
      categoryId: categoryParam,
      groupBy: "month",
    }).toString(),
    refreshKey
  );
  const period = {
    from: monthWindow(range.from).from,
    to: monthWindow(range.to).to,
  };
  // Totals over the period itself (the bars may reach further back).
  const totals = useCategorySpend(
    new URLSearchParams({ ...period, categoryId: categoryParam }).toString(),
    refreshKey
  );
  const splitQuery = (groupBy: string) =>
    new URLSearchParams({ ...period, categoryId: categoryParam, groupBy }).toString();
  const byProduct = useCategorySpend(splitQuery("product"), refreshKey);
  const byInstitution = useCategorySpend(splitQuery("institution"), refreshKey);
  // Asked for (and shown) only for a parent with subcategories.
  const bySubcategory = useCategorySpend(
    hasChildren ? `${splitQuery("category")}&split=1` : "",
    refreshKey
  );
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

  // Income anywhere in the bars turns on the toggle; without it the
  // panel shows expenses alone. If the bars fail, the splits' income decides,
  // so the toggle still works for them.
  const hasIncome = barsSpend.data
    ? barsSpend.data.income > 0
    : barsSpend.error &&
      ((byProduct.data?.income ?? 0) > 0 || (byInstitution.data?.income ?? 0) > 0);
  // The splits wait for the bars, so they never render in one measure and
  // flip to another once the default is known.
  const measureKnown = barsSpend.data != null || barsSpend.error;
  const unconverted =
    (barsSpend.data?.unconvertedCount ?? 0) +
    (barsSpend.data?.unconvertedIncomeCount ?? 0);
  const measure: Measure = hasIncome ? (chosenMeasure ?? "net") : "expenses";
  const currency = barsSpend.data?.currency ?? "CLP";
  const periodNet = totals.data?.net ?? 0;
  const monthHrefFor = (_: string | null, month: string) =>
    categoryExpensesHref({ categoryId, ...monthWindow(month) });

  return (
    <>
      <SheetHeader className="border-b">
        <SheetTitle className="flex items-center gap-2">
          <CategorySwatch color={color} />
          <CategoryIcon icon={icon} className="size-4 shrink-0 text-muted-foreground" />
          {parent ? (
            <span className="flex min-w-0 items-center gap-1">
              <Link
                href={categoryDetailHref(parent.id, serializePeriod(chosenPeriod))}
                replace
                scroll={false}
                className="truncate font-normal text-muted-foreground hover:text-foreground hover:underline"
              >
                {parent.name}
              </Link>
              <ChevronRight
                aria-hidden
                className="size-4 shrink-0 text-muted-foreground"
              />
              <span className="sr-only">›</span>
              <span className="truncate">{name}</span>
            </span>
          ) : (
            name
          )}
        </SheetTitle>
        <SheetDescription>
          En CLP, sin transferencias internas
          {hasChildren && ", con sus subcategorías"}.
        </SheetDescription>
      </SheetHeader>

      <div className="flex-1 space-y-6 overflow-y-auto p-4">
        <section className="space-y-3">
          <div className="flex flex-wrap items-center gap-1" role="group" aria-label="Período">
            {PRESETS.map((p) => (
              <Button
                key={p.id}
                variant={preset === p.id ? "secondary" : "ghost"}
                size="xs"
                aria-pressed={preset === p.id}
                onClick={() => setPreset(p.id)}
              >
                {p.label}
              </Button>
            ))}
            <Button
              variant={preset === "custom" ? "secondary" : "ghost"}
              size="xs"
              aria-pressed={preset === "custom"}
              onClick={() => {
                if (preset !== "custom") setPeriod(range);
              }}
            >
              Personalizado
            </Button>
          </div>
          {preset === "custom" && (
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <label className="flex items-center gap-1">
                Desde
                <Input
                  type="month"
                  className="h-8 w-40"
                  value={customRange.from}
                  max={customRange.to}
                  onChange={(e) =>
                    e.target.value &&
                    setCustomRange((r) => ({
                      from: e.target.value,
                      to: e.target.value > r.to ? e.target.value : r.to,
                    }))
                  }
                />
              </label>
              <label className="flex items-center gap-1">
                Hasta
                <Input
                  type="month"
                  className="h-8 w-40"
                  value={customRange.to}
                  min={customRange.from}
                  max={currentMonth}
                  onChange={(e) =>
                    e.target.value &&
                    setCustomRange((r) => ({
                      from: e.target.value < r.from ? e.target.value : r.from,
                      to: e.target.value,
                    }))
                  }
                />
              </label>
            </div>
          )}
          <PeriodTotals
            label={rangeLabel(range)}
            currency={totals.data?.currency ?? "CLP"}
            error={Boolean(totals.error)}
            data={totals.data ?? null}
            hasIncome={Boolean(hasIncome)}
            net={periodNet}
          />
        </section>

        <section className="space-y-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 className="text-sm font-semibold">Por mes</h3>
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
              netBreakdown={measure === "net"}
              emptyMessage={`Sin ${MEASURE_NOUNS[measure]} en estos meses.`}
            />
          )}
          {unconverted > 0 && (
            <p className="text-xs text-muted-foreground">
              {unconverted === 1
                ? "1 movimiento en una moneda sin tipo de cambio no se incluye."
                : `${unconverted} movimientos en monedas sin tipo de cambio no se incluyen.`}
            </p>
          )}
        </section>

        <h3 className="text-sm font-semibold">Detalle · {rangeLabel(range)}</h3>

        {hasChildren && (
          <SpendSplit
            title="Por subcategoría"
            spend={bySubcategory}
            color={color}
            measure={measure}
            ready={measureKnown}
            hrefFor={(key) => (key !== categoryId ? categoryDetailHref(key, serializePeriod(chosenPeriod)) : null)}
            emptyGroups={subcategories.map((s) => ({
              key: s.id,
              label: s.name,
              color: s.color,
            }))}
          />
        )}
        <SpendSplit
          title="Por producto"
          spend={byProduct}
          color={color}
          measure={measure}
          ready={measureKnown}
        />
        <SpendSplit
          title="Por institución"
          spend={byInstitution}
          color={color}
          measure={measure}
          ready={measureKnown}
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
                      {txn.categoryId && subcategoryNames.has(txn.categoryId) && (
                        <span className="ml-2 text-xs text-muted-foreground">
                          {subcategoryNames.get(txn.categoryId)}
                        </span>
                      )}
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

/** The category's flows split by product, institution or subcategory in the
 *  measure: share and amount per group for Gastos and Ingresos; for Neto,
 *  signed amounts (net income as a green "+X") with bars sized by magnitude.
 *  Groups that carry a color (subcategories) draw their spending in it,
 *  `hrefFor` turns a group's label into a link, and `emptyGroups` missing
 *  from the figures are listed last at zero. */
function SpendSplit({
  title,
  spend,
  color,
  measure,
  ready,
  hrefFor,
  emptyGroups,
}: {
  title: string;
  spend: { data: CategorySpendResponse | null; error: boolean };
  color: string;
  measure: Measure;
  /** False while the measure still depends on data in flight. */
  ready: boolean;
  hrefFor?: (key: string) => string | null;
  emptyGroups?: { key: string; label: string; color: string }[];
}) {
  const listed: { key: string; label: string; color?: string; value: number }[] =
    (spend.data?.groups ?? [])
      .map((g) => ({ ...g, value: measureValue(g, measure) }))
      .filter((g) => g.value !== 0)
      // Ties by key, like the server, so expense-only panels keep its order.
      .sort((a, b) => b.value - a.value || a.key.localeCompare(b.key));
  const listedKeys = new Set(listed.map((g) => g.key));
  // Padding an all-zero split would hide the empty state.
  const groups = [
    ...listed,
    ...(listed.length === 0 ? [] : (emptyGroups ?? []))
      .filter((g) => !listedKeys.has(g.key))
      .map((g) => ({ ...g, value: 0 })),
  ];
  // Shares of the listed groups' magnitudes.
  const sum = groups.reduce((s, g) => s + Math.abs(g.value), 0);
  return (
    <section className="space-y-2">
      <h3 className="text-sm font-semibold">{title}</h3>
      {spend.error ? (
        <ErrorNote />
      ) : !spend.data || !ready ? (
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
            const href = hrefFor?.(g.key) ?? null;
            return (
              <li key={g.key} className="space-y-1">
                <div className="flex items-center gap-2">
                  {href ? (
                    <Link
                      href={href}
                      replace
                      scroll={false}
                      className="min-w-0 flex-1 truncate hover:underline"
                    >
                      {g.label}
                    </Link>
                  ) : (
                    <span className="min-w-0 flex-1 truncate">{g.label}</span>
                  )}
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
                      backgroundColor: incoming ? INCOME_COLOR : (g.color ?? color),
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

/** The period's gastos, ingresos and the net between them. */
function PeriodTotals({
  label,
  currency,
  error,
  data,
  hasIncome,
  net,
}: {
  label: string;
  currency: string;
  error: boolean;
  data: SpendFlows | null;
  hasIncome: boolean;
  net: number;
}) {
  const tile = (title: string, value: string, className?: string) => (
    <div className="rounded-md border p-2">
      <p className="text-xs text-muted-foreground">{title}</p>
      <p className={cn("font-semibold tabular-nums", className)}>{value}</p>
    </div>
  );
  return (
    <div className="space-y-1">
      <p className="text-xs text-muted-foreground">{label}</p>
      {error ? (
        <ErrorNote />
      ) : !data ? (
        <Loading />
      ) : (
        <div className={cn("grid gap-2", hasIncome ? "grid-cols-3" : "grid-cols-1")}>
          {tile("Gastos", formatAmount(currency, data.expenses))}
          {hasIncome &&
            tile("Ingresos", formatAmount(currency, data.income), "text-green-600")}
          {hasIncome &&
            tile(
              net > 0 ? "Gasto neto" : net < 0 ? "Ingreso neto" : "Neto",
              formatNet(currency, net),
              net < 0 ? "text-green-600" : undefined
            )}
        </div>
      )}
    </div>
  );
}
