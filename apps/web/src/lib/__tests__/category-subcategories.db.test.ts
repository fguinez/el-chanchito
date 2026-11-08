import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import path from "node:path";
import postgres from "postgres";
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

// Subcategories (V046) against a real PostgreSQL: the one-level trigger, the
// categories routes' parentId handling and counts, and the spend and
// transaction list scopes that take a parent's subcategories in. Runs only
// with TEST_DATABASE_URL naming a disposable server (`make test-db-up` starts
// one): the suite creates its own chanchito_test_* database, applies every
// migration with migrate.mjs, and drops it at the end. Without the variable
// it is skipped.
//
// Every name, description and amount is synthetic (see the repo's personal
// data policy).

vi.mock("@/lib/rate-book", () => ({
  getRateBook: async () => ({ provider: { CLP: 1 }, overrides: [] }),
}));

const ADMIN_URL = process.env.TEST_DATABASE_URL;
const MIGRATE = path.resolve(__dirname, "../../../../../packages/db-schema/migrate.mjs");

type CategoriesRoute = typeof import("@/app/api/categories/route");
type CategoryRoute = typeof import("@/app/api/categories/[id]/route");
type SpendRoute = typeof import("@/app/api/categories/spend/route");
type TransactionsRoute = typeof import("@/app/api/transactions/route");

describe.skipIf(!ADMIN_URL)("subcategories (real PostgreSQL)", () => {
  const dbName = `chanchito_test_${randomUUID().replace(/-/g, "")}`;
  let admin: ReturnType<typeof postgres>;
  let sql: ReturnType<typeof postgres>;
  let categoriesRoute: CategoriesRoute;
  let categoryRoute: CategoryRoute;
  let spendRoute: SpendRoute;
  let transactionsRoute: TransactionsRoute;
  let productId: string;
  let travel: string;
  let trip: string;
  let food: string;

  async function category(name: string, parentId: string | null = null) {
    const [row] = await sql`
      INSERT INTO categories (name, parent_id) VALUES (${name}, ${parentId})
      RETURNING id`;
    return row.id as string;
  }

  async function txn(amount: number, categoryId: string | null) {
    await sql`
      INSERT INTO transactions (product_id, description, amount, transaction_date,
        source, category_id, is_manually_categorized)
      VALUES (${productId}, 'MOVIMIENTO DE PRUEBA', ${amount}, '2026-01-15', 'test',
        ${categoryId}, true)`;
  }

  function json(method: string, body: unknown) {
    return new NextRequest("https://dashboard.example/api/categories", {
      method,
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
  }

  function patch(id: string, body: unknown) {
    return categoryRoute.PATCH(json("PATCH", body), {
      params: Promise.resolve({ id }),
    });
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
    categoriesRoute = await import("@/app/api/categories/route");
    categoryRoute = await import("@/app/api/categories/[id]/route");
    spendRoute = await import("@/app/api/categories/spend/route");
    transactionsRoute = await import("@/app/api/transactions/route");
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
    await sql`UPDATE categories SET parent_id = NULL WHERE name LIKE 'Prueba %'`;
    await sql`DELETE FROM categories WHERE name LIKE 'Prueba %'`;
    travel = await category("Prueba Viajes");
    trip = await category("Prueba Viaje A", travel);
    food = await category("Prueba Comida");
  });

  afterAll(async () => {
    await sql?.end();
    if (admin) {
      await admin.unsafe(`DROP DATABASE IF EXISTS ${dbName} WITH (FORCE)`);
      await admin.end();
    }
  });

  it("keeps the tree one level deep in the database", async () => {
    await expect(category("Prueba Nieto", trip)).rejects.toMatchObject({
      code: "23514",
      constraint_name: "chk_categories_one_level",
    });
    await expect(
      sql`UPDATE categories SET parent_id = id WHERE id = ${food}`
    ).rejects.toMatchObject({ code: "23514" });
    await expect(
      sql`UPDATE categories SET parent_id = ${food} WHERE id = ${travel}`
    ).rejects.toMatchObject({ code: "23514" });
    // The FK the routes map to "La categoría padre no existe" keeps V001's name.
    await expect(
      category("Prueba Huerfana", "00000000-0000-4000-8000-000000000000")
    ).rejects.toMatchObject({
      code: "23503",
      constraint_name: "categories_parent_id_fkey",
    });
    // Renaming a subcategory does not re-run the check.
    await sql`UPDATE categories SET name = 'Prueba Viaje B' WHERE id = ${trip}`;
  });

  it("creates, moves and detaches subcategories through the API", async () => {
    const created = await categoriesRoute.POST(
      json("POST", { name: "Prueba Viaje C", parentId: travel })
    );
    expect(created.status).toBe(201);
    const child = (await created.json()).id as string;

    const nested = await categoriesRoute.POST(
      json("POST", { name: "Prueba Nieto", parentId: child })
    );
    expect(nested.status).toBe(400);
    expect((await nested.json()).field).toBe("parentId");

    const parentWithChildren = await patch(travel, { parentId: food });
    expect(parentWithChildren.status).toBe(400);

    const moved = await patch(child, { parentId: food });
    expect(moved.status).toBe(200);
    expect((await moved.json()).parentId).toBe(food);

    const detached = await patch(child, { parentId: null });
    expect((await detached.json()).parentId).toBeNull();
  });

  it("rolls subcategory counts and spend into the parent, and scopes by either", async () => {
    await txn(-1_000_000, travel);
    await txn(-2_500_000, trip);
    await txn(-999_999, food);

    const cats = await (await categoriesRoute.GET()).json();
    const byId = new Map(cats.map((c: { id: string }) => [c.id, c]));
    expect(byId.get(travel)).toMatchObject({
      transactionCount: 2,
      directTransactionCount: 1,
    });
    expect(byId.get(trip)).toMatchObject({ transactionCount: 1, parentId: travel });

    const spend = async (query: string) =>
      (
        await spendRoute.GET(
          new NextRequest(
            `https://dashboard.example/api/categories/spend?from=2026-01-01&to=2026-01-31${query}`
          )
        )
      ).json();
    const all = await spend("");
    expect(all.categories.map((c: { categoryId: string; amount: number }) => [c.categoryId, c.amount]))
      .toEqual([
        [travel, 3_500_000],
        [food, 999_999],
      ]);
    const split = await spend(`&categoryId=${travel}&split=1`);
    expect(split.categories.map((c: { name: string }) => c.name)).toEqual([
      "Prueba Viaje A",
      "Sin subcategoría",
    ]);
    expect((await spend(`&categoryId=${trip}`)).total).toBe(2_500_000);

    const list = async (id: string) =>
      (
        await transactionsRoute.GET(
          new NextRequest(`https://dashboard.example/api/transactions?categoryId=${id}`)
        )
      ).json();
    expect(await list(travel)).toHaveLength(2);
    expect(await list(trip)).toHaveLength(1);
  });

  it("promotes subcategories when their parent is deleted", async () => {
    await sql`UPDATE categories SET color = '#ef4444' WHERE id = ${travel}`;
    const res = await categoryRoute.DELETE(
      new NextRequest("https://dashboard.example/api/categories", { method: "DELETE" }),
      { params: Promise.resolve({ id: travel }) }
    );
    expect(res.status).toBe(200);
    const [row] = await sql`SELECT parent_id, color FROM categories WHERE id = ${trip}`;
    expect(row.parent_id).toBeNull();
    // It keeps the color it inherited.
    expect(row.color).toBe("#ef4444");
  });
});
