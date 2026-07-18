"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { Pencil, Trash2 } from "lucide-react";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  Card,
  CardAction,
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
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { formatAmount } from "@/lib/utils";
import {
  SEVERITY_LABELS,
  StatusBadge,
  formatAxisValue,
  formatDateEs,
  formatDayEs,
  type ApiMonitor,
  type HistoryPoint,
  type MonitorReference,
} from "@/components/monitors/shared";

interface MonitorDetail extends ApiMonitor {
  history: HistoryPoint[];
  references: MonitorReference[];
}

/** Selectable chart windows, passed to the API as `?days=N`. */
const RANGE_OPTIONS = [
  { days: 7, label: "7d" },
  { days: 30, label: "30d" },
  { days: 90, label: "90d" },
  { days: 180, label: "180d" },
  { days: 365, label: "1a" },
] as const;

const DEFAULT_RANGE_DAYS = 30;

/** Recharts rows: the value plus one column per threshold severity. */
function buildChartData(history: HistoryPoint[]) {
  return history.map((point) => ({
    date: formatDayEs(point.date),
    Valor: point.value,
    Alerta:
      point.thresholds.find((t) => t.severity === "alert")?.value ?? null,
    Advertencia:
      point.thresholds.find((t) => t.severity === "warning")?.value ?? null,
  }));
}

