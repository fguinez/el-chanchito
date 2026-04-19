/**
 * Core budget calculation engine.
 * Replicates the Excel formulas from "Presupuesto mensual.xlsx".
 *
 * All amounts are integers in CLP.
 */

export interface BudgetConfig {
  variableBudget: number; // presup_mensual_var
  fixedBudget: number; // presup_mensual_fijo
  creditCardLimit: number; // cupo_tc
  checkingInitialBalance: number; // monto_cc_inicial
  salary: number; // sueldo
  dayStart: number; // dia_inicio
}

export interface DayPlan {
  day: number;
  expectedTcBalance: number; // cupo_tc_mes - presup_diario * day_number
  expectedCcBalance: number; // checking account balance
  cumulativeAdjustments: number; // sum of variations up to this day
  expectedTotal: number; // TC + CC + adjustments
  isToday: boolean;
}

export interface TodayStatus {
  todayDay: number;
  expectedBalance: number;
  realBalance: number | null;
  drift: number | null; // real - expected (positive = under budget)
  presupDiario: number;
  budgetRemainingMonth: number;
  daysRemaining: number;
  daysInMonth: number;
}

/** presup_diario = presup_mensual_var / days_in_month */
export function calcPresupDiario(
  variableBudget: number,
  daysInMonth: number
): number {
  return Math.round(variableBudget / daysInMonth);
}

/** gasto_estimado_mes = presup_mensual_var + presup_mensual_fijo */
export function calcGastoEstimadoMes(
  variableBudget: number,
  fixedBudget: number
): number {
  return variableBudget + fixedBudget;
}

/** cupo_tc_mes = cupo_tc - deudas_futuras */
export function calcCupoTcMes(
  creditCardLimit: number,
  futureDebts: number
): number {
  return creditCardLimit - futureDebts;
}

/** liquido_mes = cupo_tc + monto_cc_inicial - deudas_futuras */
export function calcLiquidoMes(
  creditCardLimit: number,
  checkingInitialBalance: number,
  futureDebts: number
): number {
  return creditCardLimit + checkingInitialBalance - futureDebts;
}

/** ahorro_por_mes = sueldo - gasto_estimado_mes */
export function calcAhorroPorMes(salary: number, estimatedExpense: number): number {
  return salary - estimatedExpense;
}

/**
 * Generate the 31-day planning table (Planificacion sheet).
 *
 * For each day N (1..daysInMonth):
 *   expectedTcBalance = cupoTcMes - presupDiario * N
 *   expectedCcBalance = checkingInitialBalance
 *   cumulativeAdjustments = sum(adjustments[day <= N])
 *   expectedTotal = expectedTcBalance + expectedCcBalance + cumulativeAdjustments
 */
export function generatePlanningTable(
  config: BudgetConfig,
  daysInMonth: number,
  adjustments: { day: number; amount: number }[],
  futureDebts: number,
  todayDay: number
): DayPlan[] {
  const presupDiario = calcPresupDiario(config.variableBudget, daysInMonth);
  const cupoTcMes = calcCupoTcMes(config.creditCardLimit, futureDebts);

  // Pre-compute cumulative adjustments per day
  const adjustmentsByDay = new Map<number, number>();
  for (const adj of adjustments) {
    adjustmentsByDay.set(
      adj.day,
      (adjustmentsByDay.get(adj.day) || 0) + adj.amount
    );
  }

  const plans: DayPlan[] = [];
  let cumulativeAdj = 0;

  for (let day = 1; day <= daysInMonth; day++) {
    cumulativeAdj += adjustmentsByDay.get(day) || 0;

    const expectedTcBalance = cupoTcMes - presupDiario * day;
    const expectedCcBalance = config.checkingInitialBalance;
    const expectedTotal = expectedTcBalance + expectedCcBalance + cumulativeAdj;

    plans.push({
      day,
      expectedTcBalance,
      expectedCcBalance,
      cumulativeAdjustments: cumulativeAdj,
      expectedTotal,
      isToday: day === todayDay,
    });
  }

  return plans;
}

/**
 * Get the "Today" status widget data.
 */
export function getTodayStatus(
  config: BudgetConfig,
  daysInMonth: number,
  adjustments: { day: number; amount: number }[],
  futureDebts: number,
  todayDay: number,
  realBalance: number | null,
  spentVariableThisMonth: number
): TodayStatus {
  const presupDiario = calcPresupDiario(config.variableBudget, daysInMonth);
  const plans = generatePlanningTable(
    config,
    daysInMonth,
    adjustments,
    futureDebts,
    todayDay
  );
  const todayPlan = plans.find((p) => p.day === todayDay);
  const expectedBalance = todayPlan?.expectedTotal ?? 0;

  return {
    todayDay,
    expectedBalance,
    realBalance,
    drift: realBalance !== null ? realBalance - expectedBalance : null,
    presupDiario,
    budgetRemainingMonth: config.variableBudget - spentVariableThisMonth,
    daysRemaining: daysInMonth - todayDay,
    daysInMonth,
  };
}

/** Get the number of days in a given month */
export function getDaysInMonth(year: number, month: number): number {
  return new Date(year, month, 0).getDate();
}

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

/** Calculate income split ratios */
export function calcIncomeSplit(
  sources: { name: string; amount: number }[],
  targetAmount: number
): { name: string; amount: number; ratio: number; share: number }[] {
  const total = sources.reduce((sum, s) => sum + s.amount, 0);
  if (total === 0) return sources.map((s) => ({ ...s, ratio: 0, share: 0 }));

  return sources.map((s) => ({
    ...s,
    ratio: s.amount / total,
    share: Math.round(targetAmount * (s.amount / total)),
  }));
}
