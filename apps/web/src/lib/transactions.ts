/** Longest note a transaction may carry (POST /api/transactions and PATCH
 *  /api/transactions/[id]); the detail panel's notes box caps its input at the
 *  same length. */
export const TRANSACTION_NOTES_MAX_LENGTH = 2000;

/** What a CLP total needs from a transaction row. */
export interface CurrencyAmount {
  /** Signed, in `currency`: negative = expense, positive = income. */
  amount: number;
  currency: string;
  /** Linked to an internal transfer: money moving between the user's own
   *  products, so neither spending nor income. */
  isInternalTransfer?: boolean;
}

/**
 * Whether a row counts toward Gastos/Ingresos. A row an internal transfer
 * links (`is_internal_transfer`) is the user's money changing product, not
 * spending or income, so the totals skip it; a balance walk still counts it,
 * because the product's balance really moved.
 */
export function countsTowardFlow(row: { isInternalTransfer?: boolean }): boolean {
  return !row.isInternalTransfer;
}

export interface ClpTotals {
  /** Sum of the CLP expenses (zero or negative). */
  expenses: number;
  /** Sum of the CLP income (zero or positive). */
  income: number;
  /** Rows left out because they are in another currency. */
  otherCurrencyCount: number;
  /** Rows left out because an internal transfer links them. */
  internalTransferCount: number;
}

/**
 * Expense and income totals in pesos over the CLP rows only. A USD movement's
 * amount is dollars (issue #79), so adding it to a peso total would be wrong by
 * the exchange rate; it is counted apart instead until totals convert with
 * lib/rates. Rows an internal transfer links are left out too (see
 * countsTowardFlow) and counted apart.
 */
export function clpTotals(rows: readonly CurrencyAmount[]): ClpTotals {
  const totals: ClpTotals = {
    expenses: 0,
    income: 0,
    otherCurrencyCount: 0,
    internalTransferCount: 0,
  };
  for (const row of rows) {
    const { amount, currency } = row;
    if (!countsTowardFlow(row)) totals.internalTransferCount++;
    else if (currency !== "CLP") totals.otherCurrencyCount++;
    else if (amount < 0) totals.expenses += amount;
    else totals.income += amount;
  }
  return totals;
}
