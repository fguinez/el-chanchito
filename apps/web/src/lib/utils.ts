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
