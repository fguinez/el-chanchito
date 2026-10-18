import { describe, it, expect } from "vitest";
import { productTicker } from "../price-history";

// Synthetic values only: the ACME ticker and fake prices (100 / 250.5).

describe("productTicker", () => {
  it("reads a stock position's ticker", () => {
    expect(productTicker({ kind: "investment", ticker: "ACME" })).toBe("ACME");
  });

  it("trims it", () => {
    expect(productTicker({ kind: "investment", ticker: " ACME " })).toBe("ACME");
  });

  it.each([
    ["no attributes", null],
    ["empty attributes", {}],
    ["a goal without a ticker", { kind: "investment" as const }],
    ["a null ticker", { kind: "investment" as const, ticker: null }],
    ["a blank ticker", { kind: "investment" as const, ticker: " " }],
    ["a card", { kind: "credit_card" as const, last4: "1234" }],
  ])("is null for %s", (_label, attributes) => {
    expect(productTicker(attributes)).toBeNull();
  });
});
