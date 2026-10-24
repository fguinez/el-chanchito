import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import path from "node:path";
import postgres from "postgres";
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { DESCRIPTION_RULE_PRIORITY } from "@/lib/category-rules-db";

// applyCategoryRuleFromTransaction against a real PostgreSQL, the insert
// trigger (V021) included. Runs only with TEST_DATABASE_URL naming a disposable
// server (`make test-db-up` starts one): the suite creates its own
// chanchito_test_* database, applies every migration with migrate.mjs, and
// drops it at the end. Without the variable it is skipped.
//
// Every name, description and amount is synthetic (see the repo's personal
// data policy): one fake product, $ 999.999 movements.

const ADMIN_URL = process.env.TEST_DATABASE_URL;
const MIGRATE = path.resolve(__dirname, "../../../../../packages/db-schema/migrate.mjs");

type Lib = typeof import("@/lib/category-rules-db");
type Db = typeof import("@/lib/db");

describe.skipIf(!ADMIN_URL)("category rule from a transaction (real PostgreSQL)", () => {
  const dbName = `chanchito_test_${randomUUID().replace(/-/g, "")}`;
  let admin: ReturnType<typeof postgres>;
  let sql: ReturnType<typeof postgres>;
  let lib: Lib;
  let dbModule: Db;
  let productId: string;
  let food: string;
  let other: string;

  async function txn(
    description: string,
    extra: { categoryId?: string; manual?: boolean; deleted?: boolean } = {}
  ) {
    const [row] = await sql`
      INSERT INTO transactions (product_id, description, amount, transaction_date,
        source, category_id, is_manually_categorized, deleted_at)
      VALUES (${productId}, ${description}, -999999, '2026-01-15', 'test',
        ${extra.categoryId ?? null}, ${extra.manual ?? false},
        ${extra.deleted ? new Date() : null})
      RETURNING id`;
    return row.id as string;
  }

  async function categoryOf(id: string) {
    const [row] = await sql`
      SELECT category_id, is_manually_categorized FROM transactions WHERE id = ${id}`;
    return { categoryId: row.category_id, manual: row.is_manually_categorized };
  }

  function apply(id: string, categoryId: string) {
    return dbModule.db.transaction((tx) =>
      lib.applyCategoryRuleFromTransaction(tx, id, categoryId)
    );
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
    lib = await import("@/lib/category-rules-db");
    dbModule = await import("@/lib/db");
  });

  beforeEach(async () => {
    await sql`
      TRUNCATE category_rules, transactions, products, accounts, institutions,
        users CASCADE`;
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
    await sql`DELETE FROM categories WHERE name LIKE 'Prueba %'`;
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

  it("creates the rule and categorizes the same description, skipping manual and deleted rows", async () => {
    // A broader rule that already categorized some rows automatically.
    await sql`
      INSERT INTO category_rules (keyword, category_id, priority)
      VALUES ('comercio', ${other}, 10)`;
    const target = await txn("COMERCIO EJEMPLO SPA");
    const sameCase = await txn("comercio ejemplo spa ");
    const uncategorized = await txn("Comercio Ejemplo SPA");
    await sql`UPDATE transactions SET category_id = NULL WHERE id = ${uncategorized}`;
    const manual = await txn("COMERCIO EJEMPLO SPA", { categoryId: other, manual: true });
    const deleted = await txn("COMERCIO EJEMPLO SPA", { deleted: true });
    const longer = await txn("COMERCIO EJEMPLO SPA SUCURSAL");

    const outcome = await apply(target, food);
    expect(outcome).toMatchObject({
      ok: true,
      ruleCreated: true,
      updatedCount: 2,
      rule: {
        keyword: "comercio ejemplo spa",
        categoryId: food,
        priority: DESCRIPTION_RULE_PRIORITY,
      },
    });

    expect(await categoryOf(target)).toEqual({ categoryId: food, manual: true });
    expect(await categoryOf(sameCase)).toEqual({ categoryId: food, manual: false });
    expect(await categoryOf(uncategorized)).toEqual({ categoryId: food, manual: false });
    expect(await categoryOf(manual)).toEqual({ categoryId: other, manual: true });
    // The deleted row keeps what the old rule gave it on insert.
    expect((await categoryOf(deleted)).categoryId).toBe(other);
    // The backfill matches the whole description, not a substring.
    expect((await categoryOf(longer)).categoryId).toBe(other);

    // Future rows: the exact-description rule outranks the broader keyword.
    const future = await txn("COMERCIO EJEMPLO SPA");
    expect((await categoryOf(future)).categoryId).toBe(food);
  });

  it("reuses a rule with the same keyword and points it at the new category", async () => {
    const [rule] = await sql`
      INSERT INTO category_rules (keyword, category_id, priority)
      VALUES ('Comercio Ejemplo SPA', ${other}, 0)
      RETURNING id`;
    const target = await txn("COMERCIO EJEMPLO SPA");

    const outcome = await apply(target, food);
    expect(outcome).toMatchObject({
      ok: true,
      ruleCreated: false,
      rule: { id: rule.id, categoryId: food, priority: DESCRIPTION_RULE_PRIORITY },
    });
    const [{ n }] = await sql`SELECT count(*)::int AS n FROM category_rules`;
    expect(n).toBe(1);
  });

  it("returns 404 for a deleted transaction and leaves no rule behind", async () => {
    const deleted = await txn("COMERCIO EJEMPLO SPA", { deleted: true });
    expect(await apply(deleted, food)).toEqual({ ok: false, status: 404 });
    const [{ n }] = await sql`SELECT count(*)::int AS n FROM category_rules`;
    expect(n).toBe(0);
  });
});
