import { describe, it, expect } from "vitest";
import { calcWealthMetrics, calcPersonalAmount } from "../budget-engine";

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

  it("computes savings across a two-month gap", () => {
    // ahorro=19200000, period_savings=1000000, months=2, rate=500000
    const metrics = calcWealthMetrics(
      { patrimonio: 20_000_000, deuda: 800_000, date: new Date(2026, 2, 1) },
      { patrimonio: 19_000_000, deuda: 800_000, date: new Date(2026, 0, 1) }
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
      { patrimonio: 9_800_000, deuda: 0, date: new Date(2026, 2, 1) }   // March 1
    );
    expect(metrics.monthsBetween).toBe(0);
    expect(metrics.monthlyRate).toBeNull();
    expect(metrics.periodSavings).toBe(200_000);
  });

  it("handles negative savings (wealth decrease)", () => {
    const metrics = calcWealthMetrics(
      { patrimonio: 2_400_000, deuda: 300_000, date: new Date(2023, 8, 1) },
      { patrimonio: 2_500_000, deuda: 250_000, date: new Date(2023, 5, 1) }
    );
    // ahorro went from 2250000 to 2100000, period_savings=-150000
    expect(metrics.ahorro).toBe(2_100_000);
    expect(metrics.periodSavings).toBe(-150_000);
    expect(metrics.monthsBetween).toBe(3);
    expect(metrics.monthlyRate).toBe(-50_000); // -150000/3
  });
});

describe("calcPersonalAmount", () => {
  it("returns full amount when not shared", () => {
    expect(calcPersonalAmount(500_000, false, 0)).toBe(500_000);
  });

  it("applies 69% ratio for shared expenses", () => {
    // Arriendo 500000 * 0.69 = 345000
    expect(calcPersonalAmount(500_000, true, 0.69)).toBe(345_000);
  });

  it("applies ratio for utilities", () => {
    // Agua 20000 * 0.69 = 13800
    expect(calcPersonalAmount(20_000, true, 0.69)).toBe(13_800);
    // Gas: 50000 * 0.69 = 34500
    expect(calcPersonalAmount(50_000, true, 0.69)).toBe(34_500);
    // Electricidad: 80000 * 0.69 = 55200
    expect(calcPersonalAmount(80_000, true, 0.69)).toBe(55_200);
  });
});
