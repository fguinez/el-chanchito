import { describe, it, expect } from "vitest";
import {
  displayProductName,
  fillsNameFromDefault,
  nameFieldPatch,
} from "@/lib/product-names";

describe("displayProductName", () => {
  it("replaces an auto-name with the friendly kind label", () => {
    const product = { name: "Banco de Chile - checking", kind: "checking" as const, currency: "CLP" };
    expect(displayProductName(product, "Banco de Chile")).toBe("Cuenta corriente");
  });

  it("shows a crypto auto-name as its currency", () => {
    const product = { name: "Buda - crypto (BTC)", kind: "crypto" as const, currency: "BTC" };
    expect(displayProductName(product, "Buda")).toBe("BTC");
  });

  it("still recognizes an auto-name after the institution was renamed", () => {
    const product = { name: "csv_import - checking", kind: "checking" as const, currency: "CLP" };
    expect(displayProductName(product, "Cartola manual")).toBe("Cuenta corriente");
  });

  it("recognizes a renamed institution's auto-name with a currency suffix", () => {
    const product = { name: "csv_import - checking (USD)", kind: "checking" as const, currency: "USD" };
    expect(displayProductName(product, "Cartola manual")).toBe("Cuenta corriente");
  });

  it("strips the current institution prefix from a human suffix", () => {
    const product = { name: "Banco de Chile - Cuenta sueldo", kind: "checking" as const, currency: "CLP" };
    expect(displayProductName(product, "Banco de Chile")).toBe("Cuenta sueldo");
  });

  it("leaves a human name with a dash untouched", () => {
    const product = { name: "Ahorro - vacaciones", kind: "savings" as const, currency: "CLP" };
    expect(displayProductName(product, "Banco de Chile")).toBe("Ahorro - vacaciones");
  });
});

describe("nameFieldPatch", () => {
  // Synthetic names only.
  const unrenamed = {
    name: "Banco Sintetico - credit_card",
    defaultName: "Banco Sintetico - credit_card",
  };
  const renamed = { ...unrenamed, name: "Mi tarjeta de ejemplo" };
  const label = "Tarjeta de crédito";

  it.each([
    ["an empty field on an unrenamed product", "  ", unrenamed, undefined],
    ["an empty field on a renamed product", "", renamed, null],
    ["the placeholder label on an unrenamed product", ` ${label} `, unrenamed, undefined],
    ["the placeholder label on a renamed product", label, renamed, null],
    ["the current rename, unchanged", "Mi tarjeta de ejemplo", renamed, undefined],
    ["a new name", " Tarjeta Sintetica ", unrenamed, "Tarjeta Sintetica"],
  ])("maps %s", (_label, input, product, expected) => {
    expect(nameFieldPatch(input, product, label)).toBe(expected);
  });
});

describe("fillsNameFromDefault", () => {
  it.each([
    ["a double-click on an empty field", "", undefined, true],
    ["ArrowRight on an empty field", "", "ArrowRight", true],
    ["End on an empty field", "", "End", true],
    ["Tab on an empty field", "", "Tab", false],
    ["a letter on an empty field", "", "a", false],
    ["a double-click on typed text", "Mi tarjeta", undefined, false],
    ["ArrowRight on typed text", "Mi tarjeta", "ArrowRight", false],
  ])("%s", (_label, value, key, expected) => {
    expect(fillsNameFromDefault(value, key)).toBe(expected);
  });
});
