import { describe, it, expect } from "vitest";
import { owedInCurrency, assetClp, debtClp } from "@/lib/networth";
import type { ClpRates } from "@/lib/rates";

const rates: ClpRates = { CLP: 1, USD: 900 };

describe("owedInCurrency", () => {
  it("credit card owes limit − available", () => {
    expect(owedInCurrency("credit_card", 3550000, 4000000)).toBe(450000);
  });

  it("credit card with no limit owes nothing (available isn't debt)", () => {
    expect(owedInCurrency("credit_card", 999999, null)).toBe(0);
  });

  it("línea with a cupo is treated like a card (limit − available)", () => {
    expect(owedInCurrency("line_of_credit", 80000, 100000)).toBe(20000);
  });

  it("fully-available línea owes nothing", () => {
    expect(owedInCurrency("line_of_credit", 100000, 100000)).toBe(0);
  });

  it("línea with no cupo falls back to the stored owed amount", () => {
    // A manually-entered línea stores the owed amount directly.
    expect(owedInCurrency("line_of_credit", 50000, null)).toBe(50000);
  });

  it("clamps negative owed (available above limit) to 0", () => {
    expect(owedInCurrency("line_of_credit", 120000, 100000)).toBe(0);
  });

  it("non-liabilities owe nothing", () => {
    expect(owedInCurrency("checking", 2500000, null)).toBe(0);
  });
});

describe("assetClp / debtClp with currency conversion", () => {
  it("a USD credit card's debt converts to CLP", () => {
    // limit − available = 2400 − 2345.67 = 261.09 USD -> * 900 CLP.
    expect(debtClp("credit_card", 2345.67, 2400, "USD", rates)).toBeCloseTo(
      261.09 * 900,
      2
    );
  });

  it("a credit card is never an asset", () => {
    expect(assetClp("credit_card", 2345.67, "USD", rates)).toBe(0);
  });

  it("a línea with a cupo is never an asset (available isn't cash)", () => {
    expect(assetClp("line_of_credit", 80000, "CLP", rates)).toBe(0);
  });

  it("returns null debt when the currency has no known rate", () => {
    expect(debtClp("line_of_credit", 80000, 100000, "EUR", rates)).toBeNull();
  });
});
