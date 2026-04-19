import { describe, it, expect } from "vitest";
import {
  calcPresupDiario,
  calcGastoEstimadoMes,
  calcCupoTcMes,
  calcLiquidoMes,
  calcAhorroPorMes,
  generatePlanningTable,
  getTodayStatus,
  getDaysInMonth,
  calcWealthMetrics,
  calcPersonalAmount,
  calcIncomeSplit,
  type BudgetConfig,
} from "../budget-engine";

// Default config matching the Excel "Presupuesto mensual.xlsx"
const EXCEL_CONFIG: BudgetConfig = {
  variableBudget: 600_000,
  fixedBudget: 1_000_000,
  creditCardLimit: 2_000_000,
  checkingInitialBalance: 0,
  salary: 1_500_000,
  dayStart: 1,
};

describe("getDaysInMonth", () => {
  it("returns correct days for each month of 2026", () => {
    expect(getDaysInMonth(2026, 1)).toBe(31); // January
    expect(getDaysInMonth(2026, 2)).toBe(28); // February (non-leap)
    expect(getDaysInMonth(2026, 3)).toBe(31); // March
    expect(getDaysInMonth(2026, 4)).toBe(30); // April
    expect(getDaysInMonth(2026, 6)).toBe(30); // June
    expect(getDaysInMonth(2026, 12)).toBe(31); // December
  });

  it("handles leap years", () => {
    expect(getDaysInMonth(2024, 2)).toBe(29);
    expect(getDaysInMonth(2025, 2)).toBe(28);
  });
});

describe("calcPresupDiario", () => {
  it("matches Excel: 600000 / 31 = 19355", () => {
    // Excel formula: =presup_mensual_var/31
    expect(calcPresupDiario(600_000, 31)).toBe(19355);
  });

  it("adjusts for shorter months", () => {
    expect(calcPresupDiario(600_000, 30)).toBe(30000);
    expect(calcPresupDiario(600_000, 28)).toBe(21429);
  });

  it("handles zero budget", () => {
    expect(calcPresupDiario(0, 31)).toBe(0);
  });
});

describe("calcGastoEstimadoMes", () => {
  it("matches Excel: 600000 + 1000000 = 1600000", () => {
    expect(calcGastoEstimadoMes(600_000, 1_000_000)).toBe(1_600_000);
  });
});

describe("calcCupoTcMes", () => {
  it("matches Excel: 2000000 - 0 = 2000000", () => {
    expect(calcCupoTcMes(2_000_000, 0)).toBe(2_000_000);
  });

  it("subtracts future debts", () => {
    expect(calcCupoTcMes(2_000_000, 500_000)).toBe(1_500_000);
  });
});

describe("calcLiquidoMes", () => {
  it("matches Excel: 2000000 + 0 - 0 = 2000000", () => {
    expect(calcLiquidoMes(2_000_000, 0, 0)).toBe(2_000_000);
  });

  it("includes checking balance and subtracts debts", () => {
    expect(calcLiquidoMes(2_000_000, 200_000, 100_000)).toBe(2_100_000);
  });
});

describe("calcAhorroPorMes", () => {
  it("matches Excel: 1500000 - 1600000 = 1000000", () => {
    expect(calcAhorroPorMes(1_500_000, 1_600_000)).toBe(1_000_000);
  });

  it("can be negative if spending exceeds salary", () => {
    expect(calcAhorroPorMes(2_000_000, 2_500_000)).toBe(-500_000);
  });
});

