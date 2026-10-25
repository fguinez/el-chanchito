import { NextRequest } from "next/server";
import { PgDialect } from "drizzle-orm/pg-core";
import type { SQL } from "drizzle-orm";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { argOf, createFakeDb, type RecordedQuery } from "./fake-db";
import { categories, categoryRules, transactions } from "@/lib/db/schema";

// The wiring of POST /api/transactions/[id]/category-rule: validation before
// any query, the keyword lock, the exact rule created or reused, this row
// marked manual and the rows
// with the same description updated, all on one transaction. The db is a
// recording double (the SQL itself runs in category-rule.db.test.ts). Every id
// and description is synthetic (see the repo's personal data policy).

const fake = createFakeDb();
// The fake transaction also needs `execute`, for the advisory lock.
const execute = vi.fn<(query: unknown) => Promise<unknown[]>>(async () => []);
vi.mock("@/lib/db", () => ({
  db: {
    ...fake.db,
    transaction: <T>(fn: (tx: unknown) => Promise<T>) =>
      fn({ ...fake.db, execute }),
  },
}));

const { POST } = await import("@/app/api/transactions/[id]/category-rule/route");

const TXN_ID = "11111111-1111-4111-8111-111111111111";
const CATEGORY_ID = "22222222-2222-4222-8222-222222222222";
const RULE_ID = "33333333-3333-4333-8333-333333333333";
const DESCRIPTION = "  COMERCIO EJEMPLO SPA ";

interface World {
  txn: { description: string } | null;
  category: boolean;
  existingRule: boolean;
  similarUpdated: number;
}

let world: World;

function table(query: RecordedQuery) {
  return query.op === "select" ? argOf(query, "from") : query.calls[0].args[0];
}

function respond(query: RecordedQuery): unknown {
  const target = table(query);
  if (query.op === "select" && target === transactions) {
    // The keyword column is PostgreSQL's lower(btrim(description)).
    return world.txn
      ? [{ ...world.txn, keyword: world.txn.description.replace(/^ +| +$/g, "").toLowerCase() }]
      : [];
  }
  if (query.op === "select" && target === categories) {
    return world.category ? [{ id: CATEGORY_ID }] : [];
  }
  if (query.op === "select" && target === categoryRules) {
    return world.existingRule ? [{ id: RULE_ID }] : [];
  }
  if (target === categoryRules) {
    const values = argOf(query, query.op === "insert" ? "values" : "set");
    return [{ id: RULE_ID, keyword: "comercio ejemplo spa", ...(values as object) }];
  }
  if (query.op === "update" && target === transactions) {
    // The similar-rows update is the one that asks for the ids back.
    if (query.calls.some((c) => c.method === "returning")) {
      return Array.from({ length: world.similarUpdated }, (_, i) => ({ id: `row-${i}` }));
    }
    return [];
  }
  return [];
}

beforeEach(() => {
  fake.reset();
  execute.mockClear();
  fake.respondWith(respond);
  world = {
    txn: { description: DESCRIPTION },
    category: true,
    existingRule: false,
    similarUpdated: 2,
  };
});

function post(id: string, body: unknown) {
  return POST(
    new NextRequest(
      new URL(`http://localhost:3000/api/transactions/${id}/category-rule`),
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: typeof body === "string" ? body : JSON.stringify(body),
      }
    ),
    { params: Promise.resolve({ id }) }
  );
}

function writes(op: "insert" | "update", target: unknown) {
  return fake.queries.filter((q) => q.op === op && table(q) === target);
}

