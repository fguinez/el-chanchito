import { NextRequest } from "next/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { PgDialect } from "drizzle-orm/pg-core";
import type { SQL } from "drizzle-orm";
import { argOf, createFakeDb, type RecordedQuery } from "./fake-db";
import { categories, institutions, products, transactions } from "@/lib/db/schema";
import type { RateBook } from "@/lib/rates";

// The wiring of GET /api/categories/spend: validation before any query, the
// spend filters in the SQL, CLP conversion with the rate book outside a
// product, and the product's own currency inside one. The db is a recording
// double; the aggregation rules are unit-tested in category-spend.test.ts.
// Every id, name and amount is synthetic (see the repo's personal data policy).

const fake = createFakeDb();
vi.mock("@/lib/db", () => ({ db: fake.db }));

const book: RateBook = { provider: { CLP: 1, USD: 1000 }, overrides: [] };
const getRateBook = vi.fn(async () => book);
vi.mock("@/lib/rate-book", () => ({ getRateBook: () => getRateBook() }));

const { GET } = await import("@/app/api/categories/spend/route");

const FOOD = "11111111-1111-4111-8111-111111111111";
const FUN = "22222222-2222-4222-8222-222222222222";
const CARD = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const USD_CARD = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const CRYPTO = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";

interface Sum {
  categoryId: string | null;
  productId: string;
  productName: string;
  productKind: string;
  currency: string;
  institutionSlug: string;
  institutionName: string;
  month: string;
  amount: string;
  rows: string;
}

function sum(over: Partial<Sum>): Sum {
  return {
    categoryId: FOOD,
    productId: CARD,
    productName: "Tarjeta de prueba",
    productKind: "credit_card",
    currency: "CLP",
    institutionSlug: "banco-uno",
    institutionName: "Banco Uno",
    month: "2026-01",
    amount: "-1000000.00",
    rows: "3",
    ...over,
  };
}

let world: { sums: Sum[]; product: { currency: string } | null; institution: boolean };

function respond(query: RecordedQuery): unknown {
  const from = argOf(query, "from");
  if (from === categories) {
    return [
      { id: FOOD, name: "Comida", color: "#ff0000" },
      { id: FUN, name: "Ocio", color: null },
    ];
  }
  if (from === products) return world.product ? [world.product] : [];
  if (from === institutions) return world.institution ? [{ id: "inst" }] : [];
  if (from === transactions) return world.sums;
  return [];
}

function get(query: string) {
  return GET(new NextRequest(`https://dashboard.example/api/categories/spend?${query}`));
}

function spendWhereSql(): string {
  const select = fake.queries.find((q) => argOf(q, "from") === transactions);
  return new PgDialect().sqlToQuery(argOf(select!, "where") as SQL).sql.toLowerCase();
}

beforeEach(() => {
  fake.reset();
  fake.respondWith(respond);
  getRateBook.mockClear();
  world = { sums: [], product: { currency: "USD" }, institution: true };
});

describe("GET /api/categories/spend validation", () => {
  it.each([
    ["groupBy=month", "from"],
    ["from=2026-02-30&to=2026-03-01", "from"],
    ["from=2026-02-01&to=2026-01-01", "to"],
    ["from=2026-01-01&to=2026-01-31&groupBy=week", "groupBy"],
    ["from=2026-01-01&to=2026-01-31&categoryId=food", "categoryId"],
    ["from=2026-01-01&to=2026-01-31&productId=nope", "productId"],
  ])("rejects %s on %s before querying", async (query, field) => {
    const res = await get(query);
    expect(res.status).toBe(400);
    expect((await res.json()).field).toBe(field);
    expect(fake.queries).toHaveLength(0);
  });

  it("404s an unknown product", async () => {
    world.product = null;
    const res = await get(`from=2026-01-01&to=2026-01-31&productId=${CARD}`);
    expect(res.status).toBe(404);
    expect((await res.json()).field).toBe("productId");
  });

  it("404s an unknown institution", async () => {
    world.institution = false;
    const res = await get("from=2026-01-01&to=2026-01-31&institution=banco-nada");
    expect(res.status).toBe(404);
    expect((await res.json()).field).toBe("institution");
  });
});

describe("GET /api/categories/spend", () => {
  it("filters deleted rows, internal transfers and the window", async () => {
    await get("from=2026-01-01&to=2026-01-31&categoryId=none");
    const sql = spendWhereSql();
    expect(sql).toContain('"deleted_at" is null');
    expect(sql).toContain('"is_internal_transfer"');
    expect(sql).toContain('"transaction_date" >=');
    expect(sql).toContain('"transaction_date" <=');
    expect(sql).toContain('"category_id" is null');
  });

  it("converts every product to CLP and counts unconvertible rows", async () => {
    world.sums = [
      sum({ categoryId: FOOD, amount: "-2500000.00" }),
      sum({ categoryId: FUN, productId: USD_CARD, currency: "USD", amount: "-1234.56" }),
      sum({ productId: CRYPTO, currency: "XYZ", amount: "-1.00", rows: "2" }),
      // Unconvertible income is not spend, so it is not counted as missing.
      sum({ productId: CRYPTO, currency: "XYZ", amount: "5.00", rows: "4" }),
    ];
    const res = await get("from=2026-01-01&to=2026-01-31");
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(getRateBook).toHaveBeenCalledOnce();
    expect(body.currency).toBe("CLP");
    expect(body.unconvertedCount).toBe(2);
    expect(body.groups).toBeUndefined();
    expect(body.categories).toEqual([
      expect.objectContaining({ categoryId: FOOD, name: "Comida", amount: 2_500_000 }),
      expect.objectContaining({ categoryId: FUN, amount: 1_234_560 }),
    ]);
    expect(body.total).toBe(3_734_560);
  });

  it("keeps a product's own currency", async () => {
    world.sums = [sum({ productId: USD_CARD, currency: "USD", amount: "-1234.56" })];
    const res = await get(`from=2026-01-01&to=2026-01-31&productId=${USD_CARD}`);
    const body = await res.json();
    expect(getRateBook).not.toHaveBeenCalled();
    expect(body.currency).toBe("USD");
    expect(body.total).toBe(1234.56);
    expect(spendWhereSql()).toContain('"product_id" =');
  });

  it("labels product groups with the display name", async () => {
    world.sums = [
      sum({ productId: CARD, amount: "-1000000.00" }),
      sum({
        productId: USD_CARD,
        productName: "Banco Uno - checking",
        productKind: "checking",
        amount: "-2500000.00",
      }),
    ];
    const res = await get("from=2026-01-01&to=2026-01-31&groupBy=product");
    const body = await res.json();
    expect(body.groups.map((g: { key: string; label: string }) => [g.key, g.label])).toEqual([
      [USD_CARD, "Cuenta corriente"],
      [CARD, "Tarjeta de prueba"],
    ]);
  });

  it("fills the window's months when grouped by month", async () => {
    world.sums = [sum({ month: "2026-02" })];
    const res = await get("from=2026-01-01&to=2026-03-31&groupBy=month");
    const body = await res.json();
    expect(body.groups.map((g: { key: string; total: number }) => [g.key, g.total])).toEqual([
      ["2026-01", 0],
      ["2026-02", 1_000_000],
      ["2026-03", 0],
    ]);
  });

  it("filters a budget month without a window", async () => {
    const res = await get("month=2026-01-15");
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body).toMatchObject({ from: null, to: null, month: "2026-01-01" });
    const sql = spendWhereSql();
    expect(sql).toContain('"scheduled_month" =');
    expect(sql).not.toContain('"transaction_date"');
  });
});