describe("generatePlanningTable", () => {
  it("generates correct number of rows", () => {
    const plans = generatePlanningTable(EXCEL_CONFIG, 30, [], 0, 12);
    expect(plans).toHaveLength(30);
  });

  it("matches Excel day 1 balance", () => {
    // Excel: cupo_tc_mes - presup_diario * 1 = 2000000 - 30000*1 = 1980000
    const plans = generatePlanningTable(EXCEL_CONFIG, 30, [], 0, 1);
    const presupDiario = calcPresupDiario(600_000, 30); // 30000
    expect(plans[0].expectedTcBalance).toBe(2_000_000 - presupDiario * 1);
    expect(plans[0].expectedCcBalance).toBe(0);
    expect(plans[0].expectedTotal).toBe(2_000_000 - presupDiario);
  });

  it("matches Excel day 12 for April (30 days)", () => {
    const plans = generatePlanningTable(EXCEL_CONFIG, 30, [], 0, 12);
    const presupDiario = calcPresupDiario(600_000, 30); // 30000
    const day12 = plans[11]; // index 11 = day 12
    expect(day12.day).toBe(12);
    expect(day12.expectedTcBalance).toBe(2_000_000 - presupDiario * 12);
    expect(day12.expectedTotal).toBe(2_000_000 - presupDiario * 12);
    expect(day12.isToday).toBe(true);
  });

  it("last day has lowest expected balance", () => {
    const plans = generatePlanningTable(EXCEL_CONFIG, 30, [], 0, 1);
    const last = plans[29];
    const presupDiario = calcPresupDiario(600_000, 30);
    expect(last.expectedTotal).toBe(2_000_000 - presupDiario * 30);
  });

  it("applies adjustments cumulatively", () => {
    const adjustments = [
      { day: 5, amount: -50_000 }, // extra expense on day 5
      { day: 10, amount: 100_000 }, // extra income on day 10
    ];
    const plans = generatePlanningTable(EXCEL_CONFIG, 30, adjustments, 0, 1);

    // Day 4: no adjustments yet
    expect(plans[3].cumulativeAdjustments).toBe(0);

    // Day 5: -50000
    expect(plans[4].cumulativeAdjustments).toBe(-50_000);

    // Day 9: still -50000
    expect(plans[8].cumulativeAdjustments).toBe(-50_000);

    // Day 10: -50000 + 100000 = 50000
    expect(plans[9].cumulativeAdjustments).toBe(50_000);

    // Day 30: still 50000
    expect(plans[29].cumulativeAdjustments).toBe(50_000);
  });

  it("includes checking balance in total", () => {
    const config = { ...EXCEL_CONFIG, checkingInitialBalance: 200_000 };
    const plans = generatePlanningTable(config, 30, [], 0, 1);
    const presupDiario = calcPresupDiario(600_000, 30);
    expect(plans[0].expectedCcBalance).toBe(200_000);
    expect(plans[0].expectedTotal).toBe(
      2_000_000 - presupDiario + 200_000
    );
  });

  it("accounts for future debts", () => {
    const plans = generatePlanningTable(EXCEL_CONFIG, 30, [], 500_000, 1);
    const presupDiario = calcPresupDiario(600_000, 30);
    // cupo_tc_mes = 2000000 - 500000 = 1500000
    expect(plans[0].expectedTcBalance).toBe(1_500_000 - presupDiario);
  });
});

describe("getTodayStatus", () => {
  it("calculates drift when real balance is available", () => {
    const status = getTodayStatus(
      EXCEL_CONFIG, 30, [], 0, 12, 1_800_000, 100_000
    );
    const presupDiario = calcPresupDiario(600_000, 30);
    const expected = 2_000_000 - presupDiario * 12;
    expect(status.expectedBalance).toBe(expected);
    expect(status.realBalance).toBe(1_800_000);
    expect(status.drift).toBe(1_800_000 - expected);
  });

  it("returns null drift when real balance is not available", () => {
    const status = getTodayStatus(
      EXCEL_CONFIG, 30, [], 0, 12, null, 0
    );
    expect(status.drift).toBeNull();
    expect(status.realBalance).toBeNull();
  });

  it("calculates remaining budget correctly", () => {
    const status = getTodayStatus(
      EXCEL_CONFIG, 30, [], 0, 12, null, 300_000
    );
    expect(status.budgetRemainingMonth).toBe(600_000); // 600000 - 300000
    expect(status.daysRemaining).toBe(18); // 30 - 12
  });
});

