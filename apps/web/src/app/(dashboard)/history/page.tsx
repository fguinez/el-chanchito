"use client";

import { useCallback, useEffect, useState } from "react";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  InteractiveChart,
  useTimeSeriesChart,
} from "@/components/charts/interactive-chart";
import { TimeRangeControl } from "@/components/charts/time-range-control";
import { dayStartMs } from "@/components/charts/x-axis-range";
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
import { useSortableData } from "@/lib/use-sortable-data";
import { formatCLP } from "@/lib/utils";
import { Trash2 } from "lucide-react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
} from "recharts";

interface WealthSnapshot {
  id: string;
  snapshotDate: string;
  patrimonio: number;
  deuda: number;
  fintualBalance: number | null;
  mercadopagoBalance: number | null;
  banchileSavings: number | null;
  notes: string | null;
  source: "manual" | "computed";
  ahorro: number;
  periodSavings: number | null;
  monthsBetween: number | null;
  monthlyRate: number | null;
}

type SnapshotSortKey =
  | "fecha"
  | "patrimonio"
  | "deuda"
  | "ahorro"
  | "fintual"
  | "mercadopago"
  | "banchile"
  | "ahorrado"
  | "meses"
  | "tasa";

export default function HistoryPage() {
  const [snapshots, setSnapshots] = useState<WealthSnapshot[]>([]);
  const [form, setForm] = useState({
    snapshotDate: new Date().toISOString().split("T")[0],
    patrimonio: "",
    deuda: "",
    fintualBalance: "",
    mercadopagoBalance: "",
    banchileSavings: "",
    notes: "",
  });
  const [saving, setSaving] = useState(false);
  const chart = useTimeSeriesChart();

  const loadSnapshots = () => {
    fetch("/api/wealth")
      .then((res) => res.json())
      .then(setSnapshots)
      .catch(console.error);
  };

  useEffect(() => {
    loadSnapshots();
  }, []);

  const handleAdd = async () => {
    if (!form.patrimonio) return;
    setSaving(true);
    try {
      const res = await fetch("/api/wealth", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          snapshotDate: form.snapshotDate,
          patrimonio: parseInt(form.patrimonio),
          deuda: form.deuda ? parseInt(form.deuda) : 0,
          fintualBalance: form.fintualBalance
            ? parseInt(form.fintualBalance)
            : null,
          mercadopagoBalance: form.mercadopagoBalance
            ? parseInt(form.mercadopagoBalance)
            : null,
          banchileSavings: form.banchileSavings
            ? parseInt(form.banchileSavings)
            : null,
          notes: form.notes || null,
        }),
      });
      if (res.ok) {
        setForm({
          snapshotDate: new Date().toISOString().split("T")[0],
          patrimonio: "",
          deuda: "",
          fintualBalance: "",
          mercadopagoBalance: "",
          banchileSavings: "",
          notes: "",
        });
        loadSnapshots();
      }
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (id: string) => {
    await fetch("/api/wealth", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }),
    });
    loadSnapshots();
  };

  const chartData = snapshots.map((s) => ({
    t: dayStartMs(s.snapshotDate.slice(0, 10)),
    Patrimonio: s.patrimonio,
    Deuda: s.deuda,
    Ahorro: s.ahorro,
  }));

  const latest = snapshots[snapshots.length - 1];

  const getValue = useCallback(
    (s: WealthSnapshot, key: SnapshotSortKey): string | number | null => {
      switch (key) {
        case "fecha":
          return s.snapshotDate; // ISO "YYYY-MM-DD" strings sort lexically.
        case "patrimonio":
          return s.patrimonio;
        case "deuda":
          return s.deuda;
        case "ahorro":
          return s.ahorro;
        case "fintual":
          return s.fintualBalance;
        case "mercadopago":
          return s.mercadopagoBalance;
        case "banchile":
          return s.banchileSavings;
        case "ahorrado":
          return s.periodSavings;
        case "meses":
          return s.monthsBetween;
        case "tasa":
          return s.monthlyRate;
      }
    },
    []
  );

  const { sorted, sort, toggleSort } = useSortableData(snapshots, getValue);
  // Bridge the generic header's string key to our typed key union.
  const handleSort = (key: string) => toggleSort(key as SnapshotSortKey);

  return (
    <div className="space-y-6">
      <h2 className="text-2xl font-bold">Historial de Patrimonio</h2>

      {/* Summary cards */}
      {latest && (
        <div className="grid gap-4 md:grid-cols-4">
          <Card>
            <CardHeader className="pb-2">
              <CardDescription>Patrimonio</CardDescription>
              <CardTitle className="text-xl">
                {formatCLP(latest.patrimonio)}
              </CardTitle>
            </CardHeader>
          </Card>
          <Card>
            <CardHeader className="pb-2">
              <CardDescription>Deuda</CardDescription>
              <CardTitle className="text-xl text-red-600">
                {formatCLP(latest.deuda)}
              </CardTitle>
            </CardHeader>
          </Card>
          <Card>
            <CardHeader className="pb-2">
              <CardDescription>Ahorro neto</CardDescription>
              <CardTitle className="text-xl text-green-600">
                {formatCLP(latest.ahorro)}
              </CardTitle>
            </CardHeader>
          </Card>
          <Card>
            <CardHeader className="pb-2">
              <CardDescription>Tasa mensual ahorro</CardDescription>
              <CardTitle className="text-xl">
                {latest.monthlyRate !== null
                  ? formatCLP(latest.monthlyRate)
                  : "-"}
              </CardTitle>
            </CardHeader>
          </Card>
        </div>
      )}

      {/* Chart */}
      {chartData.length >= 2 && (
        <Card>
          <CardHeader>
            <CardTitle>Evolucion patrimonial</CardTitle>
            <CardDescription>
              Patrimonio, deuda y ahorro en el tiempo
            </CardDescription>
            <CardAction>
              <TimeRangeControl control={chart.x} allowAll />
            </CardAction>
          </CardHeader>
          <CardContent>
            <InteractiveChart {...chart.interactiveProps} height={350}>
              <LineChart data={chartData}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis {...chart.xAxisProps} />
                <YAxis {...chart.yAxisProps} />
                <Tooltip
                  formatter={(value) => formatCLP(Number(value))}
                  labelFormatter={chart.labelFormatter}
                />
                <Legend />
                <Line
                  type="monotone"
                  dataKey="Patrimonio"
                  stroke="#2563eb"
                  strokeWidth={2}
                  dot={{ r: 3 }}
                  isAnimationActive={false}
                />
                <Line
                  type="monotone"
                  dataKey="Deuda"
                  stroke="#dc2626"
                  strokeWidth={2}
                  dot={{ r: 3 }}
                  isAnimationActive={false}
                />
                <Line
                  type="monotone"
                  dataKey="Ahorro"
                  stroke="#16a34a"
                  strokeWidth={2}
                  dot={{ r: 3 }}
                  isAnimationActive={false}
                />
              </LineChart>
            </InteractiveChart>
          </CardContent>
        </Card>
      )}

      {/* Add snapshot form */}
      <Card>
        <CardHeader>
          <CardTitle>Agregar registro</CardTitle>
          <CardDescription>
            Registra un punto en el historial de patrimonio
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="grid gap-3 md:grid-cols-3">
            <div>
              <label className="mb-1 block text-sm text-muted-foreground">
                Fecha
              </label>
              <Input
                type="date"
                value={form.snapshotDate}
                onChange={(e) =>
                  setForm((p) => ({ ...p, snapshotDate: e.target.value }))
                }
              />
            </div>
            <div>
              <label className="mb-1 block text-sm text-muted-foreground">
                Patrimonio total
              </label>
              <Input
                type="number"
                value={form.patrimonio}
                onChange={(e) =>
                  setForm((p) => ({ ...p, patrimonio: e.target.value }))
                }
                placeholder="2500000"
              />
            </div>
            <div>
              <label className="mb-1 block text-sm text-muted-foreground">
                Deuda total
              </label>
              <Input
                type="number"
                value={form.deuda}
                onChange={(e) =>
                  setForm((p) => ({ ...p, deuda: e.target.value }))
                }
                placeholder="999999"
              />
            </div>
            <div>
              <label className="mb-1 block text-sm text-muted-foreground">
                Fintual
              </label>
              <Input
                type="number"
                value={form.fintualBalance}
                onChange={(e) =>
                  setForm((p) => ({ ...p, fintualBalance: e.target.value }))
                }
                placeholder="1000000"
              />
            </div>
            <div>
              <label className="mb-1 block text-sm text-muted-foreground">
                Mercado Pago
              </label>
              <Input
                type="number"
                value={form.mercadopagoBalance}
                onChange={(e) =>
                  setForm((p) => ({ ...p, mercadopagoBalance: e.target.value }))
                }
                placeholder="1000000"
              />
            </div>
            <div>
              <label className="mb-1 block text-sm text-muted-foreground">
                BanChile Ahorro
              </label>
              <Input
                type="number"
                value={form.banchileSavings}
                onChange={(e) =>
                  setForm((p) => ({ ...p, banchileSavings: e.target.value }))
                }
                placeholder="999999"
              />
            </div>
          </div>
          <div className="mt-3 flex items-end gap-3">
            <div className="flex-1">
              <label className="mb-1 block text-sm text-muted-foreground">
                Notas
              </label>
              <Input
                value={form.notes}
                onChange={(e) =>
                  setForm((p) => ({ ...p, notes: e.target.value }))
                }
                placeholder="Opcional"
              />
            </div>
            <Button onClick={handleAdd} disabled={saving}>
              {saving ? "Guardando..." : "Agregar"}
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* Snapshot table */}
      <Card>
        <CardHeader>
          <CardTitle>Registros historicos</CardTitle>
          <CardDescription>
            {snapshots.length} registros
          </CardDescription>
        </CardHeader>
        <CardContent>
          {snapshots.length === 0 ? (
            <p className="text-muted-foreground">
              No hay registros. Agrega el primero arriba.
            </p>
          ) : (
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <SortableTableHead
                      label="Fecha"
                      columnKey="fecha"
                      active={sort?.key === "fecha"}
                      direction={
                        sort?.key === "fecha" ? sort.direction : undefined
                      }
                      onSort={handleSort}
                    />
                    <SortableTableHead
                      label="Patrimonio"
                      columnKey="patrimonio"
                      align="right"
                      active={sort?.key === "patrimonio"}
                      direction={
                        sort?.key === "patrimonio" ? sort.direction : undefined
                      }
                      onSort={handleSort}
                    />
                    <SortableTableHead
                      label="Deuda"
                      columnKey="deuda"
                      align="right"
                      active={sort?.key === "deuda"}
                      direction={
                        sort?.key === "deuda" ? sort.direction : undefined
                      }
                      onSort={handleSort}
                    />
                    <SortableTableHead
                      label="Ahorro"
                      columnKey="ahorro"
                      align="right"
                      active={sort?.key === "ahorro"}
                      direction={
                        sort?.key === "ahorro" ? sort.direction : undefined
                      }
                      onSort={handleSort}
                    />
                    <SortableTableHead
                      label="Fintual"
                      columnKey="fintual"
                      align="right"
                      active={sort?.key === "fintual"}
                      direction={
                        sort?.key === "fintual" ? sort.direction : undefined
                      }
                      onSort={handleSort}
                    />
                    <SortableTableHead
                      label="MercadoPago"
                      columnKey="mercadopago"
                      align="right"
                      active={sort?.key === "mercadopago"}
                      direction={
                        sort?.key === "mercadopago" ? sort.direction : undefined
                      }
                      onSort={handleSort}
                    />
                    <SortableTableHead
                      label="BanChile"
                      columnKey="banchile"
                      align="right"
                      active={sort?.key === "banchile"}
                      direction={
                        sort?.key === "banchile" ? sort.direction : undefined
                      }
                      onSort={handleSort}
                    />
                    <SortableTableHead
                      label="Ahorrado"
                      columnKey="ahorrado"
                      align="right"
                      active={sort?.key === "ahorrado"}
                      direction={
                        sort?.key === "ahorrado" ? sort.direction : undefined
                      }
                      onSort={handleSort}
                    />
                    <SortableTableHead
                      label="Meses"
                      columnKey="meses"
                      align="right"
                      active={sort?.key === "meses"}
                      direction={
                        sort?.key === "meses" ? sort.direction : undefined
                      }
                      onSort={handleSort}
                    />
                    <SortableTableHead
                      label="Tasa/mes"
                      columnKey="tasa"
                      align="right"
                      active={sort?.key === "tasa"}
                      direction={
                        sort?.key === "tasa" ? sort.direction : undefined
                      }
                      onSort={handleSort}
                    />
                    <TableHead className="w-10"></TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {sorted.map((s) => (
                    <TableRow key={s.id}>
                      <TableCell className="whitespace-nowrap">
                        {new Date(s.snapshotDate).toLocaleDateString("es-CL")}
                      </TableCell>
                      <TableCell className="text-right">
                        {formatCLP(s.patrimonio)}
                      </TableCell>
                      <TableCell className="text-right text-red-600">
                        {formatCLP(s.deuda)}
                      </TableCell>
                      <TableCell className="text-right text-green-600">
                        {formatCLP(s.ahorro)}
                      </TableCell>
                      <TableCell className="text-right">
                        {s.fintualBalance != null
                          ? formatCLP(s.fintualBalance)
                          : "-"}
                      </TableCell>
                      <TableCell className="text-right">
                        {s.mercadopagoBalance != null
                          ? formatCLP(s.mercadopagoBalance)
                          : "-"}
                      </TableCell>
                      <TableCell className="text-right">
                        {s.banchileSavings != null
                          ? formatCLP(s.banchileSavings)
                          : "-"}
                      </TableCell>
                      <TableCell className="text-right">
                        {s.periodSavings != null
                          ? formatCLP(s.periodSavings)
                          : "-"}
                      </TableCell>
                      <TableCell className="text-right">
                        {s.monthsBetween ?? "-"}
                      </TableCell>
                      <TableCell className="text-right">
                        {s.monthlyRate != null
                          ? formatCLP(s.monthlyRate)
                          : "-"}
                      </TableCell>
                      <TableCell>
                        {s.source === "manual" && (
                          <button
                            onClick={() => handleDelete(s.id)}
                            className="text-muted-foreground hover:text-destructive"
                          >
                            <Trash2 className="h-4 w-4" />
                          </button>
                        )}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
