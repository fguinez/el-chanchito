import { NextRequest } from "next/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { argOf, createFakeDb, type RecordedQuery } from "./fake-db";
import { internalTransfers, products, transactions } from "@/lib/db/schema";

// The wiring of PUT /api/transfers/[id]/links and of DELETE /api/transfers:
// checks run before any write, the transfer, the flag on the linked rows and
// the status change together, and a unique violation is a 400 with a field.
// The db is a recording double. Every id, amount and date is synthetic (see
// the repo's personal data policy): a $ 1.000.000 transfer between two fake
// products.

const fake = createFakeDb();
vi.mock("@/lib/db", () => ({ db: fake.db }));

const { PUT } = await import("@/app/api/transfers/[id]/links/route");
const transfers = await import("@/app/api/transfers/route");

const TRANSFER_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const CHECKING_ID = "11111111-1111-4111-8111-111111111111";
const CARD_ID = "22222222-2222-4222-8222-222222222222";
const DEBIT_ID = "33333333-3333-4333-8333-333333333333";
const CREDIT_ID = "44444444-4444-4444-8444-444444444444";
const OTHER_TRANSFER_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

const AMOUNT = 1_000_000;

interface World {
  transfer: Record<string, unknown> | null;
  txns: { id: string; productId: string; amount: number }[];
  holders: Record<string, unknown>[];
  feedProducts: string[];
  updateError?: Error;
}

let world: World;

function storedTransfer(overrides: Record<string, unknown> = {}) {
  return {
    id: TRANSFER_ID,
    description: "Pago tarjeta de prueba",
    amount: AMOUNT,
    fromProductId: CHECKING_ID,
    toProductId: CARD_ID,
    fromTransactionId: null,
    toTransactionId: null,
    transferDate: "2026-09-15",
    status: "pending",
    notes: null,
    ...overrides,
  };
}

const debit = { id: DEBIT_ID, productId: CHECKING_ID, amount: -AMOUNT };
const credit = { id: CREDIT_ID, productId: CARD_ID, amount: AMOUNT };

function table(query: RecordedQuery) {
  return query.op === "select"
    ? argOf(query, "from")
    : query.calls[0].args[0];
}

function respond(query: RecordedQuery): unknown {
  const target = table(query);
  if (query.op === "select" && target === internalTransfers) {
    if (query.calls.some((c) => c.method === "for")) {
      return world.transfer ? [world.transfer] : [];
    }
    return world.holders;
  }
  if (query.op === "select" && target === products) {
    return world.feedProducts.map((id) => ({ id }));
  }
  if (query.op === "select" && target === transactions) {
    return world.txns;
  }
  if (query.op === "update" && target === internalTransfers && world.updateError) {
    throw world.updateError;
  }
  if (query.op === "delete") {
    return world.transfer
      ? [
          {
            fromTransactionId: world.transfer.fromTransactionId,
            toTransactionId: world.transfer.toTransactionId,
          },
        ]
      : [];
  }
  // loadTransfers' joined select starts from internal_transfers too, but
  // without `for`; it is answered above with the holders list, which these
  // tests leave empty or ignore.
  return [];
}

function put(body: unknown, id = TRANSFER_ID) {
  const request = new NextRequest(
    new URL(`https://dashboard.example/api/transfers/${id}/links`),
    {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }
  );
  return PUT(request, { params: Promise.resolve({ id }) });
}

function updatesOf(target: unknown) {
  return fake.queries.filter((q) => q.op === "update" && table(q) === target);
}

function setOf(query: RecordedQuery) {
  return argOf(query, "set") as Record<string, unknown>;
}

beforeEach(() => {
  fake.reset();
  fake.respondWith(respond);
  world = {
    transfer: storedTransfer(),
    txns: [debit, credit],
    holders: [],
    feedProducts: [CHECKING_ID, CARD_ID],
  };
});

