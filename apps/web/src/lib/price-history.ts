// A stock product's daily closing prices (product_prices, V038), shared by the
// product API and the product page's "Historial de precio" card. Plain
// functions, no components, so they can be unit tested.

import type { ProductAttributes, ProductMetrics } from "@chanchito/product-model";
import { dayStartMs } from "@/lib/utils";

/** One daily close, as the product API returns it. */
export interface ProductPricePoint {
  /** The trading day, plain YYYY-MM-DD. */
  date: string;
  /** Closing price per unit, in the product's currency. */
  price: number;
}

/** The product's ticker (a stock position's symbol), or null for products
 *  that have none, which are the ones without a price history. */
export function productTicker(
  attributes: ProductAttributes | Record<string, never> | null | undefined
): string | null {
  if (!attributes || !("ticker" in attributes)) return null;
  const ticker = attributes.ticker;
  return typeof ticker === "string" && ticker.trim() !== "" ? ticker.trim() : null;
}

/** Whether a stock product shows its "Historial de precio" card: once it has
 *  closes, or while it holds units (the first closes come with the next
 *  refresh). A stock sold out before any close was stored never gets one,
 *  so a card there would promise a history that never loads. */
export function showsPriceHistory(
  prices: ProductPricePoint[],
  metrics: ProductMetrics | null | undefined
): boolean {
  if (prices.length > 0) return true;
  const units = metrics?.kind === "stock" ? metrics.units : null;
  return units != null && units > 0;
}

/** A chart row: `t` is the trading day's local midnight (epoch ms), the time
 *  axis convention, so a close sits on its calendar day in any timezone. */
export interface PriceRow {
  t: number;
  /** The trading day, plain YYYY-MM-DD. */
  date: string;
  price: number;
}

/** Chart rows from the API's closes, keeping their (oldest first) order. */
export function buildPriceRows(prices: ProductPricePoint[]): PriceRow[] {
  return prices.map((p) => ({ t: dayStartMs(p.date), date: p.date, price: p.price }));
}

/** How the price moved across a time window. */
export interface PriceChange {
  /** The first and last close inside the window. */
  from: PriceRow;
  to: PriceRow;
  /** `to.price - from.price`, in the product's currency. */
  change: number;
  /** `change` as a percent number of `from.price`. */
  pct: number;
}

/** The move between the first and last close inside a [min, max] ms window
 *  (every row when `bounds` is null, i.e. "Todo"), over rows sorted oldest
 *  first; null with fewer than two closes in it. */
export function rangeChange(
  rows: PriceRow[],
  bounds: [number, number] | null
): PriceChange | null {
  const inside = bounds
    ? rows.filter((r) => r.t >= bounds[0] && r.t <= bounds[1])
    : rows;
  if (inside.length < 2) return null;
  const from = inside[0];
  const to = inside[inside.length - 1];
  const change = to.price - from.price;
  return { from, to, change, pct: (change / from.price) * 100 };
}
