// Shared net-worth math so /api/wealth and /api/institutions agree on how each
// product contributes to patrimonio and deuda — and, crucially, convert to CLP
// before summing (mixing native crypto units into a CLP total was a real bug).

import { ASSET_KINDS, LIABILITY_KINDS, type ProductKind } from "./db/schema";
import { toClp, type ClpRates } from "./rates";

/**
 * Amount owed on a product, in its own currency. Credit cards store the
 * *available* cupo (the planning drift formula relies on that), so the debt is
 * `limit − available`; other liabilities store the owed amount directly.
 * Non-liabilities owe nothing.
 */
export function owedInCurrency(
  kind: ProductKind,
  balance: number,
  creditLimit: number | null
): number {
  if (kind === "credit_card") {
    return creditLimit != null ? Math.max(creditLimit - balance, 0) : 0;
  }
  if (LIABILITY_KINDS.includes(kind)) return Math.abs(balance);
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
  if (!ASSET_KINDS.includes(kind)) return 0;
  return toClp(currency, balance, rates);
}

/** A product's debt in CLP (0 if it owes nothing). Owed amounts and card limits
 *  are denominated in CLP in practice, so we convert from the product currency
 *  for completeness. Null when the currency has no known rate. */
export function debtClp(
  kind: ProductKind,
  balance: number,
  creditLimit: number | null,
  currency: string,
  rates: ClpRates
): number | null {
  const owed = owedInCurrency(kind, balance, creditLimit);
  if (owed === 0) return 0;
  return toClp(currency, owed, rates);
}
