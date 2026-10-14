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

let isoCurrencies: Set<string> | null = null;

/** Minor-unit digits of an ISO 4217 currency (USD 2, JPY 0), or null for a
 *  code that is not one (BTC, USDC and other crypto tickers). */
function isoFractionDigits(currency: string): number | null {
  isoCurrencies ??= new Set(
    typeof Intl.supportedValuesOf === "function"
      ? Intl.supportedValuesOf("currency")
      : ["USD", "EUR"]
  );
  if (!isoCurrencies.has(currency)) return null;
  return (
    new Intl.NumberFormat("en-US", { style: "currency", currency })
      .resolvedOptions().maximumFractionDigits ?? null
  );
}

/** Format an amount in an arbitrary currency: CLP as pesos, other fiat with
 *  exactly its minor-unit digits (USD always shows its cents), anything else
 *  (crypto) as a trimmed decimal, each followed by its currency code. */
export function formatAmount(currency: string, amount: number): string {
  if (currency === "CLP") return formatCLP(amount);
  const digits = isoFractionDigits(currency);
  const formatted = new Intl.NumberFormat(
    "en-US",
    digits == null
      ? { maximumFractionDigits: 8 }
      : { minimumFractionDigits: digits, maximumFractionDigits: digits }
  ).format(amount);
  return `${formatted} ${currency}`;
}

/** es-CL calendar date from an ISO timestamp (carries its own timezone). */
export function formatDateEs(iso: string): string {
  return new Date(iso).toLocaleDateString("es-CL");
}

/** Short es-CL date and time for an ISO timestamp. */
export function formatTimestampEs(iso: string): string {
  return new Date(iso).toLocaleString("es-CL", {
    dateStyle: "short",
    timeStyle: "short",
  });
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

/** Full es-CL date (day-month-year) for a plain YYYY-MM-DD date; parsed as
 *  local time like `formatDayEs` so the label never shifts a day. */
export function formatPlainDateEs(date: string): string {
  const [year, month, day] = date.split("-").map(Number);
  return new Date(year, month - 1, day).toLocaleDateString("es-CL");
}

/** es-CL label for a raw percent number (4.2 -> "4,2%"), one to two
 *  decimals; `signed` adds an explicit "+" to positive values (+2,1%). */
export function formatPercent(
  value: number,
  { signed = false }: { signed?: boolean } = {}
): string {
  const formatted = new Intl.NumberFormat("es-CL", {
    minimumFractionDigits: 1,
    maximumFractionDigits: 2,
    signDisplay: signed ? "exceptZero" : "auto",
  }).format(value);
  return `${formatted}%`;
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
