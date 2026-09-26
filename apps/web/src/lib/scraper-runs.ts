// How the dashboard presents a scraper run. The statuses are the ones
// `run_scraper` writes to scraper_runs (apps/scrapers/main.py): `partial`
// means part of the run's data landed and part failed, or the scraper warned
// about incomplete coverage; its error_message says which.

export type ScraperRunStatus = "running" | "success" | "partial" | "error";

export interface RunStatusStyle {
  /** Badge text, in Spanish like the rest of the dashboard. */
  label: string;
  /** Tailwind classes for the outline badge. */
  badgeClass: string;
}

const STATUS_STYLES: Record<ScraperRunStatus, RunStatusStyle> = {
  success: { label: "ok", badgeClass: "text-green-600 border-green-200" },
  partial: { label: "parcial", badgeClass: "text-amber-600 border-amber-200" },
  error: { label: "error", badgeClass: "text-red-600 border-red-200" },
  running: { label: "en curso", badgeClass: "text-blue-600 border-blue-200" },
};

/** The badge for a run's status; an unknown status shows as-is, unstyled. */
export function runStatusStyle(status: string): RunStatusStyle {
  return Object.hasOwn(STATUS_STYLES, status)
    ? STATUS_STYLES[status as ScraperRunStatus]
    : { label: status, badgeClass: "" };
}

/** True when a run's row can expand to show its error_message: failed and
 *  partial runs both carry their details there. */
export function hasRunDetails(run: {
  status: string;
  error_message: string | null;
}): boolean {
  return (
    (run.status === "error" || run.status === "partial") &&
    Boolean(run.error_message)
  );
}

/** A run message cut to `max` characters for the banners. */
export function truncateRunMessage(message: string, max = 120): string {
  return message.length > max ? message.slice(0, max) + "..." : message;
}
