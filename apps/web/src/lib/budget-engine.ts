/**
 * Small finance helpers.
 *
 * What remains after the budget engine was retired in favor of Monitores:
 * a wealth-snapshot metric derivation (used by /api/wealth) and a
 * shared-expense personal-amount split (used by the fixed-expenses page).
 * All amounts are integers in CLP.
 */

/** Calculate wealth snapshot derived fields (Historial sheet logic) */
export function calcWealthMetrics(
  current: { patrimonio: number; deuda: number; date: Date },
  previous: { patrimonio: number; deuda: number; date: Date } | null
) {
  const ahorro = current.patrimonio - current.deuda;
  if (!previous) {
    return { ahorro, periodSavings: null, monthsBetween: null, monthlyRate: null };
  }

  const prevAhorro = previous.patrimonio - previous.deuda;
  const periodSavings = ahorro - prevAhorro;

  const monthsBetween =
    (current.date.getFullYear() - previous.date.getFullYear()) * 12 +
    (current.date.getMonth() - previous.date.getMonth());

  const monthlyRate =
    monthsBetween > 0 ? Math.round(periodSavings / monthsBetween) : null;

  return { ahorro, periodSavings, monthsBetween, monthlyRate };
}

/** Calculate personal amount for a fixed expense */
export function calcPersonalAmount(
  amount: number,
  isShared: boolean,
  sharedRatio: number
): number {
  return isShared ? Math.round(amount * sharedRatio) : amount;
}
