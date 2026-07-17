// Shared net-worth math so /api/wealth and /api/institutions agree on how each
// product contributes to patrimonio and deuda — and, crucially, convert to CLP
// before summing (mixing native crypto units into a CLP total was a real bug).

import {
  KIND_INFO,
  type ProductKind,
  type ProductMetrics,
} from "./db/schema";
import { toClp, type ClpRates } from "./rates";

/**
 * Amount owed on a product, in its own currency, derived from its kind's role
 * in the registry plus its latest (or per-snapshot) metrics:
 *   1. not a liability -> 0
 *   2. reported owed (the bank's Utilizado) when present
 *   3. else limit − available when the same observation carries both
 *   4. else abs(balance) only when the kind's convention stores the owed amount
 *   5. else 0 — a card with no metrics contributes no debt (never guess)
 *
 * Metrics come from JSONB, so every field is runtime-checked with typeof
 * (the `in` guards double as type narrowing over the discriminated union).
 */
export function owedInCurrency(
  kind: ProductKind,
  balance: number,
  metrics: ProductMetrics | null
): number {
  if (KIND_INFO[kind].role !== "liability") return 0;

  if (metrics && "owed" in metrics && typeof metrics.owed === "number") {
    return Math.max(metrics.owed, 0);
  }

  if (
    metrics &&
    "limit" in metrics &&
    typeof metrics.limit === "number" &&
    "available" in metrics &&
    typeof metrics.available === "number"
  ) {
    return Math.max(metrics.limit - metrics.available, 0);
  }

  if (KIND_INFO[kind].balanceConvention === "owed") return Math.abs(balance);
  return 0;
}

/** A product's asset value in CLP (0 if it isn't an asset). Null when the
 *  currency has no known rate, so the caller can flag the gap. */
export function assetClp(
  kind: ProductKind,
  balance: number,
  currency: string,
  rates: ClpRates
): number | null {
  if (KIND_INFO[kind].role !== "asset") return 0;
  return toClp(currency, balance, rates);
}

/** A product's debt in CLP (0 if it owes nothing). Owed amounts and card limits
 *  are denominated in CLP in practice, so we convert from the product currency
 *  for completeness. Null when the currency has no known rate. */
export function debtClp(
  kind: ProductKind,
  balance: number,
  metrics: ProductMetrics | null,
  currency: string,
  rates: ClpRates
): number | null {
  const owed = owedInCurrency(kind, balance, metrics);
  if (owed === 0) return 0;
  return toClp(currency, owed, rates);
}
