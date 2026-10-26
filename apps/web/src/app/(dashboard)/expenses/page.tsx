"use client";

import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { X } from "lucide-react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { useSortableData } from "@/lib/use-sortable-data";
import {
  cn,
  formatAmount,
  formatCLP,
  formatPlainDateEs,
  parseNumberInput,
} from "@/lib/utils";
import { clpTotals } from "@/lib/transactions";
import { CsvImport } from "@/components/dashboard/CsvImport";
import { TransactionDetailSheet } from "@/components/transactions/transaction-detail-sheet";
import { useSelectedTransaction } from "@/components/transactions/use-selected-transaction";
import {
  onlyCurrency,
  ProductSelect,
  useActiveProducts,
  useCategories,
} from "@/components/transactions/pickers";
import { SELECT_CLASS } from "@/components/ui/native-select";
import { LOCATION_TEXT_MAX_LENGTH } from "@/lib/transaction-location";
import { CategoryDonut, categoryKey } from "@/components/charts/category-donut";
import { useCategorySpend } from "@/components/charts/use-category-spend";
import {
  UNCATEGORIZED_NAME,
  UNCATEGORIZED_PARAM,
} from "@/lib/category-spend";
import {
  parseExpensesFilters,
  type ExpensesFilters,
} from "@/lib/expenses-filters";

/** The currency the CLP-only manual form and totals work in. */
const CLP = "CLP";

interface Transaction {
  id: string;
  description: string;
  amount: number;
  transactionDate: string;
  scheduledMonth: string | null;
  source: string;
  /** Currency of the transaction's product; `amount` is in it. */
  currency: string;
  /** Linked to an internal transfer: listed, but left out of the totals. */
  isInternalTransfer: boolean;
  notes: string | null;
  /** The category, resolved to a name client-side from GET /api/categories. */
  categoryId: string | null;
}

type TransactionSortKey =
  | "fecha"
  | "descripcion"
  | "categoria"
  | "fuente"
  | "monto";

/** The current month as "YYYY-MM", for the <input type="month"> default. */
function currentMonthInput(): string {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
}

/** How many rows a "Cargar más" click adds (and the first page size). */
const PAGE_SIZE = 50;

// useSearchParams needs a Suspense boundary for the static prerender.
export default function ExpensesPage() {
  return (
    <Suspense fallback={<ExpensesSkeleton />}>
      <ExpensesView />
    </Suspense>
  );
}

/** Static shell for the prerender, before search params are available. */
function ExpensesSkeleton() {
  return (
    <div className="space-y-6" aria-busy="true">
      <div className="grid gap-4 md:grid-cols-3">
        {[0, 1, 2].map((i) => (
          <Card key={i}>
            <CardHeader className="pb-2">
              <div className="h-4 w-24 animate-pulse rounded bg-muted" />
              <div className="h-6 w-32 animate-pulse rounded bg-muted" />
            </CardHeader>
          </Card>
        ))}
      </div>
      <Card>
        <CardContent className="space-y-3">
          {[0, 1, 2, 3].map((i) => (
            <div key={i} className="h-5 animate-pulse rounded bg-muted" />
          ))}
        </CardContent>
      </Card>
    </div>
  );
}

