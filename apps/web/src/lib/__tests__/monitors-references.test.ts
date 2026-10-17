import { describe, it, expect } from "vitest";
import type { EvalContext, ProductInfo } from "@/lib/monitors";
import { buildReferences } from "@/lib/monitors/serialize";
import type { RateBook } from "@/lib/rates";

// Synthetic fixtures only: fake uuids and slugs, obviously fake amounts and
// rates (900 provider, 999.99 product).
const CARD_USD_ID = "00000000-0000-4000-8000-0000000000c1";
const CHECKING_ID = "00000000-0000-4000-8000-0000000000c2";
const UNKNOWN_ID = "00000000-0000-4000-8000-0000000000c3";

const rates: RateBook = {
  provider: { CLP: 1, USD: 900 },
  overrides: [
    {
      institutionId: "00000000-0000-4000-8000-0000000000d1",
      institutionSlug: "banco-prueba",
      productId: CARD_USD_ID,
      currency: "USD",
      rate: 999.99,
      source: "Fuente de prueba",
      reportedAsOf: null,
      observedAt: new Date("2026-07-10T15:00:00Z"),
    },
  ],
};

function product(info: Partial<ProductInfo> & Pick<ProductInfo, "id">): ProductInfo {
  return {
    kind: "checking",
    currency: "CLP",
    isActive: true,
    metrics: null,
    balanceAsOf: null,
    slug: "producto",
    institutionSlug: "banco-prueba",
    name: "Producto",
    ...info,
  };
}

const products = new Map<string, ProductInfo>([
  [
    CARD_USD_ID,
    product({
      id: CARD_USD_ID,
      kind: "credit_card",
      currency: "USD",
      slug: "tarjeta_usd",
      metrics: { kind: "credit_card", available: 1000, owed: 100 },
    }),
  ],
  [
    CHECKING_ID,
    product({
      id: CHECKING_ID,
      metrics: { kind: "checking", balance: 1000000 },
    }),
  ],
]);

function refsFor(expression: string, currency = "CLP") {
  const ctx: EvalContext = {
    date: new Date("2026-07-15T12:00:00Z"),
    products,
    rates,
    currency,
  };
  return buildReferences(
    {
      currency,
      expression,
      thresholds: [{ severity: "alert", comparator: "<", expression: "0" }],
    },
    ctx
  );
}

describe("buildReferences monitorValue", () => {
  it("converts a foreign-currency ref with the product's own rate", () => {
    const [card] = refsFor(`@{${CARD_USD_ID}:owed}`);

    expect(card).toMatchObject({
      latestValue: 100,
      monitorValue: 99999,
      converted: true,
    });
  });

  it("passes a ref in the monitor currency through unconverted", () => {
    const [checking] = refsFor(`@{${CHECKING_ID}:balance}`);

    expect(checking).toMatchObject({
      latestValue: 1000000,
      monitorValue: 1000000,
      converted: false,
    });
  });

  it("flags an unconvertible foreign ref as converted with no value", () => {
    const [card] = refsFor(`@{${CARD_USD_ID}:owed}`, "EUR");

    expect(card).toMatchObject({
      latestValue: 100,
      monitorValue: null,
      converted: true,
    });
  });

  it("leaves monitorValue null for a ref that does not resolve", () => {
    const [unknown] = refsFor(`@{${UNKNOWN_ID}:owed}`);

    expect(unknown).toMatchObject({ monitorValue: null, broken: true });
  });
});
