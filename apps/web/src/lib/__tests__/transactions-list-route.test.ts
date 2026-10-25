import { beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";
import { PgDialect } from "drizzle-orm/pg-core";
import type { SQL } from "drizzle-orm";
import { createFakeDb, argOf, whereParams } from "@/lib/__tests__/fake-db";

// The list route runs against the recording fake db; we read back the `where`
// it built to prove the soft-delete filter is always applied.
const fake = createFakeDb();
vi.mock("@/lib/db", () => ({ db: fake.db }));

const { GET } = await import("@/app/api/transactions/route");

function get(query = "") {
  return GET(
    new NextRequest(`https://dashboard.example/api/transactions${query}`)
  );
}

/** The SQL text of the list query's `where`. */
function listWhereSql(): string {
  const select = fake.queries.find((q) => q.op === "select");
  return new PgDialect().sqlToQuery(argOf(select!, "where") as SQL).sql.toLowerCase();
}

beforeEach(() => fake.reset());

describe("GET /api/transactions excludes soft-deleted rows", () => {
  it("filters deleted_at IS NULL with no other filter", async () => {
    const res = await get();
    expect(res.status).toBe(200);
    const sql = listWhereSql();
    expect(sql).toContain("deleted_at");
    expect(sql).toContain("is null");
  });

  it("keeps the filter alongside a month filter", async () => {
    const res = await get("?month=2026-01-15");
    expect(res.status).toBe(200);
    const sql = listWhereSql();
    expect(sql).toContain("deleted_at");
    expect(sql).toContain("is null");
    expect(sql).toContain("scheduled_month");
  });
});

describe("GET /api/transactions filters", () => {
  // Synthetic ids (see the repo's personal data policy).
  const CATEGORY = "11111111-1111-4111-8111-111111111111";

  it("bounds transaction_date with from and to, inclusive", async () => {
    const res = await get("?from=2026-01-01&to=2026-01-31");
    expect(res.status).toBe(200);
    const sql = listWhereSql();
    expect(sql).toContain('"transaction_date" >=');
    expect(sql).toContain('"transaction_date" <=');
    expect(sql).not.toContain("scheduled_month");
  });

  it("takes an open-ended window", async () => {
    await get("?from=2026-01-01");
    const sql = listWhereSql();
    expect(sql).toContain('"transaction_date" >=');
    expect(sql).not.toContain('"transaction_date" <=');
  });

  it("combines a month with a window", async () => {
    await get("?month=2026-01-01&from=2026-01-10");
    const sql = listWhereSql();
    expect(sql).toContain("scheduled_month");
    expect(sql).toContain('"transaction_date" >=');
  });

  it("filters one category, case-insensitively", async () => {
    await get(`?categoryId=${CATEGORY.toUpperCase()}`);
    const select = fake.queries.find((q) => q.op === "select")!;
    expect(listWhereSql()).toContain('"category_id" =');
    expect(whereParams(select)).toContain(CATEGORY);
  });

  it("filters uncategorized rows with none", async () => {
    await get("?categoryId=none");
    expect(listWhereSql()).toContain('"category_id" is null');
  });

  it("filters an institution by slug", async () => {
    await get("?institution=banco-uno");
    const select = fake.queries.find((q) => q.op === "select")!;
    expect(listWhereSql()).toContain('"institutions"."slug" =');
    expect(whereParams(select)).toContain("banco-uno");
  });

  it.each([
    ["?from=2026-02-30", "from"],
    ["?from=2026-02-01&to=2026-01-01", "to"],
    ["?categoryId=food", "categoryId"],
  ])("rejects %s on %s before querying", async (query, field) => {
    const res = await get(query);
    expect(res.status).toBe(400);
    expect((await res.json()).field).toBe(field);
    expect(fake.queries).toHaveLength(0);
  });
});
