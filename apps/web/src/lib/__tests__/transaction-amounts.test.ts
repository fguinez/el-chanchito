import { describe, it, expect } from "vitest";
import { formatAmount, formatCLP } from "../utils";
import { clpTotals } from "../transactions";
import { formatBalance } from "@/components/institutions/shared";

// Synthetic figures only (see the repo's personal data policy): CLP amounts
// from the fake set 2.500.000 / 1.000.000 / 999.999, USD 1.234,56, and a
// made-up BTC quantity. Issue #79: transaction amounts can carry cents.

describe("formatAmount", () => {
  it("shows a USD movement with its cents", () => {
    expect(formatAmount("USD", -1234.56)).toBe("-1,234.56 USD");
  });

  it("pads USD to two decimals so cents never disappear", () => {
    expect(formatAmount("USD", 1000.5)).toBe("1,000.50 USD");
    expect(formatAmount("USD", 1000)).toBe("1,000.00 USD");
  });

  it("uses each fiat currency's own minor unit", () => {
    expect(formatAmount("JPY", 1234)).toBe("1,234 JPY");
  });

  it("keeps crypto as a trimmed decimal", () => {
    expect(formatAmount("BTC", 0.12345678)).toBe("0.12345678 BTC");
    expect(formatAmount("BTC", 0.5)).toBe("0.5 BTC");
  });

  it("formats CLP as whole pesos", () => {
    expect(formatAmount("CLP", -999999)).toBe(formatCLP(-999999));
  });
});

describe("formatBalance", () => {
  it("formats a transaction amount in the product's currency", () => {
    expect(formatBalance("USD", -1234.56)).toBe("-1,234.56 USD");
    expect(formatBalance("CLP", 2500000)).toBe(formatCLP(2500000));
  });

  it("keeps null as null", () => {
    expect(formatBalance("USD", null)).toBeNull();
  });
});

describe("clpTotals", () => {
  it("sums CLP expenses and income apart", () => {
    expect(
      clpTotals([
        { amount: -999999, currency: "CLP" },
        { amount: -1000000, currency: "CLP" },
        { amount: 2500000, currency: "CLP" },
      ])
    ).toEqual({ expenses: -1999999, income: 2500000, otherCurrencyCount: 0 });
  });

  it("never adds a USD amount to a peso total", () => {
    expect(
      clpTotals([
        { amount: -999999, currency: "CLP" },
        { amount: -1234.56, currency: "USD" },
        { amount: 1234.56, currency: "USD" },
      ])
    ).toEqual({ expenses: -999999, income: 0, otherCurrencyCount: 2 });
  });

  it("is all zeros for no rows", () => {
    expect(clpTotals([])).toEqual({
      expenses: 0,
      income: 0,
      otherCurrencyCount: 0,
    });
  });
});
