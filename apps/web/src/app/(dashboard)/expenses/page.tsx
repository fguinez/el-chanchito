"use client";

import { Suspense, useCallback, useEffect, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
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
import { formatCLP, formatPlainDateEs } from "@/lib/utils";
import { CsvImport } from "@/components/dashboard/CsvImport";
import { TransactionDetailSheet } from "@/components/transactions/transaction-detail-sheet";

interface Transaction {
  id: string;
  description: string;
  amount: number;
  transactionDate: string;
  scheduledMonth: string | null;
  source: string;
  notes: string | null;
}

type TransactionSortKey = "fecha" | "descripcion" | "fuente" | "monto";

/** Query param holding the transaction shown in the detail panel. */
const TXN_PARAM = "txn";

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
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const selectedTxnId = searchParams.get(TXN_PARAM);

  /** Opens (id) or closes (null) the detail panel, keeping other params. */
  const selectTransaction = useCallback(
    (id: string | null) => {
      const params = new URLSearchParams(searchParams.toString());
      if (id) params.set(TXN_PARAM, id);
      else params.delete(TXN_PARAM);
      const query = params.toString();
      router.replace(query ? `${pathname}?${query}` : pathname, {
        scroll: false,
      });
    },
    [router, pathname, searchParams]
  );

  const [transactions, setTransactions] = useState<Transaction[]>([]);
  const [description, setDescription] = useState("");
  const [amount, setAmount] = useState("");
  const [transactionDate, setTransactionDate] = useState(
    new Date().toISOString().split("T")[0]
  );
  const [saving, setSaving] = useState(false);

  const loadTransactions = () => {
    const now = new Date();
    const month = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-01`;
    fetch(`/api/transactions?month=${month}`)
      .then((res) => res.json())
      .then(setTransactions)
      .catch(console.error);
  };

  useEffect(() => {
    loadTransactions();
  }, []);

  const handleAdd = async () => {
    if (!description || !amount) return;

    setSaving(true);
    try {
      const res = await fetch("/api/transactions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          description,
          amount: -Math.abs(parseInt(amount)), // expenses are negative
          transactionDate,
        }),
      });

      if (res.ok) {
        setDescription("");
        setAmount("");
        loadTransactions();
      }
    } finally {
      setSaving(false);
    }
  };

  const totalExpenses = transactions
    .filter((t) => t.amount < 0)
    .reduce((sum, t) => sum + t.amount, 0);
  const totalIncome = transactions
    .filter((t) => t.amount > 0)
    .reduce((sum, t) => sum + t.amount, 0);

  const getValue = useCallback(
    (txn: Transaction, key: TransactionSortKey): string | number | null => {
      switch (key) {
        case "fecha":
          return txn.transactionDate; // ISO strings sort correctly as strings.
        case "descripcion":
          return txn.description;
        case "fuente":
          return txn.source;
        case "monto":
          return txn.amount;
      }
    },
    []
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

      {/* Add transaction form */}
      <Card>
        <CardHeader>
          <CardTitle>Agregar gasto</CardTitle>
          <CardDescription>
            Registra un gasto manualmente (equivale a agregar una fila en
            &quot;Inputs&quot;)
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="flex items-end gap-3">
            <div className="flex-1">
              <label className="mb-1 block text-sm text-muted-foreground">
                Descripcion
              </label>
              <Input
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="Ej: Supermercado"
              />
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
          <CardTitle>Transacciones del mes</CardTitle>
          <CardDescription>{transactions.length} transacciones</CardDescription>
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
                  <TableRow
                    key={txn.id}
                    tabIndex={0}
                    data-state={
                      txn.id === selectedTxnId ? "selected" : undefined
                    }
                    className="cursor-pointer focus-visible:bg-muted/50 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring"
                    aria-label={`Ver detalle: ${txn.description}`}
                    onClick={() => {
                      // Let users select and copy cell text without opening.
                      if (window.getSelection()?.toString()) return;
                      selectTransaction(txn.id);
                    }}
                    onKeyDown={(e) => {
                      if (e.target !== e.currentTarget) return;
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        selectTransaction(txn.id);
                      }
                    }}
                  >
                    <TableCell className="text-sm">
                      {formatPlainDateEs(txn.transactionDate)}
                    </TableCell>
                    <TableCell>{txn.description}</TableCell>
                    <TableCell>
                      <Badge variant="outline" className="text-xs">
                        {txn.source}
                      </Badge>
                    </TableCell>
                    <TableCell
                      className={`text-right font-medium ${
                        txn.amount < 0 ? "text-red-600" : "text-green-600"
                      }`}
                    >
                      {txn.amount < 0 ? "-" : "+"}
                      {formatCLP(Math.abs(txn.amount))}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <TransactionDetailSheet
        transactionId={selectedTxnId}
        onClose={() => selectTransaction(null)}
      />
    </div>
  );
}
