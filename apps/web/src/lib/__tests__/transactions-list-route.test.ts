import { beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";
import { PgDialect } from "drizzle-orm/pg-core";
import type { SQL } from "drizzle-orm";
import { createFakeDb, argOf } from "@/lib/__tests__/fake-db";

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
