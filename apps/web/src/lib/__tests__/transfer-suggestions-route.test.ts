import { NextRequest } from "next/server";
import { PgDialect } from "drizzle-orm/pg-core";
import type { SQL } from "drizzle-orm";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { argOf, createFakeDb, FAKE_ROW_ID, type RecordedQuery } from "./fake-db";
import {
  confirmedTransferPatterns,
  dismissedTransferSuggestions,
  internalTransfers,
  products,
  transactions,
} from "@/lib/db/schema";

// The wiring of POST /api/transfers/suggestions/confirm and .../dismiss:
// bodies are checked before any query, every re-check runs before the first
// write, a confirmed pair becomes a resolved transfer with both rows flagged
// and a learned pattern, and an already linked row is a 409. The db is a
// recording double. Every id, amount and date is synthetic (see the repo's
// personal data policy): a $ 1.000.000 card payment between fake products.

const fake = createFakeDb();
vi.mock("@/lib/db", () => ({ db: fake.db }));

const confirm = await import("@/app/api/transfers/suggestions/confirm/route");
const dismiss = await import("@/app/api/transfers/suggestions/dismiss/route");

const CHECKING_ID = "11111111-1111-4111-8111-111111111111";
const CARD_ID = "22222222-2222-4222-8222-222222222222";
const FUND_ID = "33333333-3333-4333-8333-333333333333";
const USD_ID = "44444444-4444-4444-8444-444444444444";
const DEBIT_ID = "55555555-5555-4555-8555-555555555555";
const CREDIT_ID = "66666666-6666-4666-8666-666666666666";
const OTHER_TRANSFER_ID = "77777777-7777-4777-8777-777777777777";

const AMOUNT = 1_000_000;

interface World {
  txns: Record<string, unknown>[];
  products: { id: string; currency: string }[];
  feedProducts: string[];
  holders: Record<string, unknown>[];
  created: Record<string, unknown> | null;
  linkError?: Error;
}

let world: World;

const debit = {
  id: DEBIT_ID,
  productId: CHECKING_ID,
  amount: -AMOUNT,
  transactionDate: "2026-09-15",
  description: "Pago TC 1234",
};
const credit = {
  id: CREDIT_ID,
  productId: CARD_ID,
  amount: AMOUNT,
  transactionDate: "2026-09-16",
  description: "Pago recibido",
};

function table(query: RecordedQuery) {
  return query.op === "select" ? argOf(query, "from") : query.calls[0].args[0];
}

function params(query: RecordedQuery): unknown[] {
  const where = argOf(query, "where") as SQL | undefined;
  return where ? new PgDialect().sqlToQuery(where).params : [];
}

function respond(query: RecordedQuery): unknown {
  const target = table(query);
  if (query.op === "select" && target === transactions) {
    const ids = params(query);
    return world.txns.filter((t) => ids.includes(t.id));
  }
  if (query.op === "select" && target === products) {
    const ids = params(query);
    const fields = argOf(query, "select") as Record<string, unknown>;
    if ("currency" in fields) {
      return world.products.filter((p) => ids.includes(p.id));
    }
    return world.feedProducts.filter((id) => ids.includes(id)).map((id) => ({ id }));
  }
  if (query.op === "select" && target === internalTransfers) {
    if (query.calls.some((c) => c.method === "for")) {
      return world.created ? [world.created] : [];
    }
    if (query.calls.some((c) => c.method === "leftJoin")) {
      // loadTransfers, after the commit.
      return [
        {
          transfer: world.created,
          from: { id: null },
          to: { id: null },
          fromTxn: { id: null },
          toTxn: { id: null },
          fromHasFeed: true,
          toHasFeed: false,
        },
      ];
    }
    return world.holders;
  }
  if (query.op === "insert" && target === internalTransfers) {
    world.created = {
      id: FAKE_ROW_ID,
      fromTransactionId: null,
      toTransactionId: null,
      status: "pending",
      ...(argOf(query, "values") as object),
    };
    return [{ id: FAKE_ROW_ID }];
  }
  if (query.op === "update" && target === internalTransfers) {
    if (world.linkError) throw world.linkError;
    Object.assign(world.created ?? {}, argOf(query, "set"));
  }
  return [];
}

function post(
  handler: (request: NextRequest) => Promise<Response>,
  url: string,
  body: unknown
) {
  return handler(
    new NextRequest(new URL(`https://dashboard.example${url}`), {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    })
  );
}

