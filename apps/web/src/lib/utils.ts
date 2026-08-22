import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatCLP(amount: number): string {
  return new Intl.NumberFormat("es-CL", {
    style: "currency",
    currency: "CLP",
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  }).format(amount);
}

/** Format an amount in an arbitrary currency: CLP as pesos, anything else
 *  (crypto, foreign) as a trimmed decimal followed by its currency code. */
export function formatAmount(currency: string, amount: number): string {
  if (currency === "CLP") return formatCLP(amount);
  const formatted = new Intl.NumberFormat("en-US", {
    maximumFractionDigits: 8,
  }).format(amount);
  return `${formatted} ${currency}`;
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

/** Compact es-CL label for a time-axis tick; the year appears only when it
 *  isn't the current one. */
export function formatDateTickMs(ms: number): string {
  const date = new Date(ms);
  const options: Intl.DateTimeFormatOptions = { day: "2-digit", month: "short" };
  if (date.getFullYear() !== new Date().getFullYear()) options.year = "2-digit";
  return date.toLocaleDateString("es-CL", options);
}

/** Full es-CL date for tooltips and badges over a numeric time axis. */
export function formatDateMs(ms: number): string {
  return new Date(ms).toLocaleDateString("es-CL");
}

/** Compact axis labels for large amounts (matches history/page.tsx). */
export function formatAxisValue(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (abs >= 1_000) return `${(value / 1_000).toFixed(0)}K`;
  if (abs < 1e-6) return "0";
  // 3 significant digits: keeps small values exact enough while hiding the
  // float noise interactive domains produce (e.g. 250.0000001).
  return Number(value.toPrecision(3)).toString();
}
