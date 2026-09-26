// Plain calendar-day helpers shared by evaluation, history replay, and request
// validation. Days are YYYY-MM-DD strings read from LOCAL date parts, the same
// unit as DAY_OF_MONTH() and the replay window.

const DAY_RE = /^\d{4}-\d{2}-\d{2}$/;

/** YYYY-MM-DD from a Date's local parts (the replay window's day unit). */
export function formatLocalDate(date: Date): string {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

/** True for a well-formed YYYY-MM-DD string naming a real calendar day
 *  (rejects e.g. 2026-02-31, which Date.UTC would silently roll over). */
export function isValidDay(dateStr: string): boolean {
  if (!DAY_RE.test(dateStr)) return false;
  const [year, month, day] = dateStr.split("-").map(Number);
  const date = new Date(Date.UTC(year, month - 1, day));
  return (
    date.getUTCFullYear() === year &&
    date.getUTCMonth() === month - 1 &&
    date.getUTCDate() === day
  );
}
