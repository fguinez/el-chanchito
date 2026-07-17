import { describe, it, expect } from "vitest";
import { owedInCurrency, assetClp, debtClp } from "@/lib/networth";
import type {
  CreditCardMetrics,
  LineOfCreditMetrics,
  LoanMetrics,
} from "@chanchito/product-model";
import type { ClpRates } from "@/lib/rates";

const rates: ClpRates = { CLP: 1, USD: 900 };

const card = (m: Omit<CreditCardMetrics, "kind">): CreditCardMetrics => ({
  kind: "credit_card",
  ...m,
});
const linea = (m: Omit<LineOfCreditMetrics, "kind">): LineOfCreditMetrics => ({
  kind: "line_of_credit",
  ...m,
});
const loan = (m: Omit<LoanMetrics, "kind">): LoanMetrics => ({
  kind: "loan",
  ...m,
});

describe("owedInCurrency", () => {
  it("prefers the reported owed over limit − available", () => {
    // The bank's Utilizado (400.000) is below límite − disponible (450.000,
    // pending holds); the reported figure wins.
    expect(
      owedInCurrency(
        "credit_card",
        3550000,
        card({ available: 3550000, limit: 4000000, owed: 400000 })
      )
    ).toBe(400000);
  });

  it("card with available + limit and no owed derives limit − available", () => {
    expect(
      owedInCurrency(
        "credit_card",
        3550000,
        card({ available: 3550000, limit: 4000000 })
      )
    ).toBe(450000);
  });

  it("card with no metrics owes nothing (available isn't debt)", () => {
    expect(owedInCurrency("credit_card", 999999, null)).toBe(0);
  });

  it("card with only available (no limit) owes nothing", () => {
    expect(owedInCurrency("credit_card", 999999, card({ available: 999999 }))).toBe(
      0
    );
  });

  it("línea with available + limit derives the utilizado", () => {
    expect(
      owedInCurrency("line_of_credit", 80000, linea({ available: 80000, limit: 100000 }))
    ).toBe(20000);
  });

  it("clamps a negative reported owed to 0", () => {
    expect(
      owedInCurrency("credit_card", 100, card({ available: 100, owed: -50 }))
    ).toBe(0);
  });

  it("clamps negative derived owed (available above limit) to 0", () => {
    expect(
      owedInCurrency(
        "line_of_credit",
        120000,
        linea({ available: 120000, limit: 100000 })
      )
    ).toBe(0);
  });

  it("loan stores the owed amount by convention: abs(balance)", () => {
    expect(owedInCurrency("loan", -1400000, null)).toBe(1400000);
    expect(owedInCurrency("mortgage", 78000000, null)).toBe(78000000);
  });

  it("loan metrics' reported owed still wins over the balance", () => {
    expect(owedInCurrency("loan", 1400000, loan({ owed: 1350000 }))).toBe(1350000);
  });

  it("non-liabilities owe nothing, whatever the metrics say", () => {
    expect(owedInCurrency("checking", 2500000, null)).toBe(0);
    expect(owedInCurrency("crypto", 0.5, null)).toBe(0);
  });
});

describe("assetClp / debtClp with currency conversion", () => {
  it("asset conversion is unchanged (CLP passthrough, USD via rate)", () => {
    expect(assetClp("checking", 2500000, "CLP", rates)).toBe(2500000);
    expect(assetClp("checking", 100, "USD", rates)).toBe(90000);
  });

  it("a USD credit card's debt converts to CLP", () => {
    // limit − available = 2400 − 2345.67 = 261.09 USD -> * 900 CLP.
    expect(
      debtClp(
        "credit_card",
        2345.67,
        card({ available: 2345.67, limit: 2400 }),
        "USD",
        rates
      )
    ).toBeCloseTo(261.09 * 900, 2);
  });

  it("a credit card is never an asset", () => {
    expect(assetClp("credit_card", 2345.67, "USD", rates)).toBe(0);
  });

  it("a línea is never an asset (available isn't cash)", () => {
    expect(assetClp("line_of_credit", 80000, "CLP", rates)).toBe(0);
  });

  it("returns null asset when the currency has no known rate", () => {
    expect(assetClp("checking", 100, "EUR", rates)).toBeNull();
  });

  it("returns null debt when the currency has no known rate", () => {
    expect(
      debtClp(
        "line_of_credit",
        80000,
        linea({ available: 80000, limit: 100000 }),
        "EUR",
        rates
      )
    ).toBeNull();
  });

  it("metrics-less card debt is 0, not null, even in a rate-less currency", () => {
    expect(debtClp("credit_card", 100, null, "EUR", rates)).toBe(0);
  });
});
