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
import { Badge } from "@/components/ui/badge";
import { formatCLP } from "@/lib/utils";
import { CsvImport } from "@/components/dashboard/CsvImport";

interface Transaction {
  id: string;
  description: string;
  amount: number;
  transactionDate: string;
  scheduledMonth: string | null;
  source: string;
  notes: string | null;
}

export default function ExpensesPage() {
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
                  <TableHead>Fecha</TableHead>
                  <TableHead>Descripcion</TableHead>
                  <TableHead>Fuente</TableHead>
                  <TableHead className="text-right">Monto</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {transactions.map((txn) => (
                  <TableRow key={txn.id}>
                    <TableCell className="text-sm">
                      {new Date(txn.transactionDate).toLocaleDateString("es-CL")}
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
    </div>
  );
}
