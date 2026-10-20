import { NextRequest } from "next/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { argOf, createFakeDb, whereParams, type RecordedQuery } from "./fake-db";
import { monitorAdjustments, monitors, transactions } from "@/lib/db/schema";

// The wiring of POST /api/monitors/[id]/adjustments (link a transaction as a
// variación) and GET .../candidates (the transactions it can link). The db is
// a recording double. Every id, amount and date is synthetic (see the repo's
// personal data policy).

const fake = createFakeDb();
vi.mock("@/lib/db", () => ({ db: fake.db }));

const { POST } = await import("@/app/api/monitors/[id]/adjustments/route");
const { GET: candidatesGET } = await import(
  "@/app/api/monitors/[id]/adjustments/candidates/route"
);

const MONITOR_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const CHECKING_ID = "11111111-1111-4111-8111-111111111111";
const CARD_ID = "22222222-2222-4222-8222-222222222222";
const OTHER_ID = "33333333-3333-4333-8333-333333333333";
const TXN_ID = "44444444-4444-4444-8444-444444444444";
const TXN_2_ID = "55555555-5555-4555-8555-555555555555";
const TXN_3_ID = "66666666-6666-4666-8666-666666666666";

interface World {
  monitor: { expression: string; thresholds: { severity: string; expression: string }[] } | null;
  /** The monitor vanishes after this many monitor lookups (FK race). */
  monitorLookups?: number;
  txn: { productId: string; deletedAt: Date | null } | null;
  candidates: Record<string, unknown>[];
  links: { transactionId: string }[];
  insertError?: Error;
}

let world: World;

function table(query: RecordedQuery) {
  return query.op === "select" ? argOf(query, "from") : query.calls[0].args[0];
}

function respond(query: RecordedQuery): unknown {
  const target = table(query);
  if (query.op === "select" && target === monitors) {
    if (world.monitorLookups !== undefined && world.monitorLookups-- <= 0) {
      return [];
    }
    return world.monitor ? [world.monitor] : [];
  }
  if (query.op === "select" && target === transactions) {
    // POST's single lookup selects two columns; the candidates list more.
    const selected = query.calls[0].args[0] as Record<string, unknown>;
    if ("deletedAt" in selected) return world.txn ? [world.txn] : [];
    return world.candidates;
  }
  if (query.op === "select" && target === monitorAdjustments) {
    return world.links;
  }
  if (query.op === "insert") {
    if (world.insertError) throw world.insertError;
    return [
      {
        id: "77777777-7777-4777-8777-777777777777",
        ...(argOf(query, "values") as object),
      },
    ];
  }
  return [];
}

function candidate(
  id: string,
  description: string,
  amount: number,
  overrides: Record<string, unknown> = {}
) {
  return {
    id,
    description,
    rawDescription: description.toUpperCase(),
    transactionDate: "2026-07-10",
    amount,
    productId: CHECKING_ID,
    productName: "Cuenta corriente de prueba",
    productKind: "checking",
    productCurrency: "CLP",
    institutionSlug: "banco_demo",
    institutionName: "Banco Demo",
    ...overrides,
  };
}

function post(body: unknown, id = MONITOR_ID) {
  const request = new NextRequest(
    new URL(`https://dashboard.example/api/monitors/${id}/adjustments`),
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }
  );
  return POST(request, { params: Promise.resolve({ id }) });
}

function getCandidates(search = "", id = MONITOR_ID) {
  const request = new NextRequest(
    new URL(
      `https://dashboard.example/api/monitors/${id}/adjustments/candidates${search}`
    )
  );
  return candidatesGET(request, { params: Promise.resolve({ id }) });
}

beforeEach(() => {
  fake.reset();
  fake.respondWith(respond);
  world = {
    monitor: {
      expression: `@{${CHECKING_ID}:balance} - @{${CARD_ID}:owed}`,
      thresholds: [{ severity: "alert", expression: "1000000" }],
    },
    txn: { productId: CHECKING_ID, deletedAt: null },
    candidates: [],
    links: [],
  };
});