function ExpensesView() {
  const { selectedTxnId, selectTransaction, rowProps } =
    useSelectedTransaction();
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const search = searchParams.toString();
  // A from/to day window (transaction date) replaces the month picker's
  // budget month while either side is set; picking a month clears it.
  const filters = useMemo(
    () => parseExpensesFilters(new URLSearchParams(search)),
    [search]
  );
  const hasWindow = filters.from != null || filters.to != null;

  const [transactions, setTransactions] = useState<Transaction[]>([]);
  const [month, setMonth] = useState(currentMonthInput());
  // Bumped after an edit so the list and the donut refetch.
  const [refreshKey, setRefreshKey] = useState(0);
  const refresh = useCallback(() => setRefreshKey((n) => n + 1), []);
  const [hasMore, setHasMore] = useState(false);
  // The filter set the shown rows answer (null before the first response or
  // failure): rows from another filter set are stale and read as loading.
  const [loadedFilterQuery, setLoadedFilterQuery] = useState<string | null>(
    null
  );
  const [description, setDescription] = useState("");
  const [place, setPlace] = useState("");
  const [amount, setAmount] = useState("");
  const [isExpense, setIsExpense] = useState(true);
  const [productId, setProductId] = useState("");
  const [transactionDate, setTransactionDate] = useState(
    new Date().toISOString().split("T")[0]
  );
  const [saving, setSaving] = useState(false);

  const categories = useCategories();
  const categoryName = useCallback(
    (id: string | null) => categories.find((c) => c.id === id)?.name ?? null,
    [categories]
  );
  // Every active product, to name the product and institution filters; the
  // manual form picks among the CLP ones, like the transfers picker.
  const { institutions: allInstitutions } = useActiveProducts();
  const institutions = useMemo(
    () => onlyCurrency(allInstitutions, CLP),
    [allInstitutions]
  );

  // The rows' bounds and scope, shared by the list and the donut so both
  // always cover the same transactions. Any day of the picked month works:
  // the API reads the month off the string.
  const scopeQuery = useMemo(() => {
    const params = new URLSearchParams();
    if (hasWindow) {
      if (filters.from) params.set("from", filters.from);
      if (filters.to) params.set("to", filters.to);
    } else {
      params.set("month", `${month}-01`);
    }
    if (filters.product) params.set("productId", filters.product);
    if (filters.institution) params.set("institution", filters.institution);
    return params;
  }, [hasWindow, filters, month]);

  // The list also narrows to the selected category; the donut does not, so
  // it keeps every slice and highlights the selected one.
  const filterQuery = useMemo(() => {
    const params = new URLSearchParams(scopeQuery);
    if (filters.category) params.set("categoryId", filters.category);
    return params.toString();
  }, [scopeQuery, filters.category]);
  // "Cargar más" pages within one filter set; new filters start over on the
  // first page in the same render, so the list fetches once. Resetting the
  // stored paging (not only reading past it) keeps a filter set seen before
  // from coming back with its old, expanded limit.
  const [paging, setPaging] = useState({ filterQuery, limit: PAGE_SIZE });
  if (paging.filterQuery !== filterQuery) {
    setPaging({ filterQuery, limit: PAGE_SIZE });
  }
  const limit = paging.filterQuery === filterQuery ? paging.limit : PAGE_SIZE;
  const listQuery = `${filterQuery}${filterQuery ? "&" : ""}limit=${limit}`;
  const spend = useCategorySpend(scopeQuery.toString(), refreshKey, {
    keepPrevious: true,
  });
  // "Cargar más" and a refresh keep the current rows on screen meanwhile.
  const listLoaded = loadedFilterQuery === filterQuery;

  // The AbortSignal drops a slower older response (e.g. after a rapid filter
  // switch) so it cannot overwrite a newer one.
  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    fetch(`/api/transactions?${listQuery}`, { signal })
      .then((res) => {
        // A 4xx/5xx body is { error, ... }, not an array: throw so it never
        // reaches the list state, which would otherwise break the page.
        if (!res.ok) throw new Error(`GET /api/transactions ${res.status}`);
        return res.json();
      })
      .then((rows: Transaction[]) => {
        if (signal.aborted) return;
        setTransactions(rows);
        setHasMore(rows.length >= limit);
        setLoadedFilterQuery(filterQuery);
      })
      .catch((error) => {
        if (signal.aborted) return;
        console.error(error);
        setLoadedFilterQuery(filterQuery);
      });
    return () => controller.abort();
  }, [listQuery, filterQuery, limit, refreshKey]);

  /** Sets (or, with null, removes) URL filters, keeping every other param
   *  (`?txn=` included). */
  const setFilters = useCallback(
    (updates: Partial<Record<keyof ExpensesFilters, string | null>>) => {
      const params = new URLSearchParams(search);
      for (const [key, value] of Object.entries(updates)) {
        if (value) params.set(key, value);
        else params.delete(key);
      }
      const query = params.toString();
      router.replace(query ? `${pathname}?${query}` : pathname, {
        scroll: false,
      });
    },
    [router, pathname, search]
  );

  /** null = "Sin categoría"; undefined = no category filter. */
  const selectedCategoryId =
    filters.category === undefined
      ? undefined
      : filters.category === UNCATEGORIZED_PARAM
        ? null
        : filters.category;
  // A slice click filters to its category, or clears the filter when it is
  // the one already selected.
  const toggleCategory = (categoryId: string | null) =>
    setFilters({
      category:
        selectedCategoryId === categoryId ? null : categoryKey(categoryId),
    });

  const productName = (id: string) =>
    allInstitutions
      .flatMap((inst) => inst.products.map((p) => ({ p, inst })))
      .find(({ p }) => p.id === id);
  const filterChips: { key: string; label: string; clear: () => void }[] = [];
  if (filters.category) {
    filterChips.push({
      key: "category",
      label: `Categoría: ${
        selectedCategoryId === null
          ? UNCATEGORIZED_NAME
          : (categoryName(filters.category) ?? "Categoría")
      }`,
      clear: () => setFilters({ category: null }),
    });
  }
  if (hasWindow) {
    const label =
      filters.from && filters.to
        ? `${formatPlainDateEs(filters.from)} al ${formatPlainDateEs(filters.to)}`
        : filters.from
          ? `Desde ${formatPlainDateEs(filters.from)}`
          : `Hasta ${formatPlainDateEs(filters.to!)}`;
    filterChips.push({
      key: "window",
      label: `Fechas: ${label}`,
      clear: () => setFilters({ from: null, to: null }),
    });
  }
  if (filters.product) {
    const found = productName(filters.product);
    filterChips.push({
      key: "product",
      label: `Producto: ${
        found ? `${found.inst.name} · ${found.p.name}` : "Producto"
      }`,
      clear: () => setFilters({ product: null }),
    });
  }
  if (filters.institution) {
    const name = allInstitutions.find(
      (inst) => inst.slug === filters.institution
    )?.name;
    filterChips.push({
      key: "institution",
      label: `Institución: ${name ?? filters.institution}`,
      clear: () => setFilters({ institution: null }),
    });
  }
  const periodLabel = hasWindow ? "del período" : "del mes";
  // The summary sums the rows loaded so far ("Cargar más" adds more), unlike
  // the donut, which covers the whole scope.
  const loadedLabel =
    transactions.length === 0
      ? periodLabel
      : transactions.length === 1
        ? "en la transacción cargada"
        : `en las ${transactions.length} transacciones cargadas`;
  /** A summary figure, or a placeholder until the first page arrives. */
  const summaryValue = (value: number) =>
    listLoaded ? formatCLP(value) : "...";

  const handleAdd = async () => {
    const parsedAmount = parseNumberInput(amount);
    if (!description || parsedAmount === null) return;

    const signedAmount =
      (isExpense ? -1 : 1) * Math.abs(Math.round(parsedAmount));

    setSaving(true);
    try {
      const res = await fetch("/api/transactions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          description,
          amount: signedAmount,
          transactionDate,
          // Optional; when none is picked the route resolves the manual product.
          ...(productId && { productId }),
          // Optional; the detail panel edits the rest of the location.
          ...(place.trim() && { location: { name: place } }),
        }),
      });

      if (res.ok) {
        setDescription("");
        setPlace("");
        setAmount("");
        setProductId("");
        refresh();
      }
    } finally {
      setSaving(false);
    }
  };

  const recategorize = async (id: string, categoryId: string) => {
    const res = await fetch(`/api/transactions/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ categoryId: categoryId || null }),
    });
    if (res.ok) refresh();
  };

  // Peso totals over CLP rows only: a USD amount is not pesos. Rows an
  // internal transfer links are the user's money changing product: skipped.
  const {
    expenses: totalExpenses,
    income: totalIncome,
    otherCurrencyCount,
    internalTransferCount,
  } = clpTotals(transactions);

  const getValue = useCallback(
    (txn: Transaction, key: TransactionSortKey): string | number | null => {
      switch (key) {
        case "fecha":
          return txn.transactionDate; // ISO strings sort correctly as strings.
        case "descripcion":
          return txn.description;
        case "categoria":
          return categoryName(txn.categoryId) ?? "";
        case "fuente":
          return txn.source;
        case "monto":
          return txn.amount;
      }
    },
    [categoryName]
  );

  const { sorted, sort, toggleSort } = useSortableData(transactions, getValue);
  // Bridge the generic header's string key to our typed key union.
  const handleSort = (key: string) => toggleSort(key as TransactionSortKey);

  return (
    <div className="space-y-6">
      {/* Summary */}
      <div className="grid gap-4 md:grid-cols-3">
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Gastos {loadedLabel}</CardDescription>
            <CardTitle className="text-xl text-red-600">
              {summaryValue(Math.abs(totalExpenses))}
            </CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Ingresos {loadedLabel}</CardDescription>
            <CardTitle className="text-xl text-green-600">
              {summaryValue(totalIncome)}
            </CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Neto {loadedLabel}</CardDescription>
            <CardTitle className="text-xl">
              {summaryValue(totalIncome + totalExpenses)}
            </CardTitle>
          </CardHeader>
        </Card>
      </div>
      {otherCurrencyCount > 0 && (
        <p className="text-sm text-muted-foreground">
          {otherCurrencyCount === 1
            ? "1 movimiento en otra moneda no se suma a estos totales."
            : `${otherCurrencyCount} movimientos en otras monedas no se suman a estos totales.`}
        </p>
      )}
      {internalTransferCount > 0 && (
        <p className="text-sm text-muted-foreground">
          {internalTransferCount === 1
            ? "1 transferencia interna no se suma a estos totales."
            : `${internalTransferCount} transferencias internas no se suman a estos totales.`}
        </p>
      )}

      {/* Add transaction form */}
      <Card>
        <CardHeader>
          <CardTitle>Agregar movimiento</CardTitle>
          <CardDescription>
            Registra un gasto o un ingreso manualmente (equivale a agregar una
            fila en &quot;Inputs&quot;)
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="flex flex-wrap items-end gap-3">
            <div className="min-w-48 flex-1">
              <label className="mb-1 block text-sm text-muted-foreground">
                Descripcion
              </label>
              <Input
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="Ej: Supermercado"
              />
            </div>
            <div>
              <label className="mb-1 block text-sm text-muted-foreground">
                Tipo
              </label>
              <div className="flex gap-1">
                <Button
                  type="button"
                  variant={isExpense ? "default" : "outline"}
                  onClick={() => setIsExpense(true)}
                >
                  Gasto
                </Button>
                <Button
                  type="button"
                  variant={isExpense ? "outline" : "default"}
                  onClick={() => setIsExpense(false)}
                >
                  Ingreso
                </Button>
              </div>
            </div>
            <div className="w-36">
              <label className="mb-1 block text-sm text-muted-foreground">
                Monto (CLP)
              </label>
              <Input
                type="number"
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
                placeholder="15000"
              />
            </div>
            <div className="w-44">
              <label className="mb-1 block text-sm text-muted-foreground">
                Lugar (opcional)
              </label>
              <Input
                value={place}
                onChange={(e) => setPlace(e.target.value)}
                placeholder="Ej: Tienda de ejemplo"
                maxLength={LOCATION_TEXT_MAX_LENGTH}
              />
            </div>
            <div className="w-56">
              <label className="mb-1 block text-sm text-muted-foreground">
                Producto (opcional)
              </label>
              <ProductSelect
                value={productId}
                onChange={setProductId}
                institutions={institutions}
                emptyLabel="Producto manual"
              />
            </div>
            <div className="w-40">
              <label className="mb-1 block text-sm text-muted-foreground">
                Fecha
              </label>
              <Input
                type="date"
                value={transactionDate}
                onChange={(e) => setTransactionDate(e.target.value)}
              />
            </div>
            <Button onClick={handleAdd} disabled={saving}>
              {saving ? "Guardando..." : "Agregar"}
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* CSV Import */}
      <CsvImport onImported={refresh} />

      {filterChips.length > 0 && (
        <div
          className="flex flex-wrap items-center gap-2"
          aria-label="Filtros activos"
        >
          {filterChips.map((chip) => (
            <Badge key={chip.key} variant="secondary" className="gap-1 pr-1">
              {chip.label}
              <button
                type="button"
                onClick={chip.clear}
                className="rounded-sm p-0.5 hover:bg-muted-foreground/20"
                aria-label={`Quitar filtro ${chip.label}`}
              >
                <X className="size-3" />
              </button>
            </Badge>
          ))}
          {filterChips.length > 1 && (
            <Button
              variant="ghost"
              size="xs"
              onClick={() =>
                setFilters({
                  category: null,
                  from: null,
                  to: null,
                  product: null,
                  institution: null,
                })
              }
            >
              Limpiar filtros
            </Button>
          )}
        </div>
      )}

      {/* Spending per category over the list's scope (category aside) */}
      <Card>
        <CardHeader>
          <CardTitle>Gastos por categoría</CardTitle>
          <CardDescription>
            Lo gastado {periodLabel} en cada categoría, en{" "}
            {spend.data?.currency ?? CLP} y sin transferencias internas. Elige
            una categoría para filtrar la lista.
          </CardDescription>
        </CardHeader>
        <CardContent
          aria-busy={spend.stale}
          className={cn("transition-opacity duration-200", spend.stale && "opacity-60")}
        >
          {spend.error ? (
            <p className="text-sm text-muted-foreground">
              No se pudieron cargar los gastos por categoría.
            </p>
          ) : !spend.data ? (
            <p className="text-sm text-muted-foreground">Cargando...</p>
          ) : (
            <>
              <CategoryDonut
                categories={spend.data.categories}
                total={spend.data.total}
                currency={spend.data.currency}
                onSelect={toggleCategory}
                selectedId={selectedCategoryId}
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

      {/* Transaction list */}
      <Card>
        <CardHeader>
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <CardTitle>Transacciones {periodLabel}</CardTitle>
              <CardDescription>
                {listLoaded
                  ? `${transactions.length} transacciones`
                  : "Cargando..."}
              </CardDescription>
            </div>
            <div>
              <label className="mb-1 block text-sm text-muted-foreground">
                Mes
              </label>
              <Input
                type="month"
                className="w-44"
                value={hasWindow ? "" : month}
                onChange={(e) => {
                  // A new month replaces any from/to window.
                  setMonth(e.target.value || currentMonthInput());
                  if (hasWindow) setFilters({ from: null, to: null });
                }}
              />
            </div>
          </div>
        </CardHeader>
        <CardContent>
          {!listLoaded ? (
            <p className="text-muted-foreground">Cargando...</p>
          ) : transactions.length === 0 ? (
            <p className="text-muted-foreground">
              {filterChips.length > 0
                ? "No hay transacciones con estos filtros."
                : "No hay transacciones este mes. Agrega una arriba o espera a que los scrapers importen datos."}
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <SortableTableHead
                    label="Fecha"
                    columnKey="fecha"
                    active={sort?.key === "fecha"}
                    direction={sort?.key === "fecha" ? sort.direction : undefined}
                    onSort={handleSort}
                  />
                  <SortableTableHead
                    label="Descripcion"
                    columnKey="descripcion"
                    active={sort?.key === "descripcion"}
                    direction={
                      sort?.key === "descripcion" ? sort.direction : undefined
                    }
                    onSort={handleSort}
                  />
                  <SortableTableHead
                    label="Categoría"
                    columnKey="categoria"
                    active={sort?.key === "categoria"}
                    direction={
                      sort?.key === "categoria" ? sort.direction : undefined
                    }
                    onSort={handleSort}
                  />
                  <SortableTableHead
                    label="Fuente"
                    columnKey="fuente"
                    active={sort?.key === "fuente"}
                    direction={
                      sort?.key === "fuente" ? sort.direction : undefined
                    }
                    onSort={handleSort}
                  />
                  <SortableTableHead
                    label="Monto"
                    columnKey="monto"
                    align="right"
                    active={sort?.key === "monto"}
                    direction={sort?.key === "monto" ? sort.direction : undefined}
                    onSort={handleSort}
                  />
                </TableRow>
              </TableHeader>
              <TableBody>
                {sorted.map((txn) => (
                  <TableRow key={txn.id} {...rowProps(txn.id)}>
                    <TableCell className="text-sm">
                      {formatPlainDateEs(txn.transactionDate)}
                    </TableCell>
                    <TableCell>
                      {txn.description}
                      {txn.isInternalTransfer && (
                        <Badge variant="secondary" className="ml-2 text-xs">
                          Transferencia interna
                        </Badge>
                      )}
                    </TableCell>
                    {/* The inline select recategorizes without opening the
                        panel, so its clicks and keys must not reach the row. */}
                    <TableCell
                      onClick={(e) => e.stopPropagation()}
                      onKeyDown={(e) => e.stopPropagation()}
                    >
                      <select
                        className={SELECT_CLASS}
                        value={txn.categoryId ?? ""}
                        onChange={(e) => recategorize(txn.id, e.target.value)}
                        aria-label="Categoría"
                      >
                        <option value="">Sin categoría</option>
                        {categories.map((c) => (
                          <option key={c.id} value={c.id}>
                            {c.name}
                          </option>
                        ))}
                      </select>
                    </TableCell>
                    <TableCell>
                      <Badge variant="outline" className="text-xs">
                        {txn.source}
                      </Badge>
                    </TableCell>
                    <TableCell
                      className={`text-right font-medium ${
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
          {hasMore && (
            <div className="mt-4 flex justify-center">
              <Button
                variant="outline"
                onClick={() =>
                  setPaging({ filterQuery, limit: limit + PAGE_SIZE })
                }
              >
                Cargar más
              </Button>
            </div>
          )}
        </CardContent>
      </Card>

      <TransactionDetailSheet
        transactionId={selectedTxnId}
        onClose={() => selectTransaction(null)}
        onChanged={refresh}
      />
    </div>
  );
}