const postConfirm = (body: unknown) =>
  post(confirm.POST, "/api/transfers/suggestions/confirm", body);
const postDismiss = (body: unknown) =>
  post(dismiss.POST, "/api/transfers/suggestions/dismiss", body);

function writes(op: "insert" | "update", target: unknown) {
  return fake.queries.filter((q) => q.op === op && table(q) === target);
}

beforeEach(() => {
  fake.reset();
  fake.respondWith(respond);
  world = {
    txns: [debit, credit],
    products: [
      { id: CHECKING_ID, currency: "CLP" },
      { id: CARD_ID, currency: "CLP" },
      { id: FUND_ID, currency: "CLP" },
      { id: USD_ID, currency: "USD" },
    ],
    feedProducts: [CHECKING_ID, CARD_ID, USD_ID],
    holders: [],
    created: null,
  };
});

describe("POST /api/transfers/suggestions/confirm", () => {
  it.each([
    ["no target", { sourceTransactionId: DEBIT_ID }, "targetTransactionId"],
    [
      "both targets",
      {
        sourceTransactionId: DEBIT_ID,
        targetTransactionId: CREDIT_ID,
        targetProductId: FUND_ID,
      },
      "targetProductId",
    ],
    [
      "one row on both sides",
      { sourceTransactionId: DEBIT_ID, targetTransactionId: DEBIT_ID.toUpperCase() },
      "targetTransactionId",
    ],
    [
      "a malformed id",
      { sourceTransactionId: "cargo-1", targetTransactionId: CREDIT_ID },
      "sourceTransactionId",
    ],
  ])("rejects %s before querying", async (_, body, field) => {
    const response = await postConfirm(body);

    expect(response.status).toBe(400);
    const json = await response.json();
    expect(json.field).toBe(field);
    expect(json.issues.length).toBeGreaterThan(0);
    expect(fake.queries).toHaveLength(0);
  });

  it("turns a pair into a resolved transfer, flags both rows and learns it", async () => {
    const response = await postConfirm({
      sourceTransactionId: DEBIT_ID,
      targetTransactionId: CREDIT_ID.toUpperCase(),
    });

    expect(response.status).toBe(201);
    await expect(response.json()).resolves.toMatchObject({
      id: FAKE_ROW_ID,
      status: "resolved",
    });
    const [insert] = writes("insert", internalTransfers);
    expect(argOf(insert, "values")).toEqual({
      description: "Pago TC 1234",
      amount: AMOUNT,
      fromProductId: CHECKING_ID,
      toProductId: CARD_ID,
      transferDate: "2026-09-15",
    });
    const [link] = writes("update", internalTransfers);
    expect(argOf(link, "set")).toMatchObject({
      fromTransactionId: DEBIT_ID,
      toTransactionId: CREDIT_ID,
      status: "resolved",
    });
    const [flag] = writes("update", transactions);
    expect(argOf(flag, "set")).toMatchObject({ isInternalTransfer: true });
    const [pattern] = writes("insert", confirmedTransferPatterns);
    expect(argOf(pattern, "values")).toEqual({
      sourceProductId: CHECKING_ID,
      targetProductId: CARD_ID,
      descriptionPrefix: "pago tc",
    });
  });

  it("links only the source of a one-sided suggestion and resolves it", async () => {
    const response = await postConfirm({
      sourceTransactionId: DEBIT_ID,
      targetProductId: FUND_ID,
    });

    expect(response.status).toBe(201);
    const [insert] = writes("insert", internalTransfers);
    expect(argOf(insert, "values")).toMatchObject({ toProductId: FUND_ID });
    const [link] = writes("update", internalTransfers);
    expect(argOf(link, "set")).toMatchObject({
      fromTransactionId: DEBIT_ID,
      toTransactionId: null,
      status: "resolved",
    });
  });

  it.each([
    [
      "a target amount off by a fee",
      () => {
        world.txns = [debit, { ...credit, amount: AMOUNT + 300 }];
      },
      { targetTransactionId: CREDIT_ID },
      "targetTransactionId",
    ],
    [
      "an incoming source",
      () => {
        world.txns = [{ ...debit, amount: AMOUNT }, credit];
      },
      { targetTransactionId: CREDIT_ID },
      "sourceTransactionId",
    ],
    [
      "an outgoing target",
      () => {
        world.txns = [debit, { ...credit, amount: -AMOUNT }];
      },
      { targetTransactionId: CREDIT_ID },
      "targetTransactionId",
    ],
    [
      "both rows on one product",
      () => {
        world.txns = [debit, { ...credit, productId: CHECKING_ID }];
      },
      { targetTransactionId: CREDIT_ID },
      "targetTransactionId",
    ],
    [
      "an unknown source row",
      () => {
        world.txns = [credit];
      },
      { targetTransactionId: CREDIT_ID },
      "sourceTransactionId",
    ],
    [
      "a one-sided target with a transaction feed",
      () => {},
      { targetProductId: CARD_ID },
      "targetProductId",
    ],
    [
      "a one-sided target in another currency",
      () => {},
      { targetProductId: USD_ID },
      "targetProductId",
    ],
    [
      "an unknown target product",
      () => {
        world.products = world.products.filter((p) => p.id !== FUND_ID);
      },
      { targetProductId: FUND_ID },
      "targetProductId",
    ],
  ])("refuses %s without writing", async (_, arrange, target, field) => {
    arrange();

    const response = await postConfirm({ sourceTransactionId: DEBIT_ID, ...target });

    expect(response.status).toBe(400);
    await expect(response.json()).resolves.toMatchObject({ field });
    expect(fake.queries.filter((q) => q.op !== "select")).toHaveLength(0);
  });

  it("answers 409 for a row another transfer already links", async () => {
    world.holders = [
      { id: OTHER_TRANSFER_ID, fromTransactionId: null, toTransactionId: CREDIT_ID },
    ];

    const response = await postConfirm({
      sourceTransactionId: DEBIT_ID,
      targetTransactionId: CREDIT_ID,
    });

    expect(response.status).toBe(409);
    await expect(response.json()).resolves.toMatchObject({
      error: "La transacción ya está vinculada a otro movimiento",
      field: "targetTransactionId",
    });
    expect(writes("insert", internalTransfers)).toHaveLength(0);
  });

  it("turns a lost race on the unique index into a 409", async () => {
    world.linkError = Object.assign(new Error("duplicate key"), {
      cause: Object.assign(new Error("duplicate key"), {
        code: "23505",
        constraint_name: "uq_internal_transfers_from_transaction",
      }),
    });

    const response = await postConfirm({
      sourceTransactionId: DEBIT_ID,
      targetTransactionId: CREDIT_ID,
    });

    expect(response.status).toBe(409);
    await expect(response.json()).resolves.toMatchObject({
      field: "sourceTransactionId",
    });
  });
});

