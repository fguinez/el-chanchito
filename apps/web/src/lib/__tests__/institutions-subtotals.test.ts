import { describe, it, expect, vi } from "vitest";
import type { RateBook } from "@/lib/rates";

// computeSubtotals is pure; the module's loaders are never called here.
vi.mock("@/lib/db", () => ({ db: {} }));

const { computeSubtotals } = await import("@/lib/institutions-data");
type ApiProduct = Parameters<typeof computeSubtotals>[0][number];

// Synthetic fixtures only: fake uuids, USD 1.234,56 and obviously fake rates
// (900 provider, 950.00 institution, 999.99 product).
const CARD_ID = "00000000-0000-4000-8000-0000000000c1";
const SAVINGS_ID = "00000000-0000-4000-8000-0000000000c2";

function product(overrides: Partial<ApiProduct>): ApiProduct {
  return {
    id: SAVINGS_ID,
    accountId: "00000000-0000-4000-8000-0000000000d1",
    accountName: "Personal",
    parentProductId: null,
    kind: "savings",
    name: "Cuenta de prueba",
    slug: "cuenta-prueba",
    currency: "USD",
    currentBalance: 1234.56,
    currentBalanceClp: null,
    exchangeRate: null,
    balanceAsOf: null,
    externalRef: null,
    attributes: {},
    metrics: null,
    isActive: true,
    displayOrder: 0,
    ...overrides,
  };
}

function book(overrides: RateBook["overrides"]): RateBook {
  return { provider: { CLP: 1, USD: 900 }, overrides };
}

const observedAt = new Date("2026-07-10T15:00:00Z");
const base = {
  institutionId: "00000000-0000-4000-8000-0000000000e1",
  institutionSlug: "banco-prueba",
  currency: "USD",
  source: "Fuente de prueba",
  reportedAsOf: null,
  observedAt,
};

describe("computeSubtotals with institution rates", () => {
  const card = product({
    id: CARD_ID,
    kind: "credit_card",
    slug: "tarjeta-usd",
    currentBalance: 0,
    metrics: { kind: "credit_card", available: 0, owed: 100 },
  });
  const savings = product({});

  it("converts with the provider rate when nothing overrides it", () => {
    const totals = computeSubtotals([savings, card], "banco-prueba", book([]));
    expect(totals.patrimonioClp).toBeCloseTo(1234.56 * 900);
    expect(totals.deudaClp).toBeCloseTo(100 * 900);
  });

  it("uses the institution rate, and the product rate over it", () => {
    const totals = computeSubtotals(
      [savings, card],
      "banco-prueba",
      book([
        { ...base, productId: null, rate: 950 },
        { ...base, productId: CARD_ID, rate: 999.99 },
      ])
    );
    expect(totals.patrimonioClp).toBeCloseTo(1234.56 * 950);
    expect(totals.deudaClp).toBeCloseTo(100 * 999.99);
  });

  it("ignores another institution's rates", () => {
    const totals = computeSubtotals(
      [savings],
      "banco-prueba",
      book([{ ...base, institutionSlug: "otra-fintech", productId: null, rate: 950 }])
    );
    expect(totals.patrimonioClp).toBeCloseTo(1234.56 * 900);
  });
});
