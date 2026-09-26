import { describe, it, expect } from "vitest";
import { displayProductName } from "@/lib/product-names";

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
