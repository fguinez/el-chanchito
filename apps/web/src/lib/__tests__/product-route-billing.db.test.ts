import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import path from "node:path";
import postgres from "postgres";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";

// GET /api/institutions/[slug]/products/[product] against a real PostgreSQL:
// the V044 billing columns as the route reads them (DATE as a plain day) and
// the card's billing block built from them. Runs only with TEST_DATABASE_URL
// naming a disposable server (`make test-db-up` starts one): the suite creates
// its own chanchito_test_* database, applies every migration with
// migrate.mjs, and drops it at the end. Without the variable it is skipped.
// Provider rates are stubbed so nothing reaches the network.
//
// Every name, date and amount is synthetic (see the repo's personal data
// policy): a fake card ending in 1234, $ 1.000.000 and $ 999.999 movements.

vi.mock("@/lib/rate-book", () => ({
  getRateBook: async () => ({ provider: { CLP: 1 }, overrides: [] }),
}));

const ADMIN_URL = process.env.TEST_DATABASE_URL;
const MIGRATE = path.resolve(__dirname, "../../../../../packages/db-schema/migrate.mjs");

type ProductRoute =
  typeof import("@/app/api/institutions/[slug]/products/[product]/route");

describe.skipIf(!ADMIN_URL)("product route billing (real PostgreSQL)", () => {
  const dbName = `chanchito_test_${randomUUID().replace(/-/g, "")}`;
  let admin: ReturnType<typeof postgres>;
  let sql: ReturnType<typeof postgres>;
  let route: ProductRoute;

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
    route = await import("@/app/api/institutions/[slug]/products/[product]/route");

    const [user] = await sql`INSERT INTO users (name) VALUES ('Usuario de prueba') RETURNING id`;
    const [inst] = await sql`
      INSERT INTO institutions (slug, name) VALUES ('banco-de-prueba', 'Banco de Prueba')
      RETURNING id`;
    const [account] = await sql`
      INSERT INTO accounts (user_id, institution_id) VALUES (${user.id}, ${inst.id})
      RETURNING id`;
    // The billing day comes from the movements' statement date: the card's
    // metrics carry no statement, only the attributes' due day.
    const attributes = { kind: "credit_card", last4: "1234", due_day: 5 };
    const metrics = { kind: "credit_card", available: 2500000 };
    const [card] = await sql`
      INSERT INTO products (account_id, kind, name, default_name, slug, currency,
        attributes, metrics)
      VALUES (${account.id}, 'credit_card', 'Tarjeta de prueba', 'Tarjeta de prueba',
        'tarjeta', 'CLP', ${sql.json(attributes)}, ${sql.json(metrics)})
      RETURNING id`;
    await sql`
      INSERT INTO transactions (product_id, description, amount, transaction_date,
        source, external_id, billing_status, statement_date)
      VALUES
        (${card.id}, 'COMPRA DE PRUEBA', -1000000, '2026-10-10', 'scraper', 'p-1',
          'billed', '2026-10-15'),
        (${card.id}, 'COMPRA DE PRUEBA', -999999, '2026-10-20', 'scraper', 'p-2',
          'unbilled', NULL),
        (${card.id}, 'COMPRA MANUAL', -999999, '2026-10-21', 'manual', NULL,
          NULL, NULL)`;
  });

  afterAll(async () => {
    await sql?.end();
    if (admin) {
      await admin.unsafe(`DROP DATABASE IF EXISTS ${dbName} WITH (FORCE)`);
      await admin.end();
    }
  });

  it("reads the stored billing columns into each row and the card block", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-31T15:00:00Z"));
    try {
      const res = await route.GET(
        new Request("https://dashboard.example/api/institutions/banco-de-prueba/products/tarjeta"),
        { params: Promise.resolve({ slug: "banco-de-prueba", product: "tarjeta" }) }
      );
      expect(res.status).toBe(200);
      const body = await res.json();

      expect(
        body.transactions.map((t: { transactionDate: string; billing: unknown }) => [
          t.transactionDate,
          t.billing,
        ])
      ).toEqual([
        ["2026-10-10", { status: "billed", estimated: false, statementDate: "2026-10-15" }],
        ["2026-10-20", { status: "unbilled", estimated: false, statementDate: null }],
        ["2026-10-21", { status: "unbilled", estimated: true, statementDate: null }],
      ]);
      expect(body.billing).toMatchObject({
        asOf: "2026-10-31",
        cycle: {
          basis: "transactions",
          lastClosing: { date: "2026-10-15", estimated: false },
          nextClosing: { date: "2026-11-15", estimated: true },
          dueDate: { date: "2026-11-05", estimated: true },
        },
        billedPending: null,
        unbilledCharges: 1999998,
        unbilledCount: 2,
        complete: true,
      });
    } finally {
      vi.useRealTimers();
    }
  });
});
