/** What a CLP total needs from a transaction row. */
export interface CurrencyAmount {
  /** Signed, in `currency`: negative = expense, positive = income. */
  amount: number;
  currency: string;
}

export interface ClpTotals {
  /** Sum of the CLP expenses (zero or negative). */
  expenses: number;
  /** Sum of the CLP income (zero or positive). */
  income: number;
  /** Rows left out because they are in another currency. */
  otherCurrencyCount: number;
}

/**
 * Expense and income totals in pesos over the CLP rows only. A USD movement's
 * amount is dollars (issue #79), so adding it to a peso total would be wrong by
 * the exchange rate; it is counted apart instead until totals convert with
 * lib/rates.
 */
export function clpTotals(rows: readonly CurrencyAmount[]): ClpTotals {
  const totals: ClpTotals = { expenses: 0, income: 0, otherCurrencyCount: 0 };
  for (const { amount, currency } of rows) {
    if (currency !== "CLP") totals.otherCurrencyCount++;
    else if (amount < 0) totals.expenses += amount;
    else totals.income += amount;
  }
  return totals;
}
