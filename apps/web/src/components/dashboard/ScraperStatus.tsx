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
import { cn } from "@/lib/utils";
import { AlertTriangle } from "lucide-react";

interface ScraperRun {
  method: string;
  institution: string;
  started_at: string;
  finished_at: string | null;
  status: string;
  transactions_imported: number;
  error_message: string | null;
}

const INSTITUTION_LABELS: Record<string, string> = {
  fintual: "Fintual",
  buda: "Buda",
  banchile: "Banco de Chile",
  mach: "MACH",
  mercadopago: "MercadoPago",
  tenpo: "Tenpo",
  bci_lider: "BCI Lider",
  _legacy_composite: "Email (legacy)",
};

const runKey = (r: ScraperRun) => `${r.method}_${r.institution}`;
const runLabel = (r: ScraperRun) =>
  INSTITUTION_LABELS[r.institution] ?? r.institution;

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

export function ScraperStatus() {
  const [runs, setRuns] = useState<ScraperRun[]>([]);
  const [expandedError, setExpandedError] = useState<string | null>(null);

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

  return (
    <div className="space-y-3">
      {/* Error banner */}
      {errors.length > 0 && (
        <div className="flex items-start gap-3 rounded-md border border-red-200 bg-red-50 p-3 dark:border-red-900 dark:bg-red-950">
          <AlertTriangle className="mt-0.5 h-4 w-4 text-red-600" />
          <div className="flex-1 text-sm">
            <p className="font-medium text-red-800 dark:text-red-200">
              {errors.length === 1
                ? `El scraper ${runLabel(errors[0])} tiene un error`
                : `${errors.length} scrapers con errores`}
            </p>
            {errors.map((e) => (
              <p
                key={runKey(e)}
                className="mt-1 text-red-600 dark:text-red-400"
              >
                {runLabel(e)}:{" "}
                {e.error_message
                  ? e.error_message.length > 120
                    ? e.error_message.slice(0, 120) + "..."
                    : e.error_message
                  : "Error desconocido"}
              </p>
            ))}
          </div>
        </div>
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
              return (
                <div key={key}>
                  <div
                    className={cn(
                      "flex items-center justify-between text-sm",
                      run.status === "error" && "cursor-pointer"
                    )}
                    onClick={() => {
                      if (run.status === "error") {
                        setExpandedError(
                          expandedError === key ? null : key
                        );
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
                        className={cn(
                          "text-xs",
                          run.status === "success" &&
                            "text-green-600 border-green-200",
                          run.status === "error" &&
                            "text-red-600 border-red-200",
                          run.status === "running" &&
                            "text-blue-600 border-blue-200",
                          run.status === "partial" &&
                            "text-yellow-600 border-yellow-200"
                        )}
                      >
                        {run.status}
                      </Badge>
                      <span className="text-muted-foreground">
                        {timeAgo(run.finished_at ?? run.started_at)}
                      </span>
                    </div>
                  </div>
                  {expandedError === key && run.error_message && (
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
