"use client";

import Link from "next/link";
import { Line, LineChart, ResponsiveContainer } from "recharts";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
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
export function nearestThreshold(
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

export function Sparkline({ monitor }: { monitor: ApiMonitor }) {
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

export function MonitorCard({ monitor }: { monitor: ApiMonitor }) {
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
