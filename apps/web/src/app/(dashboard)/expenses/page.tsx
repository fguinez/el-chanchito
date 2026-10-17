"use client";

import { Suspense, useCallback, useEffect, useState } from "react";
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
  ProductSelect,
  useActiveProducts,
  useCategories,
} from "@/components/transactions/pickers";
import { SELECT_CLASS } from "@/components/ui/native-select";
import { LOCATION_TEXT_MAX_LENGTH } from "@/lib/transaction-location";

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
      <h2 className="text-2xl font-bold">Gastos y Transacciones</h2>
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

  const [transactions, setTransactions] = useState<Transaction[]>([]);
  const [month, setMonth] = useState(currentMonthInput());
  const [limit, setLimit] = useState(PAGE_SIZE);
  const [hasMore, setHasMore] = useState(false);
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
  // CLP-only products for the manual form, like the transfers picker.
  const { institutions } = useActiveProducts(CLP);

  // Any day of the picked month: the API reads the month off the string.
  const loadTransactions = useCallback(() => {
    fetch(`/api/transactions?month=${month}-01&limit=${limit}`)
      .then((res) => res.json())
      .then((rows: Transaction[]) => {
        setTransactions(rows);
        setHasMore(rows.length >= limit);
      })
      .catch(console.error);
  }, [month, limit]);

  useEffect(() => {
    loadTransactions();
  }, [loadTransactions]);

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
        loadTransactions();
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
    if (res.ok) loadTransactions();
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
      <h2 className="text-2xl font-bold">Gastos y Transacciones</h2>

      {/* Summary */}
      <div className="grid gap-4 md:grid-cols-3">
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Gastos del mes</CardDescription>
            <CardTitle className="text-xl text-red-600">
              {formatCLP(Math.abs(totalExpenses))}
            </CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Ingresos del mes</CardDescription>
            <CardTitle className="text-xl text-green-600">
              {formatCLP(totalIncome)}
            </CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Neto</CardDescription>
            <CardTitle className="text-xl">
              {formatCLP(totalIncome + totalExpenses)}
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
      <CsvImport onImported={loadTransactions} />

      {/* Transaction list */}
      <Card>
        <CardHeader>
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <CardTitle>Transacciones del mes</CardTitle>
              <CardDescription>
                {transactions.length} transacciones
              </CardDescription>
            </div>
            <div>
              <label className="mb-1 block text-sm text-muted-foreground">
                Mes
              </label>
              <Input
                type="month"
                className="w-44"
                value={month}
                onChange={(e) => {
                  // A new month resets paging back to the first page.
                  setLimit(PAGE_SIZE);
                  setMonth(e.target.value || currentMonthInput());
                }}
              />
            </div>
          </div>
        </CardHeader>
        <CardContent>
          {transactions.length === 0 ? (
            <p className="text-muted-foreground">
              No hay transacciones este mes. Agrega una arriba o espera a que
              los scrapers importen datos.
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
                onClick={() => setLimit((n) => n + PAGE_SIZE)}
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
        onChanged={loadTransactions}
      />
    </div>
  );
}