describe("POST /api/transfers/suggestions/dismiss", () => {
  it.each([
    [{}, "sourceTransactionId"],
    [{ sourceTransactionId: "cargo-1" }, "sourceTransactionId"],
    [
      { sourceTransactionId: DEBIT_ID, targetTransactionId: DEBIT_ID },
      "targetTransactionId",
    ],
  ])("rejects %j before querying", async (body, field) => {
    const response = await postDismiss(body);

    expect(response.status).toBe(400);
    await expect(response.json()).resolves.toMatchObject({ field });
    expect(fake.queries).toHaveLength(0);
  });

  it("refuses an unknown row without writing", async () => {
    world.txns = [debit];

    const response = await postDismiss({
      sourceTransactionId: DEBIT_ID,
      targetTransactionId: CREDIT_ID,
    });

    expect(response.status).toBe(400);
    await expect(response.json()).resolves.toMatchObject({
      field: "targetTransactionId",
    });
    expect(writes("insert", dismissedTransferSuggestions)).toHaveLength(0);
  });

  it.each([
    ["a pair", { targetTransactionId: CREDIT_ID.toUpperCase() }, CREDIT_ID],
    ["a one-sided suggestion", {}, null],
    ["a one-sided suggestion with a null target", { targetTransactionId: null }, null],
  ])("remembers %s, ignoring a repeat", async (_, target, stored) => {
    const response = await postDismiss({ sourceTransactionId: DEBIT_ID, ...target });

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual({ ok: true });
    const [insert] = writes("insert", dismissedTransferSuggestions);
    expect(argOf(insert, "values")).toEqual({
      sourceTransactionId: DEBIT_ID,
      targetTransactionId: stored,
    });
    expect(insert.calls.some((c) => c.method === "onConflictDoNothing")).toBe(true);
  });
});