describe("PUT /api/transfers/[id]/links", () => {
  it("rejects a malformed transfer id before querying", async () => {
    const response = await put({ fromTransactionId: DEBIT_ID }, "transfer-1");

    expect(response.status).toBe(400);
    await expect(response.json()).resolves.toMatchObject({ field: "id" });
    expect(fake.queries).toHaveLength(0);
  });

  it.each([
    [{}, ""],
    [{ fromTransactionId: "cargo-1" }, "fromTransactionId"],
    [{ toTransactionId: 42 }, "toTransactionId"],
  ])("rejects %j before querying", async (body, field) => {
    const response = await put(body);

    expect(response.status).toBe(400);
    const json = await response.json();
    expect(json.field ?? "").toBe(field);
    expect(fake.queries).toHaveLength(0);
  });

  it("answers 404 for an unknown transfer", async () => {
    world.transfer = null;

    const response = await put({ fromTransactionId: DEBIT_ID });

    expect(response.status).toBe(404);
    expect(updatesOf(internalTransfers)).toHaveLength(0);
  });

  it("links both sides, flags both rows and resolves the transfer", async () => {
    const response = await put({
      fromTransactionId: DEBIT_ID,
      toTransactionId: CREDIT_ID.toUpperCase(),
    });

    expect(response.status).toBe(200);
    const [transferUpdate] = updatesOf(internalTransfers);
    expect(setOf(transferUpdate)).toMatchObject({
      fromTransactionId: DEBIT_ID,
      toTransactionId: CREDIT_ID,
      status: "resolved",
    });
    const [flag] = updatesOf(transactions);
    expect(setOf(flag)).toMatchObject({ isInternalTransfer: true });
  });

  it("locks the rows it validates until the links are written", async () => {
    await put({ fromTransactionId: DEBIT_ID });

    const lookup = fake.queries.find(
      (q) => q.op === "select" && table(q) === transactions
    );
    expect(lookup && argOf(lookup, "for")).toBe("key share");
  });

  it("stays pending while a side with a feed is unlinked", async () => {
    await put({ fromTransactionId: DEBIT_ID });

    const [transferUpdate] = updatesOf(internalTransfers);
    expect(setOf(transferUpdate)).toMatchObject({
      fromTransactionId: DEBIT_ID,
      toTransactionId: null,
      status: "pending",
    });
  });

  it("resolves once the only side with a feed is linked", async () => {
    world.feedProducts = [CHECKING_ID];

    await put({ fromTransactionId: DEBIT_ID });

    expect(setOf(updatesOf(internalTransfers)[0]).status).toBe("resolved");
  });

  it("clears a link, unflags the released row and keeps the status", async () => {
    world.transfer = storedTransfer({
      fromTransactionId: DEBIT_ID,
      toTransactionId: CREDIT_ID,
      status: "resolved",
    });

    const response = await put({ toTransactionId: null });

    expect(response.status).toBe(200);
    expect(setOf(updatesOf(internalTransfers)[0])).toMatchObject({
      fromTransactionId: DEBIT_ID,
      toTransactionId: null,
      status: "resolved",
    });
    const flags = updatesOf(transactions).map(setOf);
    expect(flags).toEqual([
      expect.objectContaining({ isInternalTransfer: true }),
      expect.objectContaining({ isInternalTransfer: false }),
    ]);
  });

  it.each([
    [
      "a source row on another product",
      { fromTransactionId: CREDIT_ID },
      "fromTransactionId",
    ],
    [
      "a target row that is outgoing",
      { toTransactionId: DEBIT_ID },
      "toTransactionId",
    ],
  ])("refuses %s without writing", async (_, body, field) => {
    const response = await put(body);

    expect(response.status).toBe(400);
    const json = await response.json();
    expect(json.field).toBe(field);
    expect(json.issues.length).toBeGreaterThan(0);
    expect(fake.queries.filter((q) => q.op === "update")).toHaveLength(0);
  });

  it("refuses an amount that is off by a fee", async () => {
    world.txns = [{ ...debit, amount: -(AMOUNT + 300) }];

    const response = await put({ fromTransactionId: DEBIT_ID });

    expect(response.status).toBe(400);
    await expect(response.json()).resolves.toMatchObject({
      field: "fromTransactionId",
    });
  });

  it("keeps a side resubmitted unchanged even if it no longer matches", async () => {
    world.transfer = storedTransfer({ fromTransactionId: DEBIT_ID });
    world.txns = [{ ...debit, amount: -(AMOUNT + 300) }, credit];

    const response = await put({
      fromTransactionId: DEBIT_ID,
      toTransactionId: CREDIT_ID,
    });

    expect(response.status).toBe(200);
    expect(setOf(updatesOf(internalTransfers)[0])).toMatchObject({
      fromTransactionId: DEBIT_ID,
      toTransactionId: CREDIT_ID,
    });
  });

  it("refuses one row on both sides", async () => {
    world.transfer = storedTransfer({ fromTransactionId: DEBIT_ID });
    world.txns = [{ ...debit, productId: CARD_ID, amount: AMOUNT }];

    const response = await put({
      fromTransactionId: DEBIT_ID,
      toTransactionId: DEBIT_ID,
    });

    expect(response.status).toBe(400);
    await expect(response.json()).resolves.toMatchObject({
      field: "toTransactionId",
    });
    expect(updatesOf(internalTransfers)).toHaveLength(0);
  });

  it("refuses an unknown transaction", async () => {
    world.txns = [];

    const response = await put({ fromTransactionId: DEBIT_ID });

    expect(response.status).toBe(400);
    await expect(response.json()).resolves.toMatchObject({
      field: "fromTransactionId",
    });
  });

  it("refuses a row another transfer already links", async () => {
    world.holders = [
      { id: OTHER_TRANSFER_ID, fromTransactionId: null, toTransactionId: CREDIT_ID },
    ];

    const response = await put({ toTransactionId: CREDIT_ID });

    expect(response.status).toBe(400);
    await expect(response.json()).resolves.toMatchObject({
      error: "La transacción ya está vinculada a otro movimiento",
      field: "toTransactionId",
    });
    expect(updatesOf(internalTransfers)).toHaveLength(0);
  });

  it("turns a lost race on the unique index into a 400", async () => {
    world.updateError = Object.assign(new Error("duplicate key"), {
      cause: Object.assign(new Error("duplicate key"), {
        code: "23505",
        constraint_name: "uq_internal_transfers_from_transaction",
      }),
    });

    const response = await put({ fromTransactionId: DEBIT_ID });

    expect(response.status).toBe(400);
    await expect(response.json()).resolves.toMatchObject({
      field: "fromTransactionId",
    });
  });
});

describe("DELETE /api/transfers", () => {
  it("unflags the rows the deleted transfer linked", async () => {
    world.transfer = storedTransfer({
      fromTransactionId: DEBIT_ID,
      toTransactionId: CREDIT_ID,
    });

    const response = await transfers.DELETE(
      new NextRequest(new URL("https://dashboard.example/api/transfers"), {
        method: "DELETE",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ id: TRANSFER_ID }),
      })
    );

    expect(response.status).toBe(200);
    const [unflag] = updatesOf(transactions);
    expect(setOf(unflag)).toMatchObject({ isInternalTransfer: false });
  });

  it("touches no transaction when the transfer linked none", async () => {
    await transfers.DELETE(
      new NextRequest(new URL("https://dashboard.example/api/transfers"), {
        method: "DELETE",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ id: TRANSFER_ID }),
      })
    );

    expect(updatesOf(transactions)).toHaveLength(0);
  });
});
