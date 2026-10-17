import { describe, it, expect } from "vitest";
import {
  isRateStale,
  rateInfoFor,
  ratesFor,
  santiagoDay,
  toClp,
  type RateBook,
  type RateOverride,
} from "@/lib/rates";

// Synthetic fixtures only: fake uuids and slugs, obviously fake rates
// (900 provider, 950.00 / 999.99 institution and product).
const CARD_USD_ID = "00000000-0000-4000-8000-0000000000a1";
const CHECKING_USD_ID = "00000000-0000-4000-8000-0000000000a2";
const OTHER_USD_ID = "00000000-0000-4000-8000-0000000000a3";
const INSTITUTION_ID = "00000000-0000-4000-8000-0000000000b1";

const OBSERVED_AT = new Date("2026-07-10T15:00:00Z");

function override(overrides: Partial<RateOverride>): RateOverride {
  return {
    institutionId: INSTITUTION_ID,
    institutionSlug: "banco-prueba",
    productId: null,
    currency: "USD",
    rate: 950,
    source: "Fuente de prueba",
    reportedAsOf: null,
    observedAt: OBSERVED_AT,
    ...overrides,
  };
}

const provider = { CLP: 1, USD: 900, EUR: 1000 };

const book: RateBook = {
  provider,
  overrides: [
    override({ rate: 950 }),
    override({ productId: CARD_USD_ID, rate: 999.99, reportedAsOf: "2026-07-10" }),
  ],
};

const card = { productId: CARD_USD_ID, institutionSlug: "banco-prueba" };
const checking = { productId: CHECKING_USD_ID, institutionSlug: "banco-prueba" };
const elsewhere = { productId: OTHER_USD_ID, institutionSlug: "otra-fintech" };

describe("ratesFor", () => {
  it("prefers the product rate over the institution and provider ones", () => {
    expect(ratesFor(book, card).USD).toBe(999.99);
  });

  it("falls back to the institution rate for its other products", () => {
    expect(ratesFor(book, checking).USD).toBe(950);
  });

  it("uses the provider rates for other institutions", () => {
    expect(ratesFor(book, elsewhere)).toBe(provider);
  });

  it("keeps the provider rates for currencies with no override", () => {
    expect(ratesFor(book, card).EUR).toBe(1000);
    expect(ratesFor(book, card).CLP).toBe(1);
  });

  it("leaves a currency nothing quotes unconvertible", () => {
    expect(toClp("XAU", 1, ratesFor(book, card))).toBeNull();
  });

  it("never mutates the provider rates", () => {
    ratesFor(book, card);
    expect(provider.USD).toBe(900);
  });
});

describe("rateInfoFor", () => {
  const now = new Date("2026-07-10T20:00:00Z");

  it("describes a product-scoped rate", () => {
    expect(rateInfoFor(book, { ...card, currency: "USD" }, now)).toEqual({
      currency: "USD",
      rate: 999.99,
      scope: "product",
      source: "Fuente de prueba",
      observedAt: OBSERVED_AT.toISOString(),
      reportedAsOf: "2026-07-10",
      stale: false,
    });
  });

  it("describes an institution-scoped rate", () => {
    const info = rateInfoFor(book, { ...checking, currency: "usd" }, now);
    expect(info?.scope).toBe("institution");
    expect(info?.rate).toBe(950);
  });

  it("describes a provider rate as never stale and with no source", () => {
    expect(rateInfoFor(book, { ...elsewhere, currency: "USD" }, now)).toEqual({
      currency: "USD",
      rate: 900,
      scope: "provider",
      source: null,
      observedAt: null,
      reportedAsOf: null,
      stale: false,
    });
  });

  it("flags an override read on an earlier day", () => {
    const nextDay = new Date("2026-07-11T20:00:00Z");
    expect(rateInfoFor(book, { ...card, currency: "USD" }, nextDay)?.stale).toBe(
      true
    );
  });

  it("is null for CLP products and unconvertible currencies", () => {
    expect(rateInfoFor(book, { ...card, currency: "CLP" }, now)).toBeNull();
    expect(rateInfoFor(book, { ...card, currency: "XAU" }, now)).toBeNull();
  });
});

describe("isRateStale", () => {
  // July 2026: Santiago is on UTC-4, so local midnight is 04:00 UTC.
  it("is stale once Santiago passes midnight", () => {
    const lateEvening = new Date("2026-07-10T03:59:00Z"); // 23:59 on Jul 9
    const justAfterMidnight = new Date("2026-07-10T04:01:00Z"); // 00:01 Jul 10
    expect(isRateStale(lateEvening, justAfterMidnight)).toBe(true);
  });

  it("is fresh within the same Santiago day, even across UTC midnight", () => {
    const afternoon = new Date("2026-07-10T22:00:00Z"); // 18:00 on Jul 10
    const lateEvening = new Date("2026-07-11T03:59:00Z"); // 23:59 on Jul 10
    expect(isRateStale(afternoon, lateEvening)).toBe(false);
  });

  it("is fresh right after Santiago midnight for a rate read then", () => {
    const justAfterMidnight = new Date("2026-07-10T04:01:00Z");
    const noon = new Date("2026-07-10T16:00:00Z");
    expect(isRateStale(justAfterMidnight, noon)).toBe(false);
  });

  it("reads the Santiago calendar, not the UTC one", () => {
    expect(santiagoDay(new Date("2026-07-11T03:59:00Z"))).toBe("2026-07-10");
    // October 2026: summer time, UTC-3.
    expect(santiagoDay(new Date("2026-10-16T02:59:00Z"))).toBe("2026-10-15");
    expect(santiagoDay(new Date("2026-10-16T03:01:00Z"))).toBe("2026-10-16");
  });
});
