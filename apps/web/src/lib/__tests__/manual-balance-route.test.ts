import { NextRequest } from "next/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { products, productSnapshots } from "@/lib/db/schema";

// The db module is replaced by a recording double shaped like the drizzle
// chains the route uses: the product lookup resolves to `lookupRows`, and the
// transaction records every insert/update so the assertions can check what
// would have been written. Synthetic ids and amounts only.

const PRODUCT_ID = "00000000-0000-4000-8000-000000000001";

let lookupRows: unknown[] = [];
const writes: { op: "insert" | "update"; table: unknown; values: unknown }[] = [];
const selectMock = vi.fn();
const transactionMock = vi.fn();

const lookupChain = {
  from: () => lookupChain,
  innerJoin: () => lookupChain,
  where: () => lookupChain,
  limit: async () => lookupRows,
};

const tx = {
  insert: (table: unknown) => ({
    values: async (values: unknown) => {
      writes.push({ op: "insert", table, values });
    },
  }),
  update: (table: unknown) => ({
    set: (values: unknown) => ({
      where: async () => {
        writes.push({ op: "update", table, values });
      },
    }),
  }),
};

vi.mock("@/lib/db", () => ({
  db: {
    select: (...args: unknown[]) => {
      selectMock(...args);
      return lookupChain;
    },
    transaction: async (fn: (t: typeof tx) => Promise<void>) => {
      transactionMock();
      await fn(tx);
    },
  },
}));

const { POST } = await import(
  "@/app/api/institutions/[slug]/products/[product]/balance/route"
);

const context = { params: Promise.resolve({ slug: "mach", product: "billetera" }) };

function balanceRequest(body: string) {
  return new NextRequest(
    new URL("http://localhost:3000/api/institutions/mach/products/billetera/balance"),
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body,
    }
  );
}

function productRow(overrides: Record<string, unknown> = {}) {
  return {
    productId: PRODUCT_ID,
    kind: "wallet",
    currentBalance: "1000000",
    isActive: true,
    ...overrides,
  };
}

beforeEach(() => {
  lookupRows = [];
  writes.length = 0;
  selectMock.mockReset();
  transactionMock.mockReset();
});

describe("POST /api/institutions/[slug]/products/[product]/balance", () => {
  it.each([
    ["malformed JSON", "{not json"],
    ["a decimal amount", JSON.stringify({ balance: 999_999.5 })],
    ["a negative amount", JSON.stringify({ balance: -1 })],
    ["a missing balance", JSON.stringify({})],
  ])("answers 400 for %s without touching the db", async (_label, body) => {
    const response = await POST(balanceRequest(body), context);

    expect(response.status).toBe(400);
    expect(selectMock).not.toHaveBeenCalled();
    expect(transactionMock).not.toHaveBeenCalled();
  });

  it.each([
    ["an unknown product", []],
    ["a retired ghost", [productRow({ isActive: false, currentBalance: null })]],
  ])("answers 404 for %s", async (_label, rows) => {
    lookupRows = rows;
    const response = await POST(
      balanceRequest(JSON.stringify({ balance: 2_500_000 })),
      context
    );

    expect(response.status).toBe(404);
    expect(transactionMock).not.toHaveBeenCalled();
  });

  it("answers 409 for an inactive product that still has a balance", async () => {
    lookupRows = [productRow({ isActive: false })];
    const response = await POST(
      balanceRequest(JSON.stringify({ balance: 2_500_000 })),
      context
    );

    expect(response.status).toBe(409);
    expect(transactionMock).not.toHaveBeenCalled();
  });

  it("answers 400 for a kind that doesn't accept manual balances", async () => {
    lookupRows = [productRow({ kind: "checking" })];
    const response = await POST(
      balanceRequest(JSON.stringify({ balance: 2_500_000 })),
      context
    );

    expect(response.status).toBe(400);
    expect(transactionMock).not.toHaveBeenCalled();
  });

  it("records a manual snapshot and the latest balance in one transaction", async () => {
    lookupRows = [productRow()];
    const response = await POST(
      balanceRequest(JSON.stringify({ balance: 2_500_000 })),
      context
    );

    expect(response.status).toBe(201);
    expect(transactionMock).toHaveBeenCalledTimes(1);
    expect(writes.map((w) => [w.op, w.table])).toEqual([
      ["insert", productSnapshots],
      ["update", products],
    ]);

    const metrics = { kind: "wallet", balance: 2_500_000 };
    const snapshot = writes[0].values as Record<string, unknown>;
    expect(snapshot).toMatchObject({
      productId: PRODUCT_ID,
      balance: "2500000",
      metrics,
      source: "manual",
    });
    const update = writes[1].values as Record<string, unknown>;
    expect(update).toMatchObject({ currentBalance: "2500000", metrics });
    // Both writes share one timestamp, which the response echoes.
    expect(update.balanceAsOf).toBe(snapshot.asOf);
    expect(update.updatedAt).toBe(snapshot.asOf);

    await expect(response.json()).resolves.toEqual({
      asOf: (snapshot.asOf as Date).toISOString(),
      balance: 2_500_000,
      source: "manual",
    });
  });

  it("records an explicit zero", async () => {
    lookupRows = [productRow()];
    const response = await POST(
      balanceRequest(JSON.stringify({ balance: 0 })),
      context
    );

    expect(response.status).toBe(201);
    expect(writes[0].values).toMatchObject({
      balance: "0",
      metrics: { kind: "wallet", balance: 0 },
    });
  });
});
