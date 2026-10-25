import { NextRequest } from "next/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { argOf, createFakeDb, whereParams, type RecordedQuery } from "./fake-db";
import {
  categories,
  dismissedCategorySuggestions,
  transactions,
} from "@/lib/db/schema";

// The wiring of the category suggestion routes: validation before any query,
// the dismissals filtering the list, the one-off apply marking rows manual,
// and the dismissal stored under the normalised description. The db is a
// recording double (the SQL itself runs in category-suggestions.db.test.ts).
// Every id and description is synthetic (see the repo's personal data
// policy).

const fake = createFakeDb();
vi.mock("@/lib/db", () => ({ db: fake.db }));

const list = await import("@/app/api/categories/suggestions/route");
const apply = await import("@/app/api/categories/suggestions/apply/route");
const dismiss = await import("@/app/api/categories/suggestions/dismiss/route");
const forTxn = await import("@/app/api/transactions/[id]/category-suggestion/route");

const TXN_ID = "11111111-1111-4111-8111-111111111111";
const FOOD = "22222222-2222-4222-8222-222222222222";
const SUPER = "33333333-3333-4333-8333-333333333333";

interface World {
  history: { description: string; categoryId: string; count: number }[];
  targets: {
    description: string;
    count: number;
    latestDate: string;
    sampleTransactionId: string;
  }[];
  dismissed: { normalizedDescription: string; categoryId: string }[];
  category: boolean;
  txn: { description: string; categoryId: string | null; isManuallyCategorized: boolean } | null;
  updated: number;
}

let world: World;

function table(query: RecordedQuery) {
  return query.op === "select" ? argOf(query, "from") : query.calls[0].args[0];
}

function respond(query: RecordedQuery): unknown {
  const target = table(query);
  if (query.op === "select" && target === categories) {
    const fields = argOf(query, "select") as Record<string, unknown>;
    // The name map selects every category; the existence check one id.
    if ("name" in fields) {
      return [
        { id: FOOD, name: "Comida" },
        { id: SUPER, name: "Supermercado" },
      ];
    }
    return world.category ? [{ id: FOOD }] : [];
  }
  if (query.op === "select" && target === dismissedCategorySuggestions) {
    return world.dismissed;
  }
  if (query.op === "select" && target === transactions) {
    const fields = argOf(query, "select") as Record<string, unknown>;
    if ("sampleTransactionId" in fields) return world.targets;
    if ("count" in fields) return world.history;
    return world.txn ? [world.txn] : [];
  }
  if (query.op === "update" && target === transactions) {
    return Array.from({ length: world.updated }, (_, i) => ({ id: `row-${i}` }));
  }
  return [];
}

beforeEach(() => {
  fake.reset();
  fake.respondWith(respond);
  world = {
    history: [
      { description: "comercio ejemplo spa 1234", categoryId: FOOD, count: 3 },
      { description: "supermercado ficticio ltda", categoryId: SUPER, count: 2 },
    ],
    targets: [
      {
        description: "COMERCIO EJEMPLO SPA 9999",
        count: 2,
        latestDate: "2026-01-15",
        sampleTransactionId: TXN_ID,
      },
      {
        description: "SUPERMERCADO FICTICIO LTDA",
        count: 1,
        latestDate: "2026-01-10",
        sampleTransactionId: "44444444-4444-4444-8444-444444444444",
      },
      {
        description: "LIBRERIA INVENTADA",
        count: 5,
        latestDate: "2026-01-12",
        sampleTransactionId: "55555555-5555-4555-8555-555555555555",
      },
    ],
    dismissed: [],
    category: true,
    txn: { description: "COMERCIO EJEMPLO SPA 1", categoryId: null, isManuallyCategorized: false },
    updated: 2,
  };
});

function post(handler: (r: NextRequest) => Promise<Response>, body: unknown) {
  return handler(
    new NextRequest(new URL("http://localhost:3000/api/categories/suggestions/x"), {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: typeof body === "string" ? body : JSON.stringify(body),
    })
  );
}

function getForTxn(id: string) {
  return forTxn.GET(
    new NextRequest(
      new URL(`http://localhost:3000/api/transactions/${id}/category-suggestion`)
    ),
    { params: Promise.resolve({ id }) }
  );
}

describe("GET /api/categories/suggestions", () => {
  it("lists the descriptions with a confident suggestion", async () => {
    const res = await list.GET();
    expect(res.status).toBe(200);
    const { suggestions } = await res.json();
    expect(suggestions).toEqual([
      expect.objectContaining({
        description: "COMERCIO EJEMPLO SPA 9999",
        count: 2,
        sampleTransactionId: TXN_ID,
        categoryId: FOOD,
        categoryName: "Comida",
        exact: true,
        reason: "3 transacciones con la misma descripción están en Comida",
      }),
      expect.objectContaining({
        description: "SUPERMERCADO FICTICIO LTDA",
        categoryId: SUPER,
      }),
    ]);
  });

  it("leaves out a dismissed suggestion", async () => {
    world.dismissed = [{ normalizedDescription: "comercio ejemplo spa", categoryId: FOOD }];
    const { suggestions } = await (await list.GET()).json();
    expect(suggestions.map((s: { categoryId: string }) => s.categoryId)).toEqual([SUPER]);
  });

  it("shows it again when it points at another category", async () => {
    world.dismissed = [{ normalizedDescription: "comercio ejemplo spa", categoryId: SUPER }];
    const { suggestions } = await (await list.GET()).json();
    expect(suggestions).toHaveLength(2);
  });
});