describe("POST /api/monitors/[id]/adjustments", () => {
  it("links a transaction of a referenced product", async () => {
    const response = await post({ transactionId: TXN_ID.toUpperCase() });

    expect(response.status).toBe(201);
    const inserts = fake.queries.filter((q) => q.op === "insert");
    expect(inserts).toHaveLength(1);
    expect(argOf(inserts[0], "values")).toEqual({
      monitorId: MONITOR_ID,
      transactionId: TXN_ID,
    });
  });

  it.each([
    [{}, "transactionId"],
    [{ transactionId: "txn-1" }, "transactionId"],
    [{ transactionId: TXN_ID, amount: 1000000 }, null],
  ])("validates %j before writing", async (body, field) => {
    const response = await post(body);
    if (field == null) {
      // Unknown keys are stripped: only the link is stored.
      expect(response.status).toBe(201);
      return;
    }
    expect(response.status).toBe(400);
    await expect(response.json()).resolves.toMatchObject({ field });
    expect(fake.queries).toHaveLength(0);
  });

  it("rejects a malformed monitor id before querying", async () => {
    const response = await post({ transactionId: TXN_ID }, "monitor-1");
    expect(response.status).toBe(400);
    expect(fake.queries).toHaveLength(0);
  });

  it("is a 404 when the monitor is gone", async () => {
    world.monitor = null;
    const response = await post({ transactionId: TXN_ID });
    expect(response.status).toBe(404);
  });

  it.each([
    ["missing", null],
    ["soft-deleted", { productId: CHECKING_ID, deletedAt: new Date() }],
  ])("rejects a %s transaction", async (_label, txn) => {
    world.txn = txn;
    const response = await post({ transactionId: TXN_ID });

    expect(response.status).toBe(400);
    await expect(response.json()).resolves.toMatchObject({
      field: "transactionId",
      issues: [{ path: "transactionId" }],
    });
    expect(fake.queries.some((q) => q.op === "insert")).toBe(false);
  });

  it("accepts a product referenced only by a threshold", async () => {
    world.monitor!.thresholds.push({
      severity: "warning",
      expression: `@{${OTHER_ID}:balance} * 0.5`,
    });
    world.txn = { productId: OTHER_ID, deletedAt: null };
    const response = await post({ transactionId: TXN_ID });
    expect(response.status).toBe(201);
  });

  it.each([
    ["the transaction vanished: a bad transactionId", undefined, 400],
    ["the monitor vanished: not found", 1, 404],
  ])("answers a late foreign-key failure when %s", async (_l, lookups, status) => {
    world.insertError = Object.assign(new Error("Failed query"), {
      cause: { code: "23503" },
    });
    world.monitorLookups = lookups;
    const response = await post({ transactionId: TXN_ID });

    expect(response.status).toBe(status);
    if (status === 400) {
      await expect(response.json()).resolves.toMatchObject({
        field: "transactionId",
      });
    }
  });

  it("rejects a transaction of a product the monitor does not read", async () => {
    world.txn = { productId: OTHER_ID, deletedAt: null };
    const response = await post({ transactionId: TXN_ID });

    expect(response.status).toBe(400);
    const body = await response.json();
    expect(body.field).toBe("transactionId");
    expect(body.error).toMatch(/not referenced/);
    expect(fake.queries.some((q) => q.op === "insert")).toBe(false);
  });

  it("is a 409 when the transaction is already linked", async () => {
    world.insertError = Object.assign(new Error("Failed query"), {
      cause: { code: "23505" },
    });
    const response = await post({ transactionId: TXN_ID });

    expect(response.status).toBe(409);
    await expect(response.json()).resolves.toMatchObject({
      field: "transactionId",
    });
  });
});

