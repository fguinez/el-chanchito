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
  scraper_name: string;
  started_at: string;
  finished_at: string | null;
  status: string;
  transactions_imported: number;
  error_message: string | null;
}

const SCRAPER_LABELS: Record<string, string> = {
  fintual_api: "Fintual",
  buda_api: "Buda",
  fintself_banchile: "Banco de Chile",
  email_parser: "Email (MP/MACH/Tenpo)",
  bci_lider: "BCI Lider",
};

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
                ? `El scraper ${SCRAPER_LABELS[errors[0].scraper_name] ?? errors[0].scraper_name} tiene un error`
                : `${errors.length} scrapers con errores`}
            </p>
            {errors.map((e) => (
              <p
                key={e.scraper_name}
                className="mt-1 text-red-600 dark:text-red-400"
              >
                {SCRAPER_LABELS[e.scraper_name] ?? e.scraper_name}:{" "}
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
            {runs.map((run) => (
              <div key={run.scraper_name}>
                <div
                  className={cn(
                    "flex items-center justify-between text-sm",
                    run.status === "error" && "cursor-pointer"
                  )}
                  onClick={() => {
                    if (run.status === "error") {
                      setExpandedError(
                        expandedError === run.scraper_name
                          ? null
                          : run.scraper_name
                      );
                    }
                  }}
                >
                  <span>
                    {SCRAPER_LABELS[run.scraper_name] ?? run.scraper_name}
                  </span>
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
                {expandedError === run.scraper_name && run.error_message && (
                  <p className="mt-1 rounded bg-muted p-2 text-xs text-muted-foreground">
                    {run.error_message}
                  </p>
                )}
              </div>
            ))}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
