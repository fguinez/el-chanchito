"use client";

import { useEffect, useState } from "react";
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
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { formatCLP } from "@/lib/utils";
import { calcPersonalAmount } from "@/lib/budget-engine";
import { Trash2 } from "lucide-react";

interface FixedExpense {
  id: string;
  name: string;
  amount: number;
  isShared: boolean;
  sharedRatio: string | null;
}

export default function FixedExpensesPage() {
  const [expenses, setExpenses] = useState<FixedExpense[]>([]);
  const [name, setName] = useState("");
  const [amount, setAmount] = useState("");
  const [isShared, setIsShared] = useState(false);
  const [sharedRatio, setSharedRatio] = useState("0.69");
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
    if (!name || !amount) return;

    setSaving(true);
    try {
      const res = await fetch("/api/fixed-expenses", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name,
          amount: parseInt(amount),
          isShared,
          sharedRatio: isShared ? parseFloat(sharedRatio) : null,
        }),
      });

      if (res.ok) {
        setName("");
        setAmount("");
        setIsShared(false);
        loadExpenses();
      }
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

  const totalPersonal = expenses.reduce((sum, e) => {
    const ratio = e.sharedRatio ? parseFloat(e.sharedRatio) : 0;
    return sum + calcPersonalAmount(e.amount, e.isShared, ratio);
  }, 0);

  const totalFull = expenses.reduce((sum, e) => sum + e.amount, 0);

  return (
    <div className="space-y-6">
      <h2 className="text-2xl font-bold">Gastos Fijos Mensuales</h2>

      {/* Summary */}
      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Total personal</CardDescription>
            <CardTitle className="text-xl">{formatCLP(totalPersonal)}</CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Total completo (antes de compartir)</CardDescription>
            <CardTitle className="text-xl">{formatCLP(totalFull)}</CardTitle>
          </CardHeader>
        </Card>
      </div>

      {/* Add form */}
      <Card>
        <CardHeader>
          <CardTitle>Agregar gasto fijo</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex items-end gap-3">
            <div className="flex-1">
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
                  value={sharedRatio}
                  onChange={(e) => setSharedRatio(e.target.value)}
                />
              </div>
            )}
            <Button onClick={handleAdd} disabled={saving}>
              {saving ? "..." : "Agregar"}
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* Table */}
      <Card>
        <CardHeader>
          <CardTitle>Gastos fijos</CardTitle>
          <CardDescription>
            {expenses.length} gastos fijos registrados
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
                  <TableHead>Gasto</TableHead>
                  <TableHead className="text-right">Monto total</TableHead>
                  <TableHead className="text-right">Monto personal</TableHead>
                  <TableHead>Compartido</TableHead>
                  <TableHead className="w-12"></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {expenses.map((expense) => {
                  const ratio = expense.sharedRatio
                    ? parseFloat(expense.sharedRatio)
                    : 0;
                  const personal = calcPersonalAmount(
                    expense.amount,
                    expense.isShared,
                    ratio
                  );
                  return (
                    <TableRow key={expense.id}>
                      <TableCell className="font-medium">
                        {expense.name}
                      </TableCell>
                      <TableCell className="text-right">
                        {formatCLP(expense.amount)}
                      </TableCell>
                      <TableCell className="text-right">
                        {formatCLP(personal)}
                      </TableCell>
                      <TableCell>
                        {expense.isShared
                          ? `${Math.round(ratio * 100)}%`
                          : "No"}
                      </TableCell>
                      <TableCell>
                        <button
                          onClick={() => handleDelete(expense.id)}
                          className="text-muted-foreground hover:text-destructive"
                        >
                          <Trash2 className="h-4 w-4" />
                        </button>
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
