import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Fake db, as in product-route-transactions.test.ts: every select gets its
// own chain whose terminal step (`.limit()`, or awaiting the chain itself as
// the exchange_rates read does) resolves to the next queued rows, in query
// order: product, snapshots, transactions (newest first), then rates. The
// billing block adds no query. Each select's field map is kept too.
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

// Synthetic fixtures only: fake ids, slugs, card last-4 1234, invented dates
// and amounts from the repo's fake set.
const PRODUCT_ID = "00000000-0000-4000-8000-0000000000f2";

function productRow(overrides: Record<string, unknown> = {}) {
  return {
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
    kind: "credit_card",
    productName: "Tarjeta de prueba",
    productDefaultName: "Tarjeta de prueba",
    productSlug: "tarjeta-prueba",
    currency: "CLP",
    currentBalance: "2500000",
    balanceAsOf: new Date("2026-10-30T15:00:00Z"),
    externalRef: null,
    attributes: { kind: "credit_card", last4: "1234", statement_day: 15, due_day: 5 },
    metrics: {
      kind: "credit_card",
      available: 2500000,
      statement_date: "2026-10-15",
      next_statement_date: "2026-11-14",
      payment_due_date: "2026-11-05",
      billed_amount: 1000000,
      minimum_payment: 999999,
    },
    isActive: true,
    displayOrder: 0,
    ...overrides,
  };
}

let nextId = 0;

/** A transaction row as the route selects it. */
function txnRow(
  transactionDate: string,
  amount: string,
  billingStatus: "unbilled" | "billed" | null = null,
  statementDate: string | null = null
) {
  nextId += 1;
  return {
    id: `00000000-0000-4000-8000-${String(nextId).padStart(12, "0")}`,
    description: "COMPRA DE PRUEBA",
    rawDescription: "COMPRA DE PRUEBA",
    amount,
    transactionDate,
    source: "scraper",
    externalId: null,
    isInternalTransfer: false,
    locationName: null,
    locationCity: null,
    locationCountry: null,
    billingStatus,
    statementDate,
  };
}

function call() {
  return GET(
    new Request(
      "https://dashboard.example/api/institutions/banco-prueba/products/tarjeta-prueba"
    ),
    {
      params: Promise.resolve({ slug: "banco-prueba", product: "tarjeta-prueba" }),
    }
  );
}

beforeEach(() => {
  queuedRows.length = 0;
  selectedFields.length = 0;
  // 23:00 on 2026-10-31 in Santiago, already 2026-11-01 in UTC: the billing
  // day must be Santiago's.
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-11-01T02:00:00Z"));
});

afterEach(() => {
  vi.useRealTimers();
});

describe("GET /api/institutions/[slug]/products/[product] billing", () => {
  it("returns a credit card's billing and each movement's status", async () => {
    queuedRows.push(
      [productRow()],
      [],
      // Newest first, as the route orders them.
      [
        txnRow("2026-10-25", "999999", "unbilled"),
        txnRow("2026-10-20", "-999999", "unbilled"),
        txnRow("2026-10-18", "-1000000"),
        txnRow("2026-10-10", "-2500000", "billed", "2026-10-15"),
      ],
      []
    );

    const body = await (await call()).json();

    expect(body.billing).toEqual({
      asOf: "2026-10-31",
      cycle: {
        lastClosing: { date: "2026-10-15", estimated: false },
        previousClosing: "2026-09-15",
        nextClosing: { date: "2026-11-14", estimated: false },
        dueDate: { date: "2026-11-05", estimated: false },
        basis: "statement",
        rolledOver: false,
      },
      statement: {
        date: "2026-10-15",
        billedAmount: 1000000,
        minimumPayment: 999999,
        paymentDueDate: "2026-11-05",
      },
      paidSinceClosing: 999999,
      billedPending: 1,
      billedPendingEstimated: true,
      hasBilledDebt: true,
      unbilledCharges: 1999999,
      unbilledCount: 2,
      statusCount: 4,
      complete: true,
    });
    // Oldest first, like the rest of the list.
    expect(
      body.transactions.map((t: { transactionDate: string; billing: unknown }) => [
        t.transactionDate,
        t.billing,
      ])
    ).toEqual([
      [
        "2026-10-10",
        { status: "billed", estimated: false, statementDate: "2026-10-15" },
      ],
      ["2026-10-18", { status: "unbilled", estimated: true, statementDate: null }],
      ["2026-10-20", { status: "unbilled", estimated: false, statementDate: null }],
      ["2026-10-25", { status: "unbilled", estimated: false, statementDate: null }],
    ]);

    const txnFields = selectedFields.find((fields) => "billingStatus" in fields);
    expect(txnFields?.billingStatus).toBe(transactions.billingStatus);
    expect(txnFields?.statementDate).toBe(transactions.statementDate);
  });

  it("marks the totals incomplete when the cut list misses the open cycles", async () => {
    // One more row than the route keeps, all after the previous closing.
    const rows = Array.from({ length: 501 }, () =>
      txnRow("2026-10-20", "-999999", "unbilled")
    );
    queuedRows.push([productRow()], [], rows, []);

    const body = await (await call()).json();

    expect(body.transactionsTruncated).toBe(true);
    expect(body.transactions).toHaveLength(500);
    expect(body.billing).toMatchObject({
      unbilledCount: 500,
      unbilledCharges: 499999500,
      complete: false,
    });
  });

  it("returns no billing for any other kind", async () => {
    queuedRows.push(
      [productRow({ kind: "line_of_credit", metrics: null, attributes: {} })],
      [],
      [txnRow("2026-10-20", "-999999", "unbilled")],
      []
    );

    const body = await (await call()).json();

    expect(body.billing).toBeNull();
    expect(body.transactions[0].billing).toBeNull();
  });
});
