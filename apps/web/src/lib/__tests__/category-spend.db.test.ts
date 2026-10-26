import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import path from "node:path";
import postgres from "postgres";
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

// GET /api/categories/spend against a real PostgreSQL: the grouped partial
// sums, the window, and the rows that never count (soft-deleted, internal
// transfers). Runs only with TEST_DATABASE_URL naming a disposable server
// (`make test-db-up` starts one): the suite creates its own chanchito_test_*
// database, applies every migration with migrate.mjs, and drops it at the end.
// Without the variable it is skipped. Provider rates are stubbed so nothing
// reaches the network.
//
// Every name, description and amount is synthetic (see the repo's personal
// data policy).

vi.mock("@/lib/rate-book", () => ({
  getRateBook: async () => ({ provider: { CLP: 1 }, overrides: [] }),
}));

const ADMIN_URL = process.env.TEST_DATABASE_URL;
const MIGRATE = path.resolve(__dirname, "../../../../../packages/db-schema/migrate.mjs");

type SpendRoute = typeof import("@/app/api/categories/spend/route");

describe.skipIf(!ADMIN_URL)("category spend (real PostgreSQL)", () => {
  const dbName = `chanchito_test_${randomUUID().replace(/-/g, "")}`;
  let admin: ReturnType<typeof postgres>;
  let sql: ReturnType<typeof postgres>;
  let route: SpendRoute;
  let productId: string;
  let food: string;
  let salary: string;

  async function txn(
    amount: number,
    date: string,
    extra: { categoryId?: string | null; transfer?: boolean; deleted?: boolean } = {}
  ) {
    // The insert trigger would categorize by rules; there are none here.
    await sql`
      INSERT INTO transactions (product_id, description, amount, transaction_date,
        source, category_id, is_manually_categorized, is_internal_transfer, deleted_at)
      VALUES (${productId}, 'MOVIMIENTO DE PRUEBA', ${amount}, ${date}, 'test',
        ${extra.categoryId ?? null}, true, ${extra.transfer ?? false},
        ${extra.deleted ? new Date() : null})`;
  }

  async function spend(query: string) {
    const res = await route.GET(
      new NextRequest(`https://dashboard.example/api/categories/spend?${query}`)
    );
    expect(res.status).toBe(200);
    return res.json();
  }

  beforeAll(async () => {
    admin = postgres(ADMIN_URL as string, { max: 1 });
    await admin.unsafe(`CREATE DATABASE ${dbName}`);
    const url = new URL(ADMIN_URL as string);
    url.pathname = `/${dbName}`;
    execFileSync(process.execPath, [MIGRATE], {
      env: { ...process.env, DATABASE_URL: url.toString() },
      stdio: "ignore",
    });
    process.env.DATABASE_URL = url.toString();
    sql = postgres(url.toString(), { max: 1, onnotice: () => {} });
    route = await import("@/app/api/categories/spend/route");
  });

  beforeEach(async () => {
    await sql`TRUNCATE transactions, products, accounts, institutions, users CASCADE`;
    const [user] = await sql`INSERT INTO users (name) VALUES ('Usuario de prueba') RETURNING id`;
    const [inst] = await sql`
      INSERT INTO institutions (slug, name) VALUES ('banco-de-prueba', 'Banco de Prueba')
      RETURNING id`;
    const [account] = await sql`
      INSERT INTO accounts (user_id, institution_id) VALUES (${user.id}, ${inst.id})
      RETURNING id`;
    const [product] = await sql`
      INSERT INTO products (account_id, kind, name, default_name, slug, currency)
      VALUES (${account.id}, 'checking', 'Cuenta de prueba', 'Cuenta de prueba',
        'checking', 'CLP')
      RETURNING id`;
    productId = product.id;
    await sql`DELETE FROM categories WHERE name LIKE 'Prueba %'`;
    const [a] = await sql`
      INSERT INTO categories (name, color) VALUES ('Prueba Comida', '#ff0000') RETURNING id`;
    const [b] = await sql`INSERT INTO categories (name) VALUES ('Prueba Sueldo') RETURNING id`;
    food = a.id;
    salary = b.id;
  });

  afterAll(async () => {
    await sql?.end();
    if (admin) {
      await admin.unsafe(`DROP DATABASE IF EXISTS ${dbName} WITH (FORCE)`);
      await admin.end();
    }
  });

  it("nets categories, keeps uncategorized expenses apart, and skips what never counts", async () => {
    await txn(-2_500_000, "2026-01-10", { categoryId: food });
    await txn(1_000_000, "2026-02-05", { categoryId: food });
    await txn(2_500_000, "2026-01-31", { categoryId: salary });
    await txn(-999_999, "2026-02-01");
    await txn(1_000_000, "2026-02-02");
    await txn(-1_000_000, "2026-01-15", { categoryId: food, transfer: true });
    await txn(-1_000_000, "2026-01-16", { categoryId: food, deleted: true });
    await txn(-1_000_000, "2026-03-01", { categoryId: food });

    const body = await spend("from=2026-01-01&to=2026-02-28&groupBy=month");
    expect(body.currency).toBe("CLP");
    expect(body.categories.map((c: { categoryId: string | null; amount: number }) => [
      c.categoryId,
      c.amount,
    ])).toEqual([
      [food, 1_500_000],
      [null, 999_999],
    ]);
    expect(body.groups.map((g: { key: string; total: number }) => [g.key, g.total])).toEqual([
      ["2026-01", 2_500_000],
      ["2026-02", 999_999],
    ]);

    const scoped = await spend(
      `from=2026-01-01&to=2026-02-28&productId=${productId}&categoryId=${food}&groupBy=institution`
    );
    expect(scoped.total).toBe(1_500_000);
    expect(scoped.groups).toEqual([
      expect.objectContaining({ key: "banco-de-prueba", label: "Banco de Prueba" }),
    ]);
  });
});
