"use client";

import { useEffect, useState } from "react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { scraperRunLabel } from "@/lib/scraper-run-label";
import { cn } from "@/lib/utils";
import {
  hasRunDetails,
  runStatusStyle,
  truncateRunMessage,
} from "@/lib/scraper-runs";
import { AlertTriangle } from "lucide-react";

interface ScraperRun {
  method: string;
  institution: string;
  institution_name: string | null;
  started_at: string;
  finished_at: string | null;
  status: string;
  transactions_imported: number;
  error_message: string | null;
}

const runKey = (r: ScraperRun) => `${r.method}_${r.institution}`;
const runLabel = (r: ScraperRun) => scraperRunLabel(r);

function timeAgo(dateStr: string): string {
  const diff = Date.now() - new Date(dateStr).getTime();
  const minutes = Math.floor(diff / 60000);
  if (minutes < 1) return "hace menos de 1 min";
  if (minutes < 60) return `hace ${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `hace ${hours}h`;
  const days = Math.floor(hours / 24);
  return `hace ${days}d`;
}

const BANNER_TONES = {
  error: {
    box: "border-red-200 bg-red-50 dark:border-red-900 dark:bg-red-950",
    icon: "text-red-600",
    title: "text-red-800 dark:text-red-200",
    body: "text-red-600 dark:text-red-400",
  },
  partial: {
    box: "border-amber-200 bg-amber-50 dark:border-amber-900 dark:bg-amber-950",
    icon: "text-amber-600",
    title: "text-amber-800 dark:text-amber-200",
    body: "text-amber-700 dark:text-amber-300",
  },
};

function RunBanner({
  tone,
  title,
  runs,
  fallback,
}: {
  tone: keyof typeof BANNER_TONES;
  title: string;
  runs: ScraperRun[];
  fallback: string;
}) {
  const t = BANNER_TONES[tone];
  return (
    <div className={cn("flex items-start gap-3 rounded-md border p-3", t.box)}>
      <AlertTriangle className={cn("mt-0.5 h-4 w-4", t.icon)} />
      <div className="flex-1 text-sm">
        <p className={cn("font-medium", t.title)}>{title}</p>
        {runs.map((r) => (
          <p key={runKey(r)} className={cn("mt-1", t.body)}>
            {runLabel(r)}:{" "}
            {r.error_message ? truncateRunMessage(r.error_message) : fallback}
          </p>
        ))}
      </div>
    </div>
  );
}

export function ScraperStatus() {
  const [runs, setRuns] = useState<ScraperRun[]>([]);
  const [expanded, setExpanded] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/scrapers")
      .then((res) => res.json())
      .then(setRuns)
      .catch(() => {});
  }, []);

  if (runs.length === 0) {
    return null;
  }

  const errors = runs.filter((r) => r.status === "error");
  // Part of the run's data landed and part failed (or the scraper warned about
  // incomplete coverage): not an outage, but the numbers may be partly stale.
  const partials = runs.filter((r) => r.status === "partial");

  return (
    <div className="space-y-3">
      {errors.length > 0 && (
        <RunBanner
          tone="error"
          title={
            errors.length === 1
              ? `El scraper ${runLabel(errors[0])} tiene un error`
              : `${errors.length} scrapers con errores`
          }
          runs={errors}
          fallback="Error desconocido"
        />
      )}
      {partials.length > 0 && (
        <RunBanner
          tone="partial"
          title={
            partials.length === 1
              ? `El scraper ${runLabel(partials[0])} terminó con datos incompletos`
              : `${partials.length} scrapers terminaron con datos incompletos`
          }
          runs={partials}
          fallback="Sin detalles"
        />
      )}

      {/* Status list */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Estado de scrapers</CardTitle>
          <CardDescription>Ultima sincronizacion por cuenta</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="space-y-2">
            {runs.map((run) => {
              const key = runKey(run);
              const expandable = hasRunDetails(run);
              const status = runStatusStyle(run.status);
              return (
                <div key={key}>
                  <div
                    className={cn(
                      "flex items-center justify-between text-sm",
                      expandable && "cursor-pointer"
                    )}
                    onClick={() => {
                      if (expandable) {
                        setExpanded(expanded === key ? null : key);
                      }
                    }}
                  >
                    <span>{runLabel(run)}</span>
                    <div className="flex items-center gap-2">
                      {run.transactions_imported > 0 && (
                        <span className="text-xs text-muted-foreground">
                          +{run.transactions_imported} txn
                        </span>
                      )}
                      <Badge
                        variant="outline"
                        className={cn("text-xs", status.badgeClass)}
                      >
                        {status.label}
                      </Badge>
                      <span className="text-muted-foreground">
                        {timeAgo(run.finished_at ?? run.started_at)}
                      </span>
                    </div>
                  </div>
                  {expanded === key && run.error_message && (
                    <p className="mt-1 rounded bg-muted p-2 text-xs text-muted-foreground">
                      {run.error_message}
                    </p>
                  )}
                </div>
              );
            })}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
