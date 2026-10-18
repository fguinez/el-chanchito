import { describe, it, expect } from "vitest";
import type { InvestmentMetrics } from "@chanchito/product-model";
import { investmentComposition } from "../investment-composition";

// Synthetic figures only (see the repo's personal data policy): CLP goals
// from the fake 2.500.000 / 1.000.000 / 999.999 set, USD positions from
// small fake values.

function metrics(fields: Omit<InvestmentMetrics, "kind">): InvestmentMetrics {
  return { kind: "investment", ...fields };
}

describe("investmentComposition", () => {
  it("splits a goal into deposits and profit", () => {
    const c = investmentComposition(
      metrics({ nav: 2500000, deposited: 1000000, profit: 1500000 })
    );
    expect(c).toEqual({
      base: 1000000,
      result: 1500000,
      pct: 150,
      nav: 2500000,
      byUnits: false,
    });
  });

  it("keeps a goal's loss as a negative result", () => {
    const c = investmentComposition(
      metrics({ nav: 999999, deposited: 1000000, profit: -1 })
    );
    expect(c?.base).toBe(1000000);
    expect(c?.result).toBe(-1);
    expect(c?.pct).toBeCloseTo(-0.0001, 6);
  });

  it("splits a stock position by the cost of the shares it holds", () => {
    // Profitable sales left net invested negative; the share held cost 100.
    const c = investmentComposition(
      metrics({
        nav: 250.5,
        deposited: -12.34,
        profit: 262.84,
        units: 1,
        unrealized_profit: 150.5,
        unrealized_profit_pct: 150.5,
      })
    );
    expect(c?.byUnits).toBe(true);
    expect(c?.base).toBe(100);
    expect(c?.result).toBe(150.5);
    expect(c?.pct).toBe(150.5);
    expect(c?.nav).toBe(250.5);
  });

  it("derives a stock's percent when it isn't reported", () => {
    const c = investmentComposition(
      metrics({ nav: 250, units: 2.5, unrealized_profit: -10 })
    );
    expect(c?.base).toBe(260);
    expect(c?.pct).toBeCloseTo((-10 / 260) * 100, 6);
  });

  it.each([
    ["a goal without deposits", metrics({ nav: 0, deposited: 0, profit: 0 })],
    [
      "a goal whose withdrawals exceed its deposits",
      metrics({ nav: 1000000, deposited: -1000000, profit: 2000000 }),
    ],
    ["a goal without a profit", metrics({ nav: 2500000, deposited: 1000000 })],
    [
      "a sold-out stock position",
      metrics({
        nav: 0,
        deposited: -20,
        profit: 20,
        units: 0,
        unrealized_profit: 0,
        unrealized_profit_pct: 0,
      }),
    ],
    [
      "a stock position without its unrealized profit",
      metrics({ nav: 250, deposited: 250, profit: 0, units: 2.5 }),
    ],
  ])("hides the card for %s", (_label, m) => {
    expect(investmentComposition(m)).toBeNull();
  });
});