describe("GET /api/monitors/[id]/adjustments/candidates", () => {
  it("lists the month's rows of the referenced products, flagging linked ones", async () => {
    world.candidates = [
      candidate(TXN_ID, "Supermercado de prueba", -999999),
      candidate(TXN_2_ID, "Pago tarjeta de prueba", 1000000, {
        productId: CARD_ID,
        productName: "Banco Demo - credit_card",
        productKind: "credit_card",
      }),
    ];
    world.links = [{ transactionId: TXN_2_ID }];

    const response = await getCandidates("?month=2026-07");
    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body.month).toBe("2026-07");
    expect(body.productIds).toEqual([CHECKING_ID, CARD_ID]);
    expect(
      body.candidates.map((c: Record<string, unknown>) => [c.id, c.linked])
    ).toEqual([
      [TXN_ID, false],
      [TXN_2_ID, true],
    ]);
    // An auto-named product shows its kind label, as transaction lists do.
    expect(body.candidates[1].productName).toBe("Tarjeta de crédito");
    expect(body.candidates[0]).not.toHaveProperty("rawDescription");

    const list = fake.queries.find(
      (q) => q.op === "select" && table(q) === transactions
    )!;
    // The month window and the referenced products bound the query.
    expect(whereParams(list)).toEqual(
      expect.arrayContaining([CHECKING_ID, CARD_ID, "2026-07-01", "2026-08-01"])
    );
  });

  it("includes the products only a threshold references", async () => {
    world.monitor!.thresholds.push({
      severity: "warning",
      expression: `@{${OTHER_ID}:balance}`,
    });
    const body = await (await getCandidates()).json();
    expect(body.productIds).toEqual([CHECKING_ID, CARD_ID, OTHER_ID]);
  });

  it("filters by text ignoring case and accents, on either description", async () => {
    world.candidates = [
      candidate(TXN_ID, "Café de prueba", -2500),
      candidate(TXN_2_ID, "Alias de prueba", -1000000, {
        rawDescription: "COMPRA CAFE DEMO",
      }),
      candidate(TXN_3_ID, "Supermercado de prueba", -999999),
    ];

    const body = await (await getCandidates("?q=CAFÉ")).json();
    expect(body.candidates.map((c: { id: string }) => c.id)).toEqual([
      TXN_ID,
      TXN_2_ID,
    ]);
  });

  it("matches amounts when the search has no letters", async () => {
    world.candidates = [
      candidate(TXN_ID, "Compra de prueba", -2500000),
      candidate(TXN_2_ID, "Otra compra de prueba", -999999),
    ];
    const body = await (await getCandidates("?q=2.500.000")).json();
    expect(body.candidates.map((c: { id: string }) => c.id)).toEqual([TXN_ID]);
  });

  it("matches amounts with cents", async () => {
    world.candidates = [
      candidate(TXN_ID, "Compra de prueba", -1234.56, { productCurrency: "USD" }),
      candidate(TXN_2_ID, "Otra compra de prueba", -999999),
    ];
    const body = await (await getCandidates("?q=1.234,56")).json();
    expect(body.candidates.map((c: { id: string }) => c.id)).toEqual([TXN_ID]);
  });

  it("caps the list at the limit", async () => {
    world.candidates = [
      candidate(TXN_ID, "Uno de prueba", -1000),
      candidate(TXN_2_ID, "Dos de prueba", -2000),
    ];
    const body = await (await getCandidates("?limit=1")).json();
    expect(body.candidates).toHaveLength(1);
  });

  it("returns no candidates without querying when the expression reads no product", async () => {
    world.monitor = {
      expression: "DAY_OF_MONTH() * 1000",
      thresholds: [{ severity: "alert", expression: "1000000" }],
    };
    const body = await (await getCandidates()).json();

    expect(body).toMatchObject({ productIds: [], candidates: [] });
    expect(
      fake.queries.some((q) => q.op === "select" && table(q) === transactions)
    ).toBe(false);
  });

  it.each(["?month=2026-13", "?limit=0", "?limit=1000"])(
    "rejects %s",
    async (search) => {
      const response = await getCandidates(search);
      expect(response.status).toBe(400);
      expect(fake.queries).toHaveLength(0);
    }
  );

  it("is a 404 when the monitor is gone", async () => {
    world.monitor = null;
    expect((await getCandidates()).status).toBe(404);
  });
});
