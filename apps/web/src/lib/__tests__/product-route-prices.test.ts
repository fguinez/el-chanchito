import { beforeEach, describe, expect, it, vi } from "vitest";

// Fake db, as in product-route-exchange-rate.test.ts: every select gets its own
// chain whose terminal step (`.limit()`, or awaiting the chain itself as the
// exchange_rates read does) resolves to the next queued rows, in query order:
// product, snapshots, transactions, prices (only for a ticker), then rates.
const queuedRows: unknown[][] = [];
const limits: number[] = [];

vi.mock("@/lib/db", () => {
  const next = () => Promise.resolve(queuedRows.shift() ?? []);
  const makeChain = () => {
    const chain: Record<string, unknown> = {};
    for (const step of ["from", "innerJoin", "where", "orderBy"]) {
      chain[step] = () => chain;
    }
    chain.limit = (n: number) => {
      limits.push(n);
      return next();
    };
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

// Synthetic fixtures only: fake ids and slugs, the ACME ticker, fake prices.
const INSTITUTION_ID = "00000000-0000-0000-0000-0000000000f1";
const PRODUCT_ID = "00000000-0000-0000-0000-0000000000f2";

function stockRow(overrides: Record<string, unknown> = {}) {
  return {
    institutionId: INSTITUTION_ID,
    institutionSlug: "fintual",
    institutionName: "Fintual",
    institutionKind: "fintech",
    institutionCountry: "CL",
    institutionUrl: null,
    accountId: "00000000-0000-0000-0000-0000000000f3",
    accountName: "Personal",
    productId: PRODUCT_ID,
    parentProductId: null,
    kind: "investment",
    productName: "Acme Corp",
    productSlug: "acme-corp",
    currency: "USD",
    currentBalance: "250.5",
    balanceAsOf: new Date("2026-01-05T15:00:00Z"),
    externalRef: "stock:0001",
    attributes: { kind: "investment", ticker: "ACME" },
    metrics: null,
    isActive: true,
    ...overrides,
  };
}

const RATE_ROW = {
  institutionId: INSTITUTION_ID,
  institutionSlug: "fintual",
  productId: PRODUCT_ID,
  currency: "USD",
  rate: "999.99000000",
  source: "Fuente de prueba",
  reportedAsOf: null,
  observedAt: new Date("2026-01-05T15:00:00Z"),
};

const SNAPSHOTS = [
  { balance: "250.5", metrics: {}, asOf: new Date("2026-01-05T15:00:00Z") },
];

function call() {
  return GET(
    new Request(
      "https://dashboard.example/api/institutions/fintual/products/acme-corp"
    ),
    { params: Promise.resolve({ slug: "fintual", product: "acme-corp" }) }
  );
}

beforeEach(() => {
  queuedRows.length = 0;
  limits.length = 0;
});

describe("GET /api/institutions/[slug]/products/[product] prices", () => {
  it("returns a stock's closes oldest first, read newest first under the cap", async () => {
    const newestFirst = [
      { date: "2026-01-06", price: "250.50000000" },
      { date: "2026-01-05", price: "100.00000000" },
      { date: "2026-01-02", price: "100.00000000" },
    ];
    queuedRows.push([stockRow()], SNAPSHOTS, [], newestFirst, [RATE_ROW]);

    const body = await (await call()).json();

    expect(body.prices).toEqual([
      { date: "2026-01-02", price: 100 },
      { date: "2026-01-05", price: 100 },
      { date: "2026-01-06", price: 250.5 },
    ]);
    expect(limits).toContain(1300);
    // The rates query still got its own rows after the prices one.
    expect(body.product.exchangeRate).toMatchObject({ scope: "product" });
  });

  it("is empty for a stock without stored closes yet", async () => {
    queuedRows.push([stockRow()], SNAPSHOTS, [], [], []);

    const body = await (await call()).json();

    expect(body.prices).toEqual([]);
  });

  it.each([
    ["no ticker attribute", {}],
    ["a blank ticker", { kind: "investment", ticker: "  " }],
    ["a null ticker", { kind: "investment", ticker: null }],
  ])("skips the query for a product with %s", async (_label, attributes) => {
    queuedRows.push([stockRow({ attributes })], SNAPSHOTS, [], [RATE_ROW]);

    const body = await (await call()).json();

    expect(body.prices).toEqual([]);
    expect(limits).not.toContain(1300);
    // The fourth queued rows went to the rates query: no prices query ran.
    expect(body.product.exchangeRate).toMatchObject({
      scope: "product",
      rate: 999.99,
    });
  });
});
