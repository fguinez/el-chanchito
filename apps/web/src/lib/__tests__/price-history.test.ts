import { describe, it, expect } from "vitest";
import type { ProductMetrics } from "@chanchito/product-model";
import {
  buildPriceRows,
  productTicker,
  rangeChange,
  showsPriceHistory,
  type PriceRow,
} from "../price-history";

// Synthetic values only: the ACME ticker and fake prices (100 / 250.5).

describe("productTicker", () => {
  it("reads a stock position's ticker", () => {
    expect(productTicker({ kind: "stock", ticker: "ACME" })).toBe("ACME");
  });

  it("trims it", () => {
    expect(productTicker({ kind: "stock", ticker: " ACME " })).toBe("ACME");
  });

  it.each([
    ["no attributes", null],
    ["empty attributes", {}],
    ["a goal without a ticker", { kind: "investment" as const }],
    ["a null ticker", { kind: "stock" as const, ticker: null }],
    ["a blank ticker", { kind: "stock" as const, ticker: " " }],
    ["a card", { kind: "credit_card" as const, last4: "1234" }],
  ])("is null for %s", (_label, attributes) => {
    expect(productTicker(attributes)).toBeNull();
  });
});

describe("showsPriceHistory", () => {
  const held = (units: number | null | undefined): ProductMetrics => ({
    kind: "stock",
    nav: 250.5,
    units,
  });
  const closes = [{ date: "2026-01-02", price: 100 }];

  it("shows a stock with closes, even once sold out", () => {
    expect(showsPriceHistory(closes, held(2.5))).toBe(true);
    expect(showsPriceHistory(closes, held(0))).toBe(true);
    expect(showsPriceHistory(closes, null)).toBe(true);
  });

  it("shows a held stock still waiting for its first closes", () => {
    expect(showsPriceHistory([], held(2.5))).toBe(true);
  });

  it.each([
    ["sold out", held(0)],
    ["without units", held(null)],
    ["with units unknown", held(undefined)],
    ["without metrics", null],
    ["with a goal's metrics", { kind: "investment", nav: 250.5 } as ProductMetrics],
    ["with other metrics", { kind: "wallet", balance: 100 } as ProductMetrics],
  ])("hides a stock without closes %s", (_label, metrics) => {
    expect(showsPriceHistory([], metrics)).toBe(false);
  });
});

// vitest.config.ts pins TZ=America/Santiago.
describe("buildPriceRows", () => {
  it("puts each close on its trading day's local midnight", () => {
    const rows = buildPriceRows([
      { date: "2026-01-02", price: 100 },
      { date: "2026-07-06", price: 250.5 },
    ]);

    expect(rows).toEqual([
      { t: new Date(2026, 0, 2).getTime(), date: "2026-01-02", price: 100 },
      { t: new Date(2026, 6, 6).getTime(), date: "2026-07-06", price: 250.5 },
    ]);
    expect(new Date(rows[1].t).getDate()).toBe(6);
  });

  it("is empty without closes", () => {
    expect(buildPriceRows([])).toEqual([]);
  });
});

describe("rangeChange", () => {
  const rows: PriceRow[] = buildPriceRows([
    { date: "2026-01-02", price: 100 },
    { date: "2026-01-05", price: 250.5 },
    { date: "2026-01-06", price: 200 },
    { date: "2026-01-07", price: 250.5 },
  ]);
  const day = (date: string) => buildPriceRows([{ date, price: 1 }])[0].t;

  it("spans every close for the whole history", () => {
    const change = rangeChange(rows, null);

    expect(change?.from.date).toBe("2026-01-02");
    expect(change?.to.date).toBe("2026-01-07");
    expect(change?.change).toBeCloseTo(150.5);
    expect(change?.pct).toBeCloseTo(150.5);
  });

  it("spans the first and last close inside the window, edges included", () => {
    const change = rangeChange(rows, [day("2026-01-05"), day("2026-01-06")]);

    expect(change?.from.date).toBe("2026-01-05");
    expect(change?.to.date).toBe("2026-01-06");
    expect(change?.change).toBeCloseTo(-50.5);
    expect(change?.pct).toBeCloseTo((-50.5 / 250.5) * 100);
  });

  it.each([
    ["one close in the window", [day("2026-01-07"), day("2026-01-08")]],
    ["no close in the window", [day("2026-02-01"), day("2026-02-28")]],
  ] as const)("is null with %s", (_label, bounds) => {
    expect(rangeChange(rows, [...bounds])).toBeNull();
  });

  it("is null with fewer than two closes overall", () => {
    expect(rangeChange(rows.slice(0, 1), null)).toBeNull();
    expect(rangeChange([], null)).toBeNull();
  });
});
