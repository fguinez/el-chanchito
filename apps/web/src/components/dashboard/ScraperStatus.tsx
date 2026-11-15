"use client";

import { useCallback, useEffect, useState, type ReactNode } from "react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { InstitutionLogo } from "@/components/institutions/institution-logo";
import { scraperRunLabel } from "@/lib/scraper-run-label";
import { cn } from "@/lib/utils";
import {
  hasRunDetails,
  runStatusStyle,
  truncateRunMessage,
} from "@/lib/scraper-runs";
import {
  parseSupportedSignIns,
  pruneSignInWatches,
  type SignInMethod,
} from "@/lib/scraper-login";
import { SignInAction } from "@/components/dashboard/sign-in-action";
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
  needsSignIn: {
    box: "border-violet-200 bg-violet-50 dark:border-violet-900 dark:bg-violet-950",
    icon: "text-violet-600",
    title: "text-violet-800 dark:text-violet-200",
    body: "text-violet-700 dark:text-violet-300",
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
  action,
}: {
  tone: keyof typeof BANNER_TONES;
  title: string;
  runs: ScraperRun[];
  fallback: string;
  action?: (run: ScraperRun) => ReactNode;
}) {
  const t = BANNER_TONES[tone];
  return (
    <div className={cn("flex items-start gap-3 rounded-md border p-3", t.box)}>
      <AlertTriangle className={cn("mt-0.5 h-4 w-4", t.icon)} />
      <div className="flex-1 text-sm">
        <p className={cn("font-medium", t.title)}>{title}</p>
        {runs.map((r) => (
          <div key={runKey(r)} className={cn("mt-1", t.body)}>
            <p>
              {runLabel(r)}:{" "}
              {r.error_message ? truncateRunMessage(r.error_message) : fallback}
            </p>
            {action?.(r)}
          </div>
        ))}
      </div>
    </div>
  );
}

// After a sign-in goes through, the runs are re-read this often until the
// institution's refresh has finished, for at most WATCH_MAX_MS.
const WATCH_POLL_MS = 3000;
const WATCH_MAX_MS = 10 * 60 * 1000;

export function ScraperStatus() {
  const [runs, setRuns] = useState<ScraperRun[]>([]);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [signIns, setSignIns] = useState<Record<string, SignInMethod>>({});
  // Institution -> when its sign-in went through (ms since epoch).
  const [watching, setWatching] = useState<Record<string, number>>({});

  const loadRuns = useCallback(async (): Promise<ScraperRun[] | null> => {
    try {
      const res = await fetch("/api/scrapers");
      const data: ScraperRun[] = await res.json();
      setRuns(data);
      return data;
    } catch {
      return null;
    }
  }, []);

  useEffect(() => {
    fetch("/api/scrapers")
      .then((res) => res.json())
      .then(setRuns)
      .catch(() => {});
    fetch("/api/scrapers/login")
      .then((res) => res.json())
      .then((data) =>
        setSignIns(
          Object.fromEntries(
            parseSupportedSignIns(data).map((s) => [s.institution, s.method])
          )
        )
      )
      .catch(() => {});
  }, [loadRuns]);

  const watched = Object.keys(watching).length > 0;
  useEffect(() => {
    if (!watched) return;
    const timer = setInterval(async () => {
      const latest = await loadRuns();
      if (latest) {
        setWatching((current) =>
          pruneSignInWatches(current, latest, Date.now(), WATCH_MAX_MS)
        );
      }
    }, WATCH_POLL_MS);
    return () => clearInterval(timer);
  }, [watched, loadRuns]);

  const onSignedIn = useCallback(
    (institution: string) => {
      // A small margin: the run row's started_at comes from the service's clock.
      setWatching((current) => ({ ...current, [institution]: Date.now() - 5000 }));
      loadRuns();
    },
    [loadRuns]
  );

  if (runs.length === 0) {
    return null;
  }

  const errors = runs.filter((r) => r.status === "error");
  // A partial run is no outage, but its numbers may be partly stale.
  const partials = runs.filter((r) => r.status === "partial");
  // Nothing is broken: the session is gone and only a sign-in brings it back.
  const signInRuns = runs.filter((r) => r.status === "needs_sign_in");

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
      {signInRuns.length > 0 && (
        <RunBanner
          tone="needsSignIn"
          title={
            signInRuns.length === 1
              ? `${runLabel(signInRuns[0])} necesita que inicies sesión`
              : `${signInRuns.length} scrapers necesitan que inicies sesión`
          }
          runs={signInRuns}
          fallback="Sesión perdida"
          action={(r) =>
            signIns[r.institution] ? (
              // Keyed by run, so a new needs_sign_in run starts it afresh.
              <SignInAction
                key={`${runKey(r)}:${r.started_at}`}
                institution={r.institution}
                method={signIns[r.institution]}
                onSucceeded={onSignedIn}
              />
            ) : null
          }
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
                    <span className="flex items-center gap-2">
                      <InstitutionLogo slug={run.institution} size="sm" />
                      {runLabel(run)}
                    </span>
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