describe("GET /api/transactions/[id]/category-suggestion", () => {
  it("rejects a malformed id before querying", async () => {
    const res = await getForTxn("not-a-uuid");
    expect(res.status).toBe(400);
    expect((await res.json()).field).toBe("id");
    expect(fake.queries).toHaveLength(0);
  });

  it("404s for an unknown or deleted row", async () => {
    world.txn = null;
    expect((await getForTxn(TXN_ID)).status).toBe(404);
  });

  it("suggests for an uncategorized row", async () => {
    const res = await getForTxn(TXN_ID);
    expect(res.status).toBe(200);
    expect((await res.json()).suggestion).toMatchObject({
      categoryId: FOOD,
      categoryName: "Comida",
    });
  });

  it.each([
    ["a categorized row", { categoryId: SUPER, isManuallyCategorized: false }],
    ["a row cleared by hand", { categoryId: null, isManuallyCategorized: true }],
  ])("returns null for %s without loading the history", async (_name, extra) => {
    world.txn = { description: "COMERCIO EJEMPLO SPA 1", ...extra };
    const res = await getForTxn(TXN_ID);
    expect(await res.json()).toEqual({ suggestion: null });
    expect(fake.queries).toHaveLength(1);
  });

  it("returns null for a dismissed suggestion", async () => {
    world.dismissed = [{ normalizedDescription: "comercio ejemplo spa", categoryId: FOOD }];
    expect(await (await getForTxn(TXN_ID)).json()).toEqual({ suggestion: null });
  });
});

describe("POST /api/categories/suggestions/apply", () => {
  it.each([
    ["a missing description", { categoryId: FOOD }, "description"],
    ["a blank description", { description: "  ", categoryId: FOOD }, "description"],
    ["a malformed categoryId", { description: "COMERCIO", categoryId: "nope" }, "categoryId"],
  ])("rejects %s with 400 before querying", async (_name, body, field) => {
    const res = await post(apply.POST, body);
    expect(res.status).toBe(400);
    expect((await res.json()).field).toBe(field);
    expect(fake.queries).toHaveLength(0);
  });

  it("rejects an unknown category without writing", async () => {
    world.category = false;
    const res = await post(apply.POST, { description: "COMERCIO", categoryId: FOOD });
    expect(res.status).toBe(400);
    expect(await res.json()).toMatchObject({
      error: "La categoría no existe",
      field: "categoryId",
    });
    expect(fake.queries.filter((q) => q.op === "update")).toHaveLength(0);
  });

  it("marks the rows with that description manual", async () => {
    const res = await post(apply.POST, {
      description: " COMERCIO EJEMPLO SPA 9999 ",
      categoryId: FOOD.toUpperCase(),
    });
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ updatedCount: 2 });
    const [update] = fake.queries.filter((q) => q.op === "update");
    expect(argOf(update, "set")).toMatchObject({
      categoryId: FOOD,
      isManuallyCategorized: true,
    });
    expect(whereParams(update)).toContain(" COMERCIO EJEMPLO SPA 9999 ");
  });
});

describe("POST /api/categories/suggestions/dismiss", () => {
  it("rejects a description with no words left", async () => {
    const res = await post(dismiss.POST, { description: "12345", categoryId: FOOD });
    expect(res.status).toBe(400);
    expect((await res.json()).field).toBe("description");
    expect(fake.queries).toHaveLength(0);
  });

  it("rejects an unknown category", async () => {
    world.category = false;
    const res = await post(dismiss.POST, { description: "COMERCIO", categoryId: FOOD });
    expect(res.status).toBe(400);
    expect((await res.json()).field).toBe("categoryId");
    expect(fake.queries.filter((q) => q.op === "insert")).toHaveLength(0);
  });

  it("stores the normalised description and the category", async () => {
    const res = await post(dismiss.POST, {
      description: "Comercio Ejemplo SpA #5678",
      categoryId: FOOD,
    });
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true });
    const [insert] = fake.queries.filter((q) => q.op === "insert");
    expect(table(insert)).toBe(dismissedCategorySuggestions);
    expect(argOf(insert, "values")).toEqual({
      normalizedDescription: "comercio ejemplo spa",
      categoryId: FOOD,
    });
    expect(insert.calls.some((c) => c.method === "onConflictDoNothing")).toBe(true);
  });
});
