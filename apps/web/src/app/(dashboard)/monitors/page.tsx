"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { Plus } from "lucide-react";
import { Line, LineChart, ResponsiveContainer } from "recharts";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn, formatAmount } from "@/lib/utils";
import {
  SEVERITY_LABELS,
  StatusBadge,
  formatDateEs,
  type ApiMonitor,
  type MonitorEvaluation,
  type ThresholdEvaluation,
} from "@/components/monitors/shared";

/** The threshold the value is closest to crossing today (smallest margin);
 *  falls back to the alert threshold when margins are unavailable (=/!=). */
function nearestThreshold(
  evaluation: MonitorEvaluation
): ThresholdEvaluation | null {
  const withValue = evaluation.thresholds.filter((t) => t.value != null);
  if (withValue.length === 0) return null;
  const withMargin = withValue.filter((t) => t.margin != null);
  if (withMargin.length > 0) {
    return withMargin.reduce((a, b) => (a.margin! <= b.margin! ? a : b));
  }
  return withValue.find((t) => t.severity === "alert") ?? withValue[0];
}

function Sparkline({ monitor }: { monitor: ApiMonitor }) {
  const data = monitor.sparkline;
  if (
    monitor.display.chart !== "line" ||
    !data ||
    !data.some((p) => p.value != null)
  ) {
    return null;
  }
  return (
    <div className="h-10 w-40 shrink-0">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data}>
          <Line
            type="monotone"
            dataKey="value"
            stroke="#2563eb"
            strokeWidth={1.5}
            dot={false}
            isAnimationActive={false}
          />
          <Line
            type="monotone"
            dataKey="alertThreshold"
            stroke="#dc2626"
            strokeWidth={1}
            strokeDasharray="4 3"
            dot={false}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function MonitorCard({ monitor }: { monitor: ApiMonitor }) {
  const { evaluation } = monitor;
  const nearest = nearestThreshold(evaluation);
  const showMargin =
    monitor.display.show_margin && evaluation.margin != null;

  return (
    <Link href={`/monitors/${monitor.id}`} className="block">
      <Card
        className={cn(
          "transition-colors hover:border-ring/60",
          !monitor.isActive && "opacity-60"
        )}
      >
        <CardContent className="flex items-center justify-between gap-4">
          <div className="min-w-0 space-y-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-semibold">{monitor.name}</span>
              <StatusBadge status={evaluation.status} />
              {!monitor.isActive && <Badge variant="outline">Inactivo</Badge>}
            </div>
            {monitor.description && (
              <p className="text-xs text-muted-foreground">
                {monitor.description}
              </p>
            )}
            <div className="text-2xl font-bold tabular-nums">
              {evaluation.value != null ? (
                formatAmount(monitor.currency, evaluation.value)
              ) : (
                <span className="text-muted-foreground">sin dato</span>
              )}
            </div>
            {nearest && nearest.value != null && (
              <p className="text-sm text-muted-foreground">
                Umbral de {SEVERITY_LABELS[nearest.severity]} hoy:{" "}
                <span className="tabular-nums">
                  {formatAmount(monitor.currency, nearest.value)}
                </span>
              </p>
            )}
            {showMargin && (
              <p
                className={cn(
                  "text-sm font-medium tabular-nums",
                  evaluation.status === "breached" && "text-red-600",
                  evaluation.status === "warning" && "text-amber-600",
                  evaluation.status === "ok" && "text-green-700"
                )}
              >
                Margen: {formatAmount(monitor.currency, evaluation.margin!)}
              </p>
            )}
            {evaluation.status === "no_data" && evaluation.noDataReason && (
              <p className="text-xs text-muted-foreground">
                {evaluation.noDataReason}
              </p>
            )}
            {evaluation.staleAsOf && (
              <p className="text-xs text-muted-foreground">
                al {formatDateEs(evaluation.staleAsOf)}
              </p>
            )}
          </div>
          <Sparkline monitor={monitor} />
        </CardContent>
      </Card>
    </Link>
  );
}

export default function MonitorsPage() {
  const [monitorList, setMonitorList] = useState<ApiMonitor[] | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    fetch("/api/monitors")
      .then((res) => {
        if (!res.ok) throw new Error("failed");
        return res.json();
      })
      .then((data: { monitors: ApiMonitor[] }) => setMonitorList(data.monitors))
      .catch(() => setError(true));
  }, []);

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="text-2xl font-bold">Monitores</h2>
          <p className="text-sm text-muted-foreground">
            Ecuaciones sobre tus productos, con umbrales de alerta y
            advertencia.
          </p>
        </div>
        <Button asChild size="sm">
          <Link href="/monitors/new">
            <Plus className="h-4 w-4" />
            Nuevo monitor
          </Link>
        </Button>
      </div>

      {error ? (
        <p className="text-muted-foreground">
          No se pudieron cargar los monitores.
        </p>
      ) : !monitorList ? (
        <p className="text-muted-foreground">Cargando...</p>
      ) : monitorList.length === 0 ? (
        <Card>
          <CardContent className="space-y-4 text-center">
            <p className="text-muted-foreground">
              Todavía no hay monitores. Crea el primero para vigilar saldos,
              deudas y umbrales de tus productos.
            </p>
            <Button asChild>
              <Link href="/monitors/new">Nuevo monitor</Link>
            </Button>
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-3">
          {monitorList.map((monitor) => (
            <MonitorCard key={monitor.id} monitor={monitor} />
          ))}
        </div>
      )}
    </div>
  );
}
