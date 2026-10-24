import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import path from "node:path";
import postgres from "postgres";
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";

// The category suggestion queries against a real PostgreSQL: the history and
// target grouping, the one-off apply and the dismissals table (V042). Runs
// only with TEST_DATABASE_URL naming a disposable server (`make test-db-up`
// starts one): the suite creates its own chanchito_test_* database, applies
// every migration with migrate.mjs, and drops it at the end. Without the
// variable it is skipped.
//
// Every name, description and amount is synthetic (see the repo's personal
// data policy): one fake product, $ 999.999 movements.

const ADMIN_URL = process.env.TEST_DATABASE_URL;
const MIGRATE = path.resolve(__dirname, "../../../../../packages/db-schema/migrate.mjs");

type Lib = typeof import("@/lib/category-suggestions-db");

describe.skipIf(!ADMIN_URL)("category suggestions (real PostgreSQL)", () => {
  const dbName = `chanchito_test_${randomUUID().replace(/-/g, "")}`;
  let admin: ReturnType<typeof postgres>;
  let sql: ReturnType<typeof postgres>;
  let lib: Lib;
  let productId: string;
  let food: string;
  let other: string;

  async function txn(
    description: string,
    extra: { categoryId?: string; manual?: boolean; deleted?: boolean; date?: string } = {}
  ) {
    const [row] = await sql`
      INSERT INTO transactions (product_id, description, amount, transaction_date,
        source, category_id, is_manually_categorized, deleted_at)
      VALUES (${productId}, ${description}, -999999, ${extra.date ?? "2026-01-15"},
        'test', ${extra.categoryId ?? null}, ${extra.manual ?? false},
        ${extra.deleted ? new Date() : null})
      RETURNING id`;
    return row.id as string;
  }

  async function categoryOf(id: string) {
    const [row] = await sql`
      SELECT category_id, is_manually_categorized FROM transactions WHERE id = ${id}`;
    return { categoryId: row.category_id, manual: row.is_manually_categorized };
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
    lib = await import("@/lib/category-suggestions-db");
  });

  beforeEach(async () => {
    await sql`
      TRUNCATE dismissed_category_suggestions, category_rules, transactions,
        products, accounts, institutions, users CASCADE`;
    const [user] = await sql`INSERT INTO users (name) VALUES ('Usuario de prueba') RETURNING id`;
    const [inst] = await sql`
      INSERT INTO institutions (slug, name) VALUES ('banco-de-prueba', 'Banco de Prueba')
      RETURNING id`;
    const [account] = await sql`
      INSERT INTO accounts (user_id, institution_id) VALUES (${user.id}, ${inst.id})
      RETURNING id`;
    const [product] = await sql`
      INSERT INTO products (account_id, kind, name, slug, currency)
      VALUES (${account.id}, 'checking', 'Cuenta corriente 00-000-00000-01',
        'checking', 'CLP')
      RETURNING id`;
    productId = product.id;
    // Seeded categories would vote too; only the test's own may.
    await sql`DELETE FROM categories`;
    const [a] = await sql`INSERT INTO categories (name) VALUES ('Prueba Comida') RETURNING id`;
    const [b] = await sql`INSERT INTO categories (name) VALUES ('Prueba Otra') RETURNING id`;
    food = a.id;
    other = b.id;
  });

  afterAll(async () => {
    await sql?.end();
    if (admin) {
      await admin.unsafe(`DROP DATABASE IF EXISTS ${dbName} WITH (FORCE)`);
      await admin.end();
    }
  });

  it("groups the targets by description and learns from visible categorized rows", async () => {
    await txn("COMERCIO EJEMPLO SPA 1234", { categoryId: food });
    await txn("COMERCIO EJEMPLO SPA 5678", { categoryId: food, manual: true });
    // Deleted history does not vote.
    await txn("COMERCIO EJEMPLO SPA 0000", { categoryId: other, deleted: true });
    await txn("COMERCIO EJEMPLO SPA 9999", { date: "2026-01-10" });
    const latest = await txn(" comercio ejemplo spa 9999", { date: "2026-01-20" });
    // Not targets: deleted, cleared by hand, or with no suggestion.
    await txn("COMERCIO EJEMPLO SPA 9999", { deleted: true });
    await txn("COMERCIO EJEMPLO SPA 9999", { manual: true });
    await txn("LIBRERIA INVENTADA");

    const groups = await lib.listCategorySuggestions();
    expect(groups).toEqual([
      expect.objectContaining({
        count: 2,
        latestDate: "2026-01-20",
        sampleTransactionId: latest,
        categoryId: food,
        categoryName: "Prueba Comida",
        matchCount: 2,
        exact: true,
        reason: "2 transacciones con la misma descripción están en Prueba Comida",
      }),
    ]);
    expect(groups[0].description.trim().toLowerCase()).toBe("comercio ejemplo spa 9999");
  });

  it("suggests for one uncategorized row only", async () => {
    await txn("SUPERMERCADO FICTICIO LTDA", { categoryId: other });
    const target = await txn("SUPERMERCADO FICTICIO LTDA SUCURSAL CENTRO");
    const categorized = await txn("LIBRERIA INVENTADA", { categoryId: food, manual: true });
    const deleted = await txn("SUPERMERCADO FICTICIO LTDA", { deleted: true });

    const outcome = await lib.suggestionForTransaction(target);
    expect(outcome).toMatchObject({
      ok: true,
      suggestion: { categoryId: other, exact: false },
    });
    expect(await lib.suggestionForTransaction(categorized)).toEqual({
      ok: true,
      suggestion: null,
    });
    expect(await lib.suggestionForTransaction(deleted)).toEqual({ ok: false, status: 404 });
  });

  it("applies once to the uncategorized rows with the same description only", async () => {
    const a = await txn("COMERCIO EJEMPLO SPA");
    const b = await txn(" comercio ejemplo spa ");
    const categorized = await txn("COMERCIO EJEMPLO SPA", { categoryId: other });
    const cleared = await txn("COMERCIO EJEMPLO SPA", { manual: true });
    const deleted = await txn("COMERCIO EJEMPLO SPA", { deleted: true });
    const longer = await txn("COMERCIO EJEMPLO SPA SUCURSAL");

    const outcome = await lib.applySuggestionOnce({
      description: "Comercio Ejemplo SPA",
      categoryId: food,
    });
    expect(outcome).toEqual({ ok: true, updatedCount: 2 });
    expect(await categoryOf(a)).toEqual({ categoryId: food, manual: true });
    expect(await categoryOf(b)).toEqual({ categoryId: food, manual: true });
    expect(await categoryOf(categorized)).toEqual({ categoryId: other, manual: false });
    expect(await categoryOf(cleared)).toEqual({ categoryId: null, manual: true });
    expect(await categoryOf(deleted)).toEqual({ categoryId: null, manual: false });
    expect(await categoryOf(longer)).toEqual({ categoryId: null, manual: false });
    // No rule was created.
    const [{ n }] = await sql`SELECT count(*)::int AS n FROM category_rules`;
    expect(n).toBe(0);
  });

  it("rejects an unknown category", async () => {
    await txn("COMERCIO EJEMPLO SPA");
    expect(
      await lib.applySuggestionOnce({
        description: "COMERCIO EJEMPLO SPA",
        categoryId: randomUUID(),
      })
    ).toMatchObject({ ok: false, status: 400, field: "categoryId" });
    expect(
      await lib.dismissCategorySuggestion({
        description: "COMERCIO EJEMPLO SPA",
        categoryId: randomUUID(),
      })
    ).toMatchObject({ ok: false, status: 400, field: "categoryId" });
  });

  it("remembers a dismissal until the suggestion changes category", async () => {
    await txn("COMERCIO EJEMPLO SPA 1234", { categoryId: food });
    const target = await txn("COMERCIO EJEMPLO SPA 9999");

    const input = { description: "COMERCIO EJEMPLO SPA 9999", categoryId: food };
    expect(await lib.dismissCategorySuggestion(input)).toEqual({ ok: true });
    // Idempotent.
    expect(await lib.dismissCategorySuggestion(input)).toEqual({ ok: true });
    const rows = await sql`
      SELECT normalized_description, category_id FROM dismissed_category_suggestions`;
    expect(rows).toEqual([
      { normalized_description: "comercio ejemplo spa", category_id: food },
    ]);
    expect(await lib.listCategorySuggestions()).toEqual([]);
    expect(await lib.suggestionForTransaction(target)).toEqual({ ok: true, suggestion: null });

    // The history now points at another category: the suggestion is back.
    await sql`UPDATE transactions SET category_id = ${other} WHERE category_id = ${food}`;
    const groups = await lib.listCategorySuggestions();
    expect(groups.map((g) => g.categoryId)).toEqual([other]);
  });

  it("drops dismissals with their category", async () => {
    await lib.dismissCategorySuggestion({ description: "COMERCIO EJEMPLO", categoryId: food });
    await sql`DELETE FROM categories WHERE id = ${food}`;
    const [{ n }] = await sql`SELECT count(*)::int AS n FROM dismissed_category_suggestions`;
    expect(n).toBe(0);
  });

  it("enforces one dismissal per description and category", async () => {
    await sql`
      INSERT INTO dismissed_category_suggestions (normalized_description, category_id)
      VALUES ('comercio ejemplo', ${food})`;
    await expect(
      sql`
        INSERT INTO dismissed_category_suggestions (normalized_description, category_id)
        VALUES ('comercio ejemplo', ${food})`
    ).rejects.toThrow(/uq_dismissed_category_suggestions/);
  });
});