export default function MonitorDetailPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();

  const [monitor, setMonitor] = useState<MonitorDetail | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [rangeDays, setRangeDays] = useState<number>(DEFAULT_RANGE_DAYS);
  const [historyLoading, setHistoryLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setHistoryLoading(true);
    fetch(`/api/monitors/${id}?days=${rangeDays}`)
      .then((res) => {
        if (res.status === 404) {
          if (!cancelled) setNotFound(true);
          throw new Error("not found");
        }
        if (!res.ok) throw new Error("failed");
        return res.json();
      })
      .then((data: MonitorDetail) => {
        if (!cancelled) setMonitor(data);
      })
      .catch(() => {
        if (!cancelled) setError(true);
      })
      .finally(() => {
        if (!cancelled) setHistoryLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [id, rangeDays]);

  async function handleDelete() {
    if (
      !window.confirm(
        "¿Eliminar este monitor? Esta acción no se puede deshacer."
      )
    ) {
      return;
    }
    try {
      const res = await fetch(`/api/monitors/${id}`, { method: "DELETE" });
      if (!res.ok) throw new Error("failed");
      router.push("/monitors");
    } catch {
      setActionError("No se pudo eliminar el monitor.");
    }
  }

  if (notFound) {
    return (
      <div className="space-y-6">
        <h2 className="text-2xl font-bold">Monitor</h2>
        <p className="text-muted-foreground">Monitor no encontrado.</p>
        <Button asChild variant="outline" size="sm">
          <Link href="/monitors">Volver a Monitores</Link>
        </Button>
      </div>
    );
  }
  if (error && !monitor) {
    return (
      <div className="space-y-6">
        <h2 className="text-2xl font-bold">Monitor</h2>
        <p className="text-muted-foreground">No se pudo cargar el monitor.</p>
      </div>
    );
  }
  if (!monitor) {
    return (
      <div className="space-y-6">
        <h2 className="text-2xl font-bold">Monitor</h2>
        <p className="text-muted-foreground">Cargando...</p>
      </div>
    );
  }

  const { evaluation } = monitor;
  const hasWarningThreshold = monitor.thresholds.some(
    (t) => t.severity === "warning"
  );
  const chartData = buildChartData(monitor.history);
  const showMargin = monitor.display.show_margin && evaluation.margin != null;

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="flex flex-wrap items-center gap-2 text-2xl font-bold">
            {monitor.name}
            <StatusBadge status={evaluation.status} />
            {!monitor.isActive && <Badge variant="outline">Inactivo</Badge>}
            <Badge variant="secondary">{monitor.currency}</Badge>
          </h2>
          {monitor.description && (
            <p className="text-sm text-muted-foreground">
              {monitor.description}
            </p>
          )}
        </div>
        <div className="flex items-center gap-2">
          <Button asChild variant="outline" size="sm">
            <Link href={`/monitors/${monitor.id}/edit`}>
              <Pencil className="h-4 w-4" />
              Editar
            </Link>
          </Button>
          <Button variant="destructive" size="sm" onClick={handleDelete}>
            <Trash2 className="h-4 w-4" />
            Eliminar
          </Button>
        </div>
      </div>

      {actionError && <p className="text-sm text-destructive">{actionError}</p>}

      {/* Current state */}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Valor actual</CardDescription>
            <CardTitle className="text-2xl tabular-nums">
              {evaluation.value != null ? (
                formatAmount(monitor.currency, evaluation.value)
              ) : (
                <span className="text-muted-foreground">sin dato</span>
              )}
            </CardTitle>
            {evaluation.staleAsOf && (
              <p className="text-xs text-muted-foreground">
                al {formatDateEs(evaluation.staleAsOf)}
              </p>
            )}
          </CardHeader>
        </Card>
        {evaluation.thresholds.map((t) => (
          <Card key={t.severity}>
            <CardHeader className="pb-2">
              <CardDescription>
                Umbral de {SEVERITY_LABELS[t.severity]} hoy
              </CardDescription>
              <CardTitle className="text-2xl tabular-nums">
                {t.value != null ? (
                  formatAmount(monitor.currency, t.value)
                ) : (
                  <span className="text-muted-foreground">sin dato</span>
                )}
              </CardTitle>
            </CardHeader>
          </Card>
        ))}
        {showMargin && (
          <Card>
            <CardHeader className="pb-2">
              <CardDescription>Margen</CardDescription>
              <CardTitle className="text-2xl tabular-nums">
                {formatAmount(monitor.currency, evaluation.margin!)}
              </CardTitle>
              <p className="text-xs text-muted-foreground">
                antes de cruzar el umbral más cercano
              </p>
            </CardHeader>
          </Card>
        )}
      </div>

      {evaluation.status === "no_data" && evaluation.noDataReason && (
        <p className="text-sm text-muted-foreground">
          Sin datos: {evaluation.noDataReason}
        </p>
      )}

      {/* History chart (line) or big stat, per the monitor's display config */}
      {monitor.display.chart === "line" ? (
        <Card>
          <CardHeader>
            <CardTitle>Historial</CardTitle>
            <CardDescription>
              Valor de la expresión frente a sus umbrales, día a día
            </CardDescription>
            <CardAction>
              <div
                className="flex items-center gap-1"
                role="group"
                aria-label="Rango del gráfico"
              >
                {RANGE_OPTIONS.map((option) => (
                  <Button
                    key={option.days}
                    variant={option.days === rangeDays ? "secondary" : "ghost"}
                    size="xs"
                    onClick={() => setRangeDays(option.days)}
                  >
                    {option.label}
                  </Button>
                ))}
              </div>
            </CardAction>
          </CardHeader>
          <CardContent
            className={historyLoading ? "opacity-60 transition-opacity" : ""}
          >
            {chartData.some((p) => p.Valor != null) ? (
              <>
                <ResponsiveContainer width="100%" height={350}>
                  <LineChart data={chartData}>
                    <CartesianGrid strokeDasharray="3 3" />
                    <XAxis dataKey="date" fontSize={12} />
                    <YAxis tickFormatter={formatAxisValue} fontSize={12} />
                    <Tooltip
                      formatter={(value) =>
                        formatAmount(monitor.currency, Number(value))
                      }
                    />
                    <Legend />
                    <Line
                      type="monotone"
                      dataKey="Valor"
                      stroke="#2563eb"
                      strokeWidth={2}
                      dot={false}
                    />
                    <Line
                      type="monotone"
                      dataKey="Alerta"
                      stroke="#dc2626"
                      strokeWidth={1.5}
                      strokeDasharray="6 4"
                      dot={false}
                    />
                    {hasWarningThreshold && (
                      <Line
                        type="monotone"
                        dataKey="Advertencia"
                        stroke="#d97706"
                        strokeWidth={1.5}
                        strokeDasharray="6 4"
                        dot={false}
                      />
                    )}
                  </LineChart>
                </ResponsiveContainer>
                <p className="mt-2 text-xs text-muted-foreground">
                  Aproximación: los días pasados se convierten con los tipos de
                  cambio actuales.
                </p>
              </>
            ) : (
              <p className="text-muted-foreground">
                Aún no hay historial para este monitor.
              </p>
            )}
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardContent className="space-y-2 text-center">
            <div className="text-5xl font-bold tabular-nums">
              {evaluation.value != null ? (
                formatAmount(monitor.currency, evaluation.value)
              ) : (
                <span className="text-muted-foreground">sin dato</span>
              )}
            </div>
            <div className="flex items-center justify-center gap-3">
              <StatusBadge status={evaluation.status} />
              {showMargin && (
                <span className="text-sm text-muted-foreground tabular-nums">
                  margen {formatAmount(monitor.currency, evaluation.margin!)}
                </span>
              )}
            </div>
          </CardContent>
        </Card>
      )}

      {/* The stored equation, in display syntax */}
      <Card>
        <CardHeader>
          <CardTitle>Ecuación</CardTitle>
        </CardHeader>
        <CardContent>
          <pre className="overflow-x-auto rounded-md bg-muted p-3 font-mono text-sm whitespace-pre-wrap">
            {monitor.displayExpression}
            {monitor.thresholds.map(
              (t) =>
                `\n${t.comparator} ${t.displayExpression}   (${SEVERITY_LABELS[t.severity]})`
            )}
          </pre>
        </CardContent>
      </Card>

      {/* Referenced products */}
      <Card>
        <CardHeader>
          <CardTitle>Referencias</CardTitle>
          <CardDescription>
            Productos que participan en la ecuación
          </CardDescription>
        </CardHeader>
        <CardContent>
          {monitor.references.length === 0 ? (
            <p className="text-muted-foreground">
              La ecuación no referencia productos.
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Referencia</TableHead>
                  <TableHead>Producto</TableHead>
                  <TableHead className="text-right">Valor actual</TableHead>
                  <TableHead className="text-right">Al</TableHead>
                  <TableHead></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {monitor.references.map((ref) => (
                  <TableRow key={`${ref.productId}:${ref.field}`}>
                    <TableCell className="font-mono text-xs">
                      {ref.institutionSlug ?? "?"}:{ref.productSlug ?? "?"}:
                      {ref.field}
                    </TableCell>
                    <TableCell>
                      {ref.name ?? (
                        <span className="text-muted-foreground">
                          producto eliminado
                        </span>
                      )}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {ref.latestValue != null && ref.currency != null ? (
                        formatAmount(ref.currency, ref.latestValue)
                      ) : (
                        <span className="text-muted-foreground">sin dato</span>
                      )}
                    </TableCell>
                    <TableCell className="text-right text-sm text-muted-foreground">
                      {ref.balanceAsOf ? formatDateEs(ref.balanceAsOf) : "-"}
                    </TableCell>
                    <TableCell>
                      {ref.broken && (
                        <Badge variant="destructive">referencia rota</Badge>
                      )}
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
