"use client";

// Refresh + polling for the Instituciones pages: trigger a scrape for one
// institution (or all) via POST /api/institutions/refresh, then poll
// GET /api/scrapers until each triggered run finishes, calling the page's
// `reload` as each one lands. Also fetches which institutions have a live
// scraper (GET /api/scrapers/available) so the pages can disable the
// per-institution buttons for everything else.

import { useCallback, useEffect, useState } from "react";
import {
  fetchRunMap,
  POLL_INTERVAL_MS,
  POLL_TIMEOUT_MS,
  sleep,
  type ScraperRun,
} from "./shared";

export interface UseInstitutionRefresh {
  /** Slugs with a scrape currently in flight (spinning buttons). */
  syncing: Set<string>;
  /** Slugs with a live scraper the per-institution buttons can trigger. Empty
   *  while loading and when the scraper service is unknown/unreachable, which
   *  disables the buttons: a refresh would fail anyway. */
  scrapers: Set<string>;
  /** Set when the scraper service is unreachable / not configured (proxy 503). */
  serviceError: string | null;
  /** Trigger a scrape for one institution (by slug) or all when omitted. */
  refresh: (slug?: string) => Promise<void>;
}

/**
 * Poll `/api/scrapers` until each triggered institution's run finishes (a
 * *new* run appears — started_at past its baseline — and leaves `running`),
 * reloading balances as each one lands. Caps at POLL_TIMEOUT_MS so a slow or
 * unconfigured scraper can't spin forever.
 */
async function pollUntilDone(
  pending: Set<string>,
  baseline: Map<string, ScraperRun>,
  markDone: (slug: string) => void,
  reload: () => void | Promise<void>
) {
  const deadline = Date.now() + POLL_TIMEOUT_MS;
  while (pending.size > 0 && Date.now() < deadline) {
    await sleep(POLL_INTERVAL_MS);
    const runs = await fetchRunMap();
    let anyDone = false;
    for (const slug of [...pending]) {
      const run = runs.get(slug);
      if (!run) continue;
      const base = baseline.get(slug);
      const isNewRun = !base || run.started_at > base.started_at;
      if (isNewRun && run.status !== "running") {
        pending.delete(slug);
        markDone(slug);
        anyDone = true;
      }
    }
    if (anyDone) await reload();
  }
  // Timed out with runs still pending: stop spinning and show latest data.
  if (pending.size > 0) {
    for (const slug of pending) markDone(slug);
    await reload();
  }
}

export function useInstitutionRefresh(
  reload: () => void | Promise<void>
): UseInstitutionRefresh {
  // Institution slugs with a scrape currently in flight (spinning buttons).
  const [syncing, setSyncing] = useState<Set<string>>(new Set());
  // Slugs with a live scraper (empty until loaded / when the service is down).
  const [scrapers, setScrapers] = useState<Set<string>>(new Set());
  // Set when the scraper service is unreachable / not configured (proxy 503).
  const [serviceError, setServiceError] = useState<string | null>(null);

  // Which scrapers exist is decided by the backend's env at startup
  // (build_scrapers()), so ask it once instead of hardcoding the list. The
  // proxy already degrades to an empty list on any failure.
  useEffect(() => {
    let cancelled = false;
    fetch("/api/scrapers/available")
      .then((res) => res.json())
      .then((data) => {
        if (!cancelled && Array.isArray(data?.scrapers)) {
          setScrapers(new Set<string>(data.scrapers));
        }
      })
      .catch(() => {
        /* keep the empty set */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const markDone = useCallback((slug: string) => {
    setSyncing((prev) => {
      const next = new Set(prev);
      next.delete(slug);
      return next;
    });
  }, []);

  /** Trigger a scrape for one institution (by slug) or all when omitted. */
  const refresh = useCallback(
    async (slug?: string) => {
      setServiceError(null);
      // Snapshot current runs first so polling can tell the new run apart.
      const baseline = await fetchRunMap();

      let triggered: string[];
      try {
        const res = await fetch("/api/institutions/refresh", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(slug ? { institution: slug } : {}),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          setServiceError(
            data.error ??
              (res.status === 503
                ? "Servicio de scrapers no disponible."
                : "No se pudo iniciar la sincronización.")
          );
          return;
        }
        triggered =
          Array.isArray(data.triggered) && data.triggered.length > 0
            ? data.triggered
            : slug
              ? [slug]
              : [];
      } catch {
        setServiceError("Servicio de scrapers no disponible.");
        return;
      }

      if (triggered.length === 0) return;
      setSyncing((prev) => new Set([...prev, ...triggered]));
      void pollUntilDone(
        new Set(triggered),
        baseline,
        markDone,
        reload
      );
    },
    [markDone, reload]
  );

  return { syncing, scrapers, serviceError, refresh };
}
