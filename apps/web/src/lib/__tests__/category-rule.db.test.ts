import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import path from "node:path";
import postgres from "postgres";
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { NextRequest } from "next/server";

// applyCategoryRuleFromTransaction and the exact/contains rule matching (V041)
// against a real PostgreSQL: the insert trigger, the "Categorizar ahora"
// backfill and the rule preview included. Runs only with TEST_DATABASE_URL naming a disposable
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
type CategoriesRoute = typeof import("@/app/api/categories/route");
type PreviewRoute = typeof import("@/app/api/categories/rules/preview/route");
type RulesRoute = typeof import("@/app/api/categories/rules/route");
type RuleRoute = typeof import("@/app/api/categories/rules/[id]/route");

describe.skipIf(!ADMIN_URL)("category rule from a transaction (real PostgreSQL)", () => {
  const dbName = `chanchito_test_${randomUUID().replace(/-/g, "")}`;
  let admin: ReturnType<typeof postgres>;
  let sql: ReturnType<typeof postgres>;
  let lib: Lib;
  let dbModule: Db;
  let categoriesRoute: CategoriesRoute;
  let previewRoute: PreviewRoute;
  let rulesRoute: RulesRoute;
  let ruleRoute: RuleRoute;
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
    categoriesRoute = await import("@/app/api/categories/route");
    previewRoute = await import("@/app/api/categories/rules/preview/route");
    rulesRoute = await import("@/app/api/categories/rules/route");
    ruleRoute = await import("@/app/api/categories/rules/[id]/route");
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
        priority: 0,
        matchType: "exact",
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

    // Future rows: the exact rule outranks the broader keyword, even at a
    // lower priority, and only for the same whole description.
    const future = await txn(" comercio ejemplo SPA");
    expect((await categoryOf(future)).categoryId).toBe(food);
    const futureLonger = await txn("COMERCIO EJEMPLO SPA SUCURSAL");
    expect((await categoryOf(futureLonger)).categoryId).toBe(other);
  });

  it("an exact rule from a short description does not grab longer ones", async () => {
    const target = await txn("UBER");
    await apply(target, food);
    const trip = await txn("UBER");
    const eats = await txn("UBER EATS");
    expect((await categoryOf(trip)).categoryId).toBe(food);
    expect((await categoryOf(eats)).categoryId).toBeNull();
  });

  it("keys the rule on PostgreSQL's trim, so a trailing tab or NBSP still matches", async () => {
    const target = await txn("COMERCIO EJEMPLO SPA\u00a0");

    const outcome = await apply(target, food);
    // Saving it from the Categorías editor resends the keyword through the rules API.
    const id = outcome.ok ? outcome.rule.id : "";
    const patched = await ruleRoute.PATCH(
      new NextRequest(`http://localhost:3000/api/categories/rules/${id}`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ keyword: "COMERCIO EJEMPLO SPA\u00a0 ", priority: 5 }),
      }),
      { params: Promise.resolve({ id }) }
    );

    expect(outcome).toMatchObject({ ok: true, rule: { keyword: "comercio ejemplo spa\u00a0" } });
    expect(await patched.json()).toMatchObject({ keyword: "comercio ejemplo spa\u00a0" });
    const future = await txn("COMERCIO EJEMPLO SPA\u00a0");
    expect((await categoryOf(future)).categoryId).toBe(food);
  });

  it("rejects a blank description as empty, not too long", async () => {
    const target = await txn("   ");

    const outcome = await apply(target, food);

    expect(outcome).toMatchObject({
      ok: false,
      status: 400,
      error: "La descripción original está vacía; no se puede crear una regla",
    });
  });

  it("the backfill and the preview honor the match type", async () => {
    const same = await txn("COMERCIO EJEMPLO SPA");
    const longer = await txn("COMERCIO EJEMPLO SPA SUCURSAL");
    await sql`
      INSERT INTO category_rules (keyword, category_id, priority, match_type)
      VALUES ('comercio ejemplo spa', ${food}, -5, 'exact'),
             ('comercio', ${other}, 50, 'contains')`;

    const res = await categoriesRoute.PUT();
    expect(await res.json()).toEqual({ assigned: 2 });
    expect((await categoryOf(same)).categoryId).toBe(food);
    expect((await categoryOf(longer)).categoryId).toBe(other);

    const preview = async (matchType: string) => {
      const url = `http://localhost:3000/api/categories/rules/preview?keyword=${encodeURIComponent(
        "Comercio Ejemplo SPA"
      )}&matchType=${matchType}`;
      return (await previewRoute.GET(new NextRequest(url))).json();
    };
    expect(await preview("exact")).toMatchObject({ matchType: "exact", total: 1 });
    expect(await preview("contains")).toMatchObject({
      matchType: "contains",
      total: 2,
    });
  });

  it("narrows the preview with a search on the raw or the custom description", async () => {
    const cafe = await txn("COMERCIO CAFE EJEMPLO");
    const relabeled = await txn("COMERCIO TIENDA EJEMPLO");
    await txn("COMERCIO FARMACIA EJEMPLO");
    await sql`
      UPDATE transactions SET custom_description = 'Café de prueba'
      WHERE id = ${relabeled}`;

    const preview = async (q: string) => {
      const url = `http://localhost:3000/api/categories/rules/preview?keyword=comercio&q=${encodeURIComponent(q)}`;
      return (await previewRoute.GET(new NextRequest(url))).json();
    };
    const ids = (body: { transactions: { id: string }[] }) =>
      body.transactions.map((t) => t.id).sort();

    // "café" folds to "cafe": the raw text of one row, the custom text of the other.
    const both = await preview("Café");
    expect(both).toMatchObject({ total: 2, uncategorized: 2 });
    expect(ids(both)).toEqual([cafe, relabeled].sort());
    expect(both.transactions).toContainEqual(
      expect.objectContaining({
        id: relabeled,
        description: "COMERCIO TIENDA EJEMPLO",
        displayDescription: "Café de prueba",
      })
    );
    // Every word must match, each in either description.
    expect(await preview("prueba tienda")).toMatchObject({ total: 1 });
    expect(await preview("prueba farmacia")).toMatchObject({ total: 0 });
  });

  it("allows one exact rule per keyword through the rules API (400, not 500)", async () => {
    const send = (method: string, url: string, body: unknown) =>
      new NextRequest(`http://localhost:3000${url}`, {
        method,
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
    const create = (keyword: string, matchType: string) =>
      rulesRoute.POST(
        send("POST", "/api/categories/rules", { keyword, categoryId: food, matchType })
      );
    const clash = {
      error: "Ya existe una regla exacta para esa descripción",
      field: "keyword",
    };

    expect((await create("Comercio Ejemplo SPA", "exact")).status).toBe(201);
    const second = await create(" comercio ejemplo spa ", "exact");
    expect(second.status).toBe(400);
    expect(await second.json()).toMatchObject(clash);

    // A contains rule with the same keyword is fine, but cannot become exact.
    const contains = await create("comercio ejemplo spa", "contains");
    expect(contains.status).toBe(201);
    const { id } = await contains.json();
    const patched = await ruleRoute.PATCH(
      send("PATCH", `/api/categories/rules/${id}`, { matchType: "exact" }),
      { params: Promise.resolve({ id }) }
    );
    expect(patched.status).toBe(400);
    expect(await patched.json()).toMatchObject(clash);

    // The panel reuses the one exact rule instead of clashing with it.
    const target = await txn("COMERCIO EJEMPLO SPA");
    expect(await apply(target, other)).toMatchObject({ ok: true, ruleCreated: false });
  });

  it("rejects a match type outside contains/exact", async () => {
    await expect(sql`
      INSERT INTO category_rules (keyword, category_id, match_type)
      VALUES ('comercio', ${food}, 'substring')`).rejects.toThrow();
  });

  it("reuses an exact rule with the same keyword, keeping its priority", async () => {
    const [rule] = await sql`
      INSERT INTO category_rules (keyword, category_id, priority, match_type)
      VALUES ('Comercio Ejemplo SPA', ${other}, 7, 'exact')
      RETURNING id`;
    const target = await txn("COMERCIO EJEMPLO SPA");

    const outcome = await apply(target, food);
    expect(outcome).toMatchObject({
      ok: true,
      ruleCreated: false,
      rule: { id: rule.id, categoryId: food, priority: 7 },
    });
    const [{ n }] = await sql`SELECT count(*)::int AS n FROM category_rules`;
    expect(n).toBe(1);
  });

  it("does not reuse a contains rule with the same keyword", async () => {
    await sql`
      INSERT INTO category_rules (keyword, category_id)
      VALUES ('comercio ejemplo spa', ${other})`;
    const target = await txn("COMERCIO EJEMPLO SPA");
    expect(await apply(target, food)).toMatchObject({ ok: true, ruleCreated: true });
    const rows = await sql`
      SELECT match_type, category_id FROM category_rules ORDER BY match_type`;
    expect(rows.map((r) => [r.match_type, r.category_id])).toEqual([
      ["contains", other],
      ["exact", food],
    ]);
  });

  it("serializes concurrent saves of the same keyword into one rule", async () => {
    const a = await txn("COMERCIO EJEMPLO SPA");
    const b = await txn("comercio ejemplo spa");
    const outcomes = await Promise.all([apply(a, food), apply(b, food)]);
    expect(outcomes.filter((o) => o.ok && o.ruleCreated)).toHaveLength(1);
    const [{ n }] = await sql`SELECT count(*)::int AS n FROM category_rules`;
    expect(n).toBe(1);
  });

  it("returns 404 for a deleted transaction and leaves no rule behind", async () => {
    const deleted = await txn("COMERCIO EJEMPLO SPA", { deleted: true });
    expect(await apply(deleted, food)).toEqual({ ok: false, status: 404 });
    const [{ n }] = await sql`SELECT count(*)::int AS n FROM category_rules`;
    expect(n).toBe(0);
  });

  it("onlyIfUncategorized leaves a row recategorized meanwhile alone but still saves the rule", async () => {
    const stale = await txn("COMERCIO EJEMPLO SPA", { categoryId: other, manual: true });
    const fresh = await txn("COMERCIO EJEMPLO SPA");
    const outcome = await dbModule.db.transaction((tx) =>
      lib.applyCategoryRuleFromTransaction(tx, stale, food, { onlyIfUncategorized: true })
    );
    expect(outcome).toMatchObject({
      ok: true,
      ruleCreated: true,
      updatedCount: 1,
      transactionUpdated: false,
    });
    expect(await categoryOf(stale)).toEqual({ categoryId: other, manual: true });
    expect(await categoryOf(fresh)).toEqual({ categoryId: food, manual: false });

    // The insert trigger now categorizes it; clear that to leave it uncategorized.
    const again = await txn("COMERCIO EJEMPLO SPA");
    await sql`UPDATE transactions SET category_id = NULL WHERE id = ${again}`;
    expect(
      await dbModule.db.transaction((tx) =>
        lib.applyCategoryRuleFromTransaction(tx, again, food, { onlyIfUncategorized: true })
      )
    ).toMatchObject({ ok: true, ruleCreated: false, transactionUpdated: true });
    expect(await categoryOf(again)).toEqual({ categoryId: food, manual: true });
  });
});
