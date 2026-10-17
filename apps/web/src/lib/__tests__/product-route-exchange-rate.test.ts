import { beforeEach, describe, expect, it, vi } from "vitest";

// Fake db: every select gets its own chain whose terminal step (`.limit()`,
// or awaiting the chain itself as the exchange_rates read does) resolves to
// the next queued rows, in query order. No Postgres needed.
const queuedRows: unknown[][] = [];

vi.mock("@/lib/db", () => {
  const next = () => Promise.resolve(queuedRows.shift() ?? []);
  const makeChain = () => {
    const chain: Record<string, unknown> = {};
    for (const step of ["from", "innerJoin", "where", "orderBy"]) {
      chain[step] = () => chain;
    }
    chain.limit = next;
    chain.then = (
      resolve: (rows: unknown[]) => unknown,
      reject: (error: unknown) => unknown
    ) => next().then(resolve, reject);
    return chain;
  };
  return { db: { select: () => makeChain() } };
});

// Provider rates without the network.
vi.mock("@/lib/rates", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/rates")>()),
  getClpRates: async () => ({ CLP: 1, USD: 900 }),
}));

const { GET } = await import(
  "@/app/api/institutions/[slug]/products/[product]/route"
);

// Synthetic fixtures only: fake ids and slugs, USD 1.234,56, fake rates.
const INSTITUTION_ID = "00000000-0000-4000-8000-0000000000f1";
const PRODUCT_ID = "00000000-0000-4000-8000-0000000000f2";

function productRow(overrides: Record<string, unknown> = {}) {
  return {
    institutionId: INSTITUTION_ID,
    institutionSlug: "banco-prueba",
    institutionName: "Banco de Prueba",
    institutionKind: "bank",
    institutionCountry: "CL",
    institutionUrl: null,
    accountId: "00000000-0000-4000-8000-0000000000f3",
    accountName: "Personal",
    productId: PRODUCT_ID,
    parentProductId: null,
    kind: "credit_card",
    productName: "Tarjeta de prueba",
    productSlug: "tarjeta-usd",
    currency: "USD",
    currentBalance: "1234.56",
    balanceAsOf: new Date("2026-07-10T15:00:00Z"),
    externalRef: null,
    attributes: {},
    metrics: null,
    isActive: true,
    ...overrides,
  };
}

function rateRow(overrides: Record<string, unknown> = {}) {
  return {
    institutionId: INSTITUTION_ID,
    institutionSlug: "banco-prueba",
    productId: PRODUCT_ID,
    currency: "USD",
    rate: "999.99000000",
    source: "Fuente de prueba",
    reportedAsOf: "2026-07-10",
    observedAt: new Date("2026-07-10T15:00:00Z"),
    ...overrides,
  };
}

function queue(product: unknown, rates: unknown[]) {
  const snapshots = [
    { balance: "1234.56", metrics: {}, asOf: new Date("2026-07-10T15:00:00Z") },
  ];
  queuedRows.push([product], snapshots, [], rates);
}

function call() {
  return GET(
    new Request(
      "https://dashboard.example/api/institutions/banco-prueba/products/tarjeta-usd"
    ),
    {
      params: Promise.resolve({ slug: "banco-prueba", product: "tarjeta-usd" }),
    }
  );
}

beforeEach(() => {
  queuedRows.length = 0;
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-07-11T16:00:00Z"));
  return () => vi.useRealTimers();
});

describe("GET /api/institutions/[slug]/products/[product] exchange rate", () => {
  it("converts with the product rate and reports it, stale after its day", async () => {
    queue(productRow(), [rateRow()]);
    const body = await (await call()).json();

    expect(body.product.currentBalanceClp).toBeCloseTo(1234.56 * 999.99);
    expect(body.history[0].balanceClp).toBeCloseTo(1234.56 * 999.99);
    expect(body.product.exchangeRate).toEqual({
      currency: "USD",
      rate: 999.99,
      scope: "product",
      source: "Fuente de prueba",
      observedAt: "2026-07-10T15:00:00.000Z",
      reportedAsOf: "2026-07-10",
      stale: true,
    });
  });

  it("falls back to the provider rate when the institution has none", async () => {
    queue(productRow(), []);
    const body = await (await call()).json();

    expect(body.product.currentBalanceClp).toBeCloseTo(1234.56 * 900);
    expect(body.product.exchangeRate).toMatchObject({
      scope: "provider",
      rate: 900,
      stale: false,
    });
  });

  it("has no exchange rate for a CLP product", async () => {
    queue(productRow({ currency: "CLP", currentBalance: "2500000" }), [
      rateRow(),
    ]);
    const body = await (await call()).json();

    expect(body.product.currentBalanceClp).toBe(2500000);
    expect(body.product.exchangeRate).toBeNull();
  });
});
