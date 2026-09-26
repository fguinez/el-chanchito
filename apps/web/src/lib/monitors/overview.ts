// What Inicio surfaces from the monitor list: the ones needing attention, the
// ones that can't be computed, and how many OK ones rest on old data. Pure
// and dependency-free so client components can import it directly.

import type { MonitorStatus } from "./types";

/** The slowest scrapers (BanChile, BCI Lider) run daily (see _SCHEDULES in
 *  apps/scrapers/main.py), so data older than two days means a missed run. */
export const STALE_AFTER_HOURS = 48;

const STALE_AFTER_MS = STALE_AFTER_HOURS * 60 * 60 * 1000;

/** Whether a monitor's oldest observation is older than STALE_AFTER_HOURS
 *  before `now`; a monitor with no observation (null) is never stale. */
export function isStale(staleAsOf: string | null, now: Date): boolean {
  if (staleAsOf == null) return false;
  return now.getTime() - new Date(staleAsOf).getTime() > STALE_AFTER_MS;
}

type OverviewMonitor = {
  isActive: boolean;
  evaluation: { status: MonitorStatus; staleAsOf: string | null };
};

/** Split the active monitors for Inicio, keeping input order: breached and
 *  warning in `attention`, no_data in `noData`, stale ok ones in `staleOk`. */
export function groupForInicio<M extends OverviewMonitor>(
  monitors: M[],
  now: Date
): { attention: M[]; noData: M[]; staleOk: M[] } {
  const attention: M[] = [];
  const noData: M[] = [];
  const staleOk: M[] = [];
  for (const monitor of monitors) {
    if (!monitor.isActive) continue;
    const { status, staleAsOf } = monitor.evaluation;
    if (status === "breached" || status === "warning") attention.push(monitor);
    else if (status === "no_data") noData.push(monitor);
    else if (isStale(staleAsOf, now)) staleOk.push(monitor);
  }
  return { attention, noData, staleOk };
}
