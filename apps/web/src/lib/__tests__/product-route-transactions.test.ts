import { beforeEach, describe, expect, it, vi } from "vitest";

// Fake db, as in product-route-exchange-rate.test.ts: every select gets its own
// chain whose terminal step (`.limit()`, or awaiting the chain itself as the
// exchange_rates read does) resolves to the next queued rows, in query order:
// product, snapshots, transactions, then rates. Each select's field map is
// kept too, so a test can check what a column reads.
const queuedRows: unknown[][] = [];
const selectedFields: Record<string, unknown>[] = [];

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
  return {
    db: {
      select: (fields: Record<string, unknown>) => {
        selectedFields.push(fields);
        return makeChain();
      },
    },
  };
});

// Provider rates without the network.
vi.mock("@/lib/rates", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/rates")>()),
  getClpRates: async () => ({ CLP: 1 }),
}));

const { GET } = await import(
  "@/app/api/institutions/[slug]/products/[product]/route"
);
const { transactions } = await import("@/lib/db/schema");
const { displayDescription } = await import("@/lib/db/transactions");

// Synthetic fixtures only: fake ids, slugs, descriptions and amounts.
const PRODUCT_ID = "00000000-0000-4000-8000-0000000000f2";

const PRODUCT_ROW = {
  institutionId: "00000000-0000-4000-8000-0000000000f1",
  institutionSlug: "banco-prueba",
  institutionName: "Banco de Prueba",
  institutionKind: "bank",
  institutionCountry: "CL",
  institutionUrl: null,
  accountId: "00000000-0000-4000-8000-0000000000f3",
  accountName: "Personal",
  productId: PRODUCT_ID,
  parentProductId: null,
  kind: "checking",
  productName: "Cuenta de prueba",
  productSlug: "cuenta-prueba",
  currency: "CLP",
  currentBalance: "2500000",
  balanceAsOf: new Date("2026-07-10T15:00:00Z"),
  externalRef: null,
  attributes: {},
  metrics: null,
  isActive: true,
};

/** A transaction row as the route selects it. */
function txnRow(id: string, description: string, rawDescription: string) {
  return {
    id,
    description,
    rawDescription,
    amount: "-999999",
    transactionDate: "2026-07-09",
    source: "scraper",
    externalId: null,
    isInternalTransfer: false,
    locationName: null,
    locationCity: null,
    locationCountry: null,
  };
}

function call() {
  return GET(
    new Request(
      "https://dashboard.example/api/institutions/banco-prueba/products/cuenta-prueba"
    ),
    {
      params: Promise.resolve({ slug: "banco-prueba", product: "cuenta-prueba" }),
    }
  );
}

beforeEach(() => {
  queuedRows.length = 0;
  selectedFields.length = 0;
});

describe("GET /api/institutions/[slug]/products/[product] transactions", () => {
  it("returns each row's display and raw description", async () => {
    queuedRows.push(
      [PRODUCT_ROW],
      [],
      [
        txnRow(
          "00000000-0000-4000-8000-000000000011",
          "Café de prueba",
          "COMPRA CAFE EJEMPLO"
        ),
      ],
      []
    );

    const body = await (await call()).json();

    expect(body.transactions).toHaveLength(1);
    expect(body.transactions[0]).toMatchObject({
      description: "Café de prueba",
      rawDescription: "COMPRA CAFE EJEMPLO",
      amount: -999999,
    });
    // The transactions read takes rawDescription from the scraped column, not
    // from the resolved display text.
    const txnFields = selectedFields.find((fields) => "rawDescription" in fields);
    expect(txnFields?.rawDescription).toBe(transactions.description);
    expect(txnFields?.description).toBe(displayDescription);
  });
});
