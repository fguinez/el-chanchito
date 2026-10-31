import { beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";
import { PgDialect } from "drizzle-orm/pg-core";
import type { SQL } from "drizzle-orm";
import { argOf, createFakeDb, whereParams, type RecordedQuery } from "./fake-db";

// GET /api/categories/rules/preview against the recording fake db: the `where`
// the count and the rows selects share, with and without a search. Its
// matching runs against a real PostgreSQL in category-rule.db.test.ts. Every
// keyword and description is synthetic.
const fake = createFakeDb();
vi.mock("@/lib/db", () => ({ db: fake.db }));

const { GET } = await import("@/app/api/categories/rules/preview/route");

function get(query: string) {
  return GET(
    new NextRequest(`https://dashboard.example/api/categories/rules/preview?${query}`)
  );
}

/** Every recorded select (the counts and the rows) with its where SQL. */
function selects(): { query: RecordedQuery; sql: string }[] {
  return fake.queries
    .filter((q) => q.op === "select")
    .map((query) => ({
      query,
      sql: new PgDialect()
        .sqlToQuery(argOf(query, "where") as SQL)
        .sql.toLowerCase(),
    }));
}

beforeEach(() => {
  fake.reset();
  // The counts select has no limit; the rows select does.
  fake.respondWith((query) => {
    if (query.op !== "select") return [];
    const hasLimit = query.calls.some((c) => c.method === "limit");
    return hasLimit ? [] : [{ total: 0, uncategorized: 0 }];
  });
});

describe("GET /api/categories/rules/preview search", () => {
  it("narrows the counts and the rows with q, on either description", async () => {
    const res = await get("keyword=comercio&q=%20Caf%C3%A9%20PRUEBA%20");

    expect(res.status).toBe(200);
    const all = selects();
    expect(all).toHaveLength(2);
    for (const { query, sql } of all) {
      // The rule's own strpos, then one per search word and field.
      expect(sql.match(/strpos\(/g)).toHaveLength(5);
      expect(sql).toContain("normalize(");
      expect(sql).toContain("alias_for_transaction(");
      expect(sql).toContain("deleted_at");
      expect(whereParams(query)).toEqual(
        expect.arrayContaining(["comercio", "cafe", "prueba"])
      );
      // The display description reads products.currency: both join products.
      expect(query.calls.some((c) => c.method === "innerJoin")).toBe(true);
    }
    const rows = all.find(({ query }) =>
      query.calls.some((c) => c.method === "limit")
    )!.query;
    expect(rows.calls[0].args[0]).toHaveProperty("displayDescription");
  });

  it("keeps the rule's own match on the raw description", async () => {
    await get("keyword=comercio&matchType=exact&q=prueba");

    for (const { sql } of selects()) {
      expect(sql).toContain('lower(btrim("transactions"."description"))');
    }
  });

  it.each(["", "&q=", "&q=%20%20"])("adds nothing for no search (%s)", async (q) => {
    const res = await get(`keyword=comercio${q}`);

    expect(res.status).toBe(200);
    for (const { sql } of selects()) expect(sql).not.toContain("normalize(");
  });

  it("answers 400 to a q over the length cap without querying", async () => {
    const res = await get(`keyword=comercio&q=${"a".repeat(201)}`);

    expect(res.status).toBe(400);
    await expect(res.json()).resolves.toMatchObject({ field: "q" });
    expect(fake.queries).toHaveLength(0);
  });
});
