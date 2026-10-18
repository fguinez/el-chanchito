// A stock product's daily closing prices (product_prices, V038), shared by the
// product API and the product page's "Historial de precio" card. No React, so
// it can be unit tested.

import type { ProductAttributes } from "@chanchito/product-model";

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
