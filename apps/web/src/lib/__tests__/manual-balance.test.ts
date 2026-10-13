import { describe, it, expect } from "vitest";
import type { ProductKind } from "@chanchito/product-model";
import {
  acceptsManualBalance,
  MAX_MANUAL_BALANCE,
  manualBalanceMetrics,
  parseClpInput,
  parseManualBalance,
  snapshotSourceLabel,
} from "../manual-balance";

// Synthetic amounts only (2.500.000 / 999.999).

describe("parseManualBalance", () => {
  it.each([2_500_000, 0, MAX_MANUAL_BALANCE])(
    "accepts the whole CLP amount %d",
    (balance) => {
      expect(parseManualBalance({ balance })).toEqual({ ok: true, balance });
    }
  );

  it.each([
    ["a negative amount", { balance: -1 }],
    ["a decimal (CLP has none; never rounded)", { balance: 999_999.5 }],
    ["NaN", { balance: Number.NaN }],
    ["Infinity", { balance: Number.POSITIVE_INFINITY }],
    ["an unsafe integer", { balance: Number.MAX_SAFE_INTEGER + 1 }],
    ["more digits than the NUMERIC column holds", { balance: MAX_MANUAL_BALANCE + 1 }],
    ["a numeric string", { balance: "1000000" }],
    ["null", { balance: null }],
    ["a missing field", {}],
  ])("rejects %s", (_label, body) => {
    const result = parseManualBalance(body);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toEqual(expect.any(String));
  });

  it.each([
    ["null", null],
    ["an array", [2_500_000]],
    ["a bare number", 2_500_000],
    ["a string", "2500000"],
  ])("rejects a non-object body: %s", (_label, body) => {
    expect(parseManualBalance(body).ok).toBe(false);
  });
});

describe("parseClpInput", () => {
  it.each([
    ["2500000", 2_500_000],
    ["2.500.000", 2_500_000],
    ["$ 2.500.000", 2_500_000],
    [" 999.999 ", 999_999],
    ["$1.000.000", 1_000_000],
    ["0", 0],
  ])("reads %j as %d", (text, expected) => {
    expect(parseClpInput(text)).toBe(expected);
  });

  it.each([
    ["an empty string", ""],
    ["a decimal comma", "999.999,5"],
    ["a dot that is not a thousands separator", "2500.50"],
    ["a misplaced separator", "25.00.000"],
    ["a negative sign", "-1.000.000"],
    ["letters", "1000000abc"],
  ])("rejects %s", (_label, text) => {
    expect(parseClpInput(text)).toBeNull();
  });
});

describe("acceptsManualBalance", () => {
  it("accepts wallets", () => {
    expect(acceptsManualBalance("wallet")).toBe(true);
  });

  it.each<ProductKind>(["checking", "credit_card", "investment", "crypto"])(
    "rejects %s, whose metrics carry more than one number",
    (kind) => {
      expect(acceptsManualBalance(kind)).toBe(false);
    }
  );
});

describe("manualBalanceMetrics", () => {
  it("builds the wallet payload from the balance", () => {
    expect(manualBalanceMetrics("wallet", 2_500_000)).toEqual({
      kind: "wallet",
      balance: 2_500_000,
    });
  });

  it("refuses a kind that doesn't accept manual balances", () => {
    expect(() => manualBalanceMetrics("checking", 2_500_000)).toThrow();
  });
});

describe("snapshotSourceLabel", () => {
  it.each([
    ["scraper", "Scraper"],
    ["manual", "Manual"],
    ["derived", "Derivado"],
    ["wealth_snapshot", "Histórico"],
  ])("labels %s as %s", (source, label) => {
    expect(snapshotSourceLabel(source)).toBe(label);
  });

  it("passes an unknown source through unchanged", () => {
    expect(snapshotSourceLabel("csv_import")).toBe("csv_import");
  });

  it("does not resolve inherited object keys", () => {
    expect(snapshotSourceLabel("toString")).toBe("toString");
  });
});