describe("calcWealthMetrics", () => {
  it("calculates ahorro = patrimonio - deuda", () => {
    const metrics = calcWealthMetrics(
      { patrimonio: 20_000_000, deuda: 800_000, date: new Date(2026, 2, 1) },
      null
    );
    expect(metrics.ahorro).toBe(19_200_000);
    expect(metrics.periodSavings).toBeNull();
    expect(metrics.monthlyRate).toBeNull();
  });

  it("matches Excel historial row Mar 2026", () => {
    // Excel: ahorro=19200000, period_savings=1000000, months=2, rate=500000
    const metrics = calcWealthMetrics(
      { patrimonio: 20_000_000, deuda: 800_000, date: new Date(2026, 2, 1) },
      { patrimonio: 19_000_000, deuda: 700_000, date: new Date(2026, 0, 1) }
    );
    expect(metrics.ahorro).toBe(19_200_000);
    expect(metrics.periodSavings).toBe(1_000_000);
    expect(metrics.monthsBetween).toBe(2);
    expect(metrics.monthlyRate).toBe(500_000);
  });

  it("handles same-month snapshots (0 months between)", () => {
    // Use explicit dates to avoid UTC timezone issues
    const metrics = calcWealthMetrics(
      { patrimonio: 10_000_000, deuda: 0, date: new Date(2026, 2, 15) }, // March 15
      { patrimonio: 2_000_000, deuda: 0, date: new Date(2026, 2, 1) }   // March 1
    );
    expect(metrics.monthsBetween).toBe(0);
    expect(metrics.monthlyRate).toBeNull();
    expect(metrics.periodSavings).toBe(500_000);
  });

  it("handles negative savings (wealth decrease)", () => {
    const metrics = calcWealthMetrics(
      { patrimonio: 2_400_000, deuda: 300_000, date: new Date(2023, 8, 1) },
      { patrimonio: 2_500_000, deuda: 250_000, date: new Date(2023, 5, 1) }
    );
    // Excel: ahorro went from 2250000 to 2100000, period_savings=-150000
    expect(metrics.ahorro).toBe(2_100_000);
    expect(metrics.periodSavings).toBe(-150_000);
    expect(metrics.monthsBetween).toBe(3);
    expect(metrics.monthlyRate).toBe(-50000); // -150000/3 rounded
  });
});

describe("calcPersonalAmount", () => {
  it("returns full amount when not shared", () => {
    expect(calcPersonalAmount(500_000, false, 0)).toBe(500_000);
  });

  it("applies 69% ratio for shared expenses", () => {
    // Excel: Arriendo 500000 * 0.69 = 345000
    expect(calcPersonalAmount(500_000, true, 0.69)).toBe(345_000);
  });

  it("applies ratio for utilities", () => {
    // Excel: Agua 20000 * 0.69 = 13800
    expect(calcPersonalAmount(20_000, true, 0.69)).toBe(13_800);
    // Gas: 50000 * 0.69 = 34500
    expect(calcPersonalAmount(50_000, true, 0.69)).toBe(34_500);
    // Electricidad: 80000 * 0.69 = 55200
    expect(calcPersonalAmount(80_000, true, 0.69)).toBe(55_200);
  });
});

describe("calcIncomeSplit", () => {
  it("matches Excel porcentaje pagos", () => {
    // Excel: Sueldo1=1500000 (70.34%), Sueldo2=1000000 (29.66%)
    const result = calcIncomeSplit(
      [
        { name: "Sueldo 1", amount: 1_500_000 },
        { name: "Sueldo 2", amount: 1_000_000 },
      ],
      800_000
    );

    expect(result).toHaveLength(2);

    // Sueldo 1: 70.34% of 800000 = 480000
    expect(result[0].ratio).toBeCloseTo(0.7034, 3);
    expect(result[0].share).toBe(480_000);

    // Sueldo 2: 29.66% of 800000 = 320000
    expect(result[1].ratio).toBeCloseTo(0.2966, 3);
    expect(result[1].share).toBe(320_000);
  });

  it("handles single income source", () => {
    const result = calcIncomeSplit(
      [{ name: "Sueldo", amount: 1_500_000 }],
      500_000
    );
    expect(result[0].ratio).toBe(1);
    expect(result[0].share).toBe(500_000);
  });

  it("handles zero total income", () => {
    const result = calcIncomeSplit(
      [
        { name: "A", amount: 0 },
        { name: "B", amount: 0 },
      ],
      800_000
    );
    expect(result[0].ratio).toBe(0);
    expect(result[0].share).toBe(0);
  });
});