describe("POST /api/transactions/[id]/category-rule", () => {
  it.each([
    ["a malformed id", "not-a-uuid", { categoryId: CATEGORY_ID }, "id"],
    ["a missing categoryId", TXN_ID, {}, "categoryId"],
    ["a malformed categoryId", TXN_ID, { categoryId: "nope" }, "categoryId"],
  ])("rejects %s with 400 before querying", async (_name, id, body, field) => {
    const res = await post(id, body);
    expect(res.status).toBe(400);
    expect((await res.json()).field).toBe(field);
    expect(fake.queries).toHaveLength(0);
    expect(execute).not.toHaveBeenCalled();
  });

  it("creates a rule from the trimmed description and applies it", async () => {
    const res = await post(TXN_ID, { categoryId: CATEGORY_ID });
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body).toEqual({
      rule: expect.objectContaining({
        id: RULE_ID,
        keyword: "comercio ejemplo spa",
        categoryId: CATEGORY_ID,
        matchType: "exact",
      }),
      ruleCreated: true,
      updatedCount: 2,
      transactionUpdated: true,
    });

    // The keyword is locked before the lookup, which only reuses exact rules.
    const lock = new PgDialect().sqlToQuery(execute.mock.calls[0][0] as SQL);
    expect(lock.sql).toContain("pg_advisory_xact_lock(hashtext(lower(");
    expect(lock.params).toEqual(["comercio ejemplo spa"]);
    const lookup = fake.queries.find(
      (q) => q.op === "select" && table(q) === categoryRules
    ) as RecordedQuery;
    expect(
      new PgDialect().sqlToQuery(argOf(lookup, "where") as SQL).sql
    ).toContain("match_type");

    const [insert] = writes("insert", categoryRules);
    expect(argOf(insert, "values")).toEqual({
      keyword: "comercio ejemplo spa",
      categoryId: CATEGORY_ID,
      matchType: "exact",
    });

    const [own, similar] = writes("update", transactions);
    expect(argOf(own, "set")).toEqual({
      categoryId: CATEGORY_ID,
      isManuallyCategorized: true,
      updatedAt: expect.any(Date),
    });
    // Other rows keep their manual flag as it was (false: manual ones are skipped).
    expect(argOf(similar, "set")).toEqual({
      categoryId: CATEGORY_ID,
      updatedAt: expect.any(Date),
    });
    const where = new PgDialect()
      .sqlToQuery(argOf(similar, "where") as SQL)
      .sql.toLowerCase();
    expect(where).toContain("deleted_at");
    expect(where).toContain("is_manually_categorized");
    expect(where).toContain("lower(btrim(");
  });

  it("reuses an existing rule with the same keyword", async () => {
    world.existingRule = true;
    const res = await post(TXN_ID, { categoryId: CATEGORY_ID });
    expect(res.status).toBe(200);
    expect((await res.json()).ruleCreated).toBe(false);
    expect(writes("insert", categoryRules)).toHaveLength(0);
    const [update] = writes("update", categoryRules);
    // Only the category moves; the rule keeps its priority.
    expect(argOf(update, "set")).toEqual({ categoryId: CATEGORY_ID });
  });

  it("returns 404 for an unknown or deleted transaction, writing nothing", async () => {
    world.txn = null;
    const res = await post(TXN_ID, { categoryId: CATEGORY_ID });
    expect(res.status).toBe(404);
    expect(fake.queries.every((q) => q.op === "select")).toBe(true);
  });

  it("returns 400 on categoryId for an unknown category", async () => {
    world.category = false;
    const res = await post(TXN_ID, { categoryId: CATEGORY_ID });
    expect(res.status).toBe(400);
    expect(await res.json()).toMatchObject({
      error: "La categoría no existe",
      field: "categoryId",
    });
    expect(fake.queries.every((q) => q.op === "select")).toBe(true);
  });

  it("returns 400 on description when it is too long for a rule", async () => {
    world.txn = { description: "X".repeat(101) };
    const res = await post(TXN_ID, { categoryId: CATEGORY_ID });
    expect(res.status).toBe(400);
    expect((await res.json()).field).toBe("description");
    expect(fake.queries.every((q) => q.op === "select")).toBe(true);
  });

  it("returns 500 when a write fails", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    fake.respondWith((query) => {
      if (query.op === "insert") throw new Error("synthetic failure");
      return respond(query);
    });
    const res = await post(TXN_ID, { categoryId: CATEGORY_ID });
    expect(res.status).toBe(500);
    expect(await res.json()).toEqual({ error: "Internal error" });
  });
});
