// Shared pieces of the Monitores pages: typed mirrors of the /api/monitors
// response shapes plus the status badge and small formatting helpers used by
// the list, detail, and builder views.

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

export type MonitorStatus = "ok" | "warning" | "breached" | "no_data";
export type ThresholdSeverity = "alert" | "warning";
export type Comparator = "<" | "<=" | ">" | ">=" | "=" | "!=";

export interface ThresholdEvaluation {
  severity: ThresholdSeverity;
  comparator: Comparator;
  value: number | null;
  margin: number | null;
}

export interface MonitorEvaluation {
  status: MonitorStatus;
  value: number | null;
  thresholds: ThresholdEvaluation[];
  /** Min across thresholds (nearest to crossing); null for =/!= or no_data. */
  margin: number | null;
  /** Oldest balanceAsOf among referenced products, ISO string. */
  staleAsOf: string | null;
  noDataReason: string | null;
}

export interface ApiThreshold {
  severity: ThresholdSeverity;
  comparator: Comparator;
  expression: string;
  displayExpression: string;
}

export interface MonitorDisplay {
  chart: "line" | "stat";
  show_margin: boolean;
}

export interface SparklinePoint {
  date: string;
  value: number | null;
  alertThreshold: number | null;
}

export interface ApiMonitor {
  id: string;
  name: string;
  description: string | null;
  currency: string;
  expression: string;
  displayExpression: string;
  thresholds: ApiThreshold[];
  display: MonitorDisplay;
  isActive: boolean;
  evaluation: MonitorEvaluation;
  sparkline?: SparklinePoint[];
}

export interface HistoryPoint {
  date: string;
  value: number | null;
  status: MonitorStatus;
  margin: number | null;
  thresholds: ThresholdEvaluation[];
}

export interface MonitorReference {
  productId: string;
  institutionSlug: string | null;
  productSlug: string | null;
  field: string;
  name: string | null;
  currency: string | null;
  latestValue: number | null;
  balanceAsOf: string | null;
  broken: boolean;
}

export const STATUS_LABELS: Record<MonitorStatus, string> = {
  ok: "OK",
  warning: "Advertencia",
  breached: "Alerta",
  no_data: "Sin datos",
};

export const SEVERITY_LABELS: Record<ThresholdSeverity, string> = {
  alert: "alerta",
  warning: "advertencia",
};

/** Colored status pill shared by the list, detail, and preview views. */
export function StatusBadge({
  status,
  className,
}: {
  status: MonitorStatus;
  className?: string;
}) {
  if (status === "breached") {
    return (
      <Badge variant="destructive" className={className}>
        {STATUS_LABELS.breached}
      </Badge>
    );
  }
  if (status === "warning") {
    return (
      <Badge
        variant="secondary"
        className={cn(
          "bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-200",
          className
        )}
      >
        {STATUS_LABELS.warning}
      </Badge>
    );
  }
  if (status === "no_data") {
    return (
      <Badge variant="outline" className={cn("text-muted-foreground", className)}>
        {STATUS_LABELS.no_data}
      </Badge>
    );
  }
  return (
    <Badge
      variant="outline"
      className={cn("border-green-200 text-green-700", className)}
    >
      {STATUS_LABELS.ok}
    </Badge>
  );
}

/** es-CL calendar date from an ISO timestamp (carries its own timezone). */
export function formatDateEs(iso: string): string {
  return new Date(iso).toLocaleDateString("es-CL");
}

/** Short es-CL label for a plain YYYY-MM-DD date; parsed as local time so
 *  the label never shifts a day across timezones. */
export function formatDayEs(date: string): string {
  const [year, month, day] = date.split("-").map(Number);
  return new Date(year, month - 1, day).toLocaleDateString("es-CL", {
    day: "2-digit",
    month: "short",
  });
}

/** Compact axis labels for large amounts (matches history/page.tsx). */
export function formatAxisValue(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (abs >= 1_000) return `${(value / 1_000).toFixed(0)}K`;
  return value.toString();
}

/** True when an expression (display or stored uuid form) references a
 *  product metric; used to warn about exact comparison on floats. */
export function referencesProduct(source: string): boolean {
  return /@\{|[a-z0-9][a-z0-9_-]*:[a-z0-9][a-z0-9_-]*:[a-z][a-z0-9_]*/.test(
    source
  );
}
