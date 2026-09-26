"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
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
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { FixedExpenseEditDialog } from "@/components/fixed-expenses/fixed-expense-edit-dialog";
import { useSortableData } from "@/lib/use-sortable-data";
import { cn, formatCLP } from "@/lib/utils";
import {
  fixedExpensePersonalAmount,
  fixedExpenseStatus,
  fixedExpenseTotals,
  formatActiveWindow,
  parseFixedExpenseForm,
  ratioInput,
  sharedRatioValue,
  sortByStatus,
  type FixedExpense,
} from "@/lib/fixed-expenses";
import { formatLocalDate } from "@/lib/monitors/history";
import { Trash2 } from "lucide-react";

const STATUS_BADGES = {
  scheduled: { label: "Programado", variant: "outline" },
  ended: { label: "Finalizado", variant: "secondary" },
} as const;

type ExpenseSortKey = "gasto" | "total" | "personal" | "compartido";

export default function FixedExpensesPage() {
  const [expenses, setExpenses] = useState<FixedExpense[]>([]);
  const [name, setName] = useState("");
  const [amount, setAmount] = useState("");
  const [isShared, setIsShared] = useState(false);
  const [sharedRatio, setSharedRatio] = useState(ratioInput(null));
  const [activeFrom, setActiveFrom] = useState("");
  const [activeTo, setActiveTo] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const loadExpenses = () => {
    fetch("/api/fixed-expenses")
      .then((res) => res.json())
      .then(setExpenses)
      .catch(console.error);
  };

  useEffect(() => {
    loadExpenses();
  }, []);

  const handleAdd = async () => {
    const parsed = parseFixedExpenseForm({
      name,
      amount,
      isShared,
      sharedRatio,
      activeFrom,
      activeTo,
    });
    if (!parsed.ok) {
      setFormError(parsed.error);
      return;
    }

    setSaving(true);
    setFormError(null);
    try {
      const res = await fetch("/api/fixed-expenses", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(parsed.body),
      });

      if (res.ok) {
        setName("");
        setAmount("");
        setIsShared(false);
        setActiveFrom("");
        setActiveTo("");
        loadExpenses();
      } else {
        const data = await res.json().catch(() => null);
        setFormError(data?.error ?? "No se pudo agregar el gasto");
      }
    } catch {
      setFormError("No se pudo agregar el gasto");
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (id: string) => {
    await fetch("/api/fixed-expenses", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }),
    });
    loadExpenses();
  };

  // Vigencia is decided on the browser's local day, never the UTC date.
  const today = formatLocalDate(new Date());
  const totals = fixedExpenseTotals(expenses, today);
  const registeredLabel =
    expenses.length === 1
      ? "1 gasto fijo registrado"
      : `${expenses.length} gastos fijos registrados`;
  const activeLabel =
    totals.activeCount === 1
      ? "1 vigente hoy"
      : `${totals.activeCount} vigentes hoy`;

  const getValue = useCallback(
    (expense: FixedExpense, key: ExpenseSortKey): string | number | null => {
      switch (key) {
        case "gasto":
          return expense.name;
        case "total":
          return expense.amount;
        case "personal":
          return fixedExpensePersonalAmount(expense);
        case "compartido":
          // "Compartido" renders a share % (or "No", i.e. 0%): sort numerically.
          return expense.isShared ? sharedRatioValue(expense.sharedRatio) : 0;
      }
    },
    []
  );

  const byStatus = useMemo(() => sortByStatus(expenses, today), [expenses, today]);
  const { sorted, sort, toggleSort } = useSortableData(byStatus, getValue);
  // Bridge the generic header's string key to our typed key union.
  const handleSort = (key: string) => toggleSort(key as ExpenseSortKey);

  return (
    <div className="space-y-6">
      <h2 className="text-2xl font-bold">Gastos Fijos Mensuales</h2>

      {/* Summary */}
      <div className="space-y-2">
        <div className="grid gap-4 md:grid-cols-2">
          <Card>
            <CardHeader className="pb-2">
              <CardDescription>Total personal</CardDescription>
              <CardTitle className="text-xl">
                {formatCLP(totals.personal)}
              </CardTitle>
            </CardHeader>
          </Card>
          <Card>
            <CardHeader className="pb-2">
              <CardDescription>
                Total completo (antes de compartir)
              </CardDescription>
              <CardTitle className="text-xl">{formatCLP(totals.full)}</CardTitle>
            </CardHeader>
          </Card>
        </div>
        <p className="text-xs text-muted-foreground">
          Los totales suman solo los gastos vigentes hoy.
        </p>
      </div>

      {/* Add form */}
      <Card>
        <CardHeader>
          <CardTitle>Agregar gasto fijo</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex flex-wrap items-end gap-3">
            <div className="min-w-48 flex-1">
              <label className="mb-1 block text-sm text-muted-foreground">
                Nombre
              </label>
              <Input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Ej: Arriendo"
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
                placeholder="500000"
              />
            </div>
            <div className="flex items-center gap-2">
              <input
                type="checkbox"
                id="shared"
                checked={isShared}
                onChange={(e) => setIsShared(e.target.checked)}
                className="h-4 w-4"
              />
              <label htmlFor="shared" className="text-sm">
                Compartido
              </label>
            </div>
            {isShared && (
              <div className="w-24">
                <label className="mb-1 block text-sm text-muted-foreground">
                  Ratio
                </label>
                <Input
                  type="number"
                  step="0.01"
                  min="0"
                  max="1"
                  value={sharedRatio}
                  onChange={(e) => setSharedRatio(e.target.value)}
                />
              </div>
            )}
            <div className="w-40">
              <label
                htmlFor="active-from"
                className="mb-1 block text-sm text-muted-foreground"
              >
                Vigente desde
              </label>
              <Input
                id="active-from"
                type="date"
                value={activeFrom}
                onChange={(e) => setActiveFrom(e.target.value)}
              />
            </div>
            <div className="w-40">
              <label
                htmlFor="active-to"
                className="mb-1 block text-sm text-muted-foreground"
              >
                Vigente hasta
              </label>
              <Input
                id="active-to"
                type="date"
                value={activeTo}
                onChange={(e) => setActiveTo(e.target.value)}
              />
            </div>
            <Button onClick={handleAdd} disabled={saving}>
              {saving ? "..." : "Agregar"}
            </Button>
          </div>
          {formError && (
            <p className="mt-3 text-sm text-destructive">{formError}</p>
          )}
        </CardContent>
      </Card>

      {/* Table */}
      <Card>
        <CardHeader>
          <CardTitle>Gastos fijos</CardTitle>
          <CardDescription>
            {registeredLabel}, {activeLabel}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {expenses.length === 0 ? (
            <p className="text-muted-foreground">
              No hay gastos fijos registrados. Agrega uno arriba.
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <SortableTableHead
                    label="Gasto"
                    columnKey="gasto"
                    active={sort?.key === "gasto"}
                    direction={sort?.key === "gasto" ? sort.direction : undefined}
                    onSort={handleSort}
                  />
                  <SortableTableHead
                    label="Monto total"
                    columnKey="total"
                    align="right"
                    active={sort?.key === "total"}
                    direction={sort?.key === "total" ? sort.direction : undefined}
                    onSort={handleSort}
                  />
                  <SortableTableHead
                    label="Monto personal"
                    columnKey="personal"
                    align="right"
                    active={sort?.key === "personal"}
                    direction={
                      sort?.key === "personal" ? sort.direction : undefined
                    }
                    onSort={handleSort}
                  />
                  <SortableTableHead
                    label="Compartido"
                    columnKey="compartido"
                    align="right"
                    active={sort?.key === "compartido"}
                    direction={
                      sort?.key === "compartido" ? sort.direction : undefined
                    }
                    onSort={handleSort}
                  />
                  <TableHead>Vigencia</TableHead>
                  <TableHead className="w-16"></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {sorted.map((expense) => {
                  const ratio = sharedRatioValue(expense.sharedRatio);
                  const personal = fixedExpensePersonalAmount(expense);
                  const status = fixedExpenseStatus(expense, today);
                  const badge = status === "active" ? null : STATUS_BADGES[status];
                  return (
                    <TableRow
                      key={expense.id}
                      className={cn(badge && "text-muted-foreground")}
                    >
                      <TableCell className="font-medium">
                        <span className="flex items-center gap-2">
                          {expense.name}
                          {badge && (
                            <Badge variant={badge.variant}>{badge.label}</Badge>
                          )}
                        </span>
                      </TableCell>
                      <TableCell className="text-right">
                        {formatCLP(expense.amount)}
                      </TableCell>
                      <TableCell className="text-right">
                        {formatCLP(personal)}
                      </TableCell>
                      <TableCell className="text-right">
                        {expense.isShared
                          ? `${Math.round(ratio * 100)}%`
                          : "No"}
                      </TableCell>
                      <TableCell>{formatActiveWindow(expense)}</TableCell>
                      <TableCell>
                        <div className="flex items-center gap-3">
                          <FixedExpenseEditDialog
                            expense={expense}
                            onSaved={loadExpenses}
                          />
                          <button
                            onClick={() => handleDelete(expense.id)}
                            aria-label="Eliminar"
                            className="text-muted-foreground hover:text-destructive"
                          >
                            <Trash2 className="h-4 w-4" />
                          </button>
                        </div>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
