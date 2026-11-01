import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import path from "node:path";
import postgres from "postgres";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";

// A credit card movement's billing status as the detail panel reads it
// (GET /api/transactions/[id]) against the card page's list (GET
// /api/institutions/[slug]/products/[product]), on a real PostgreSQL. With no
// statement in the card's metrics the cycle rests on the statement dates of
// the listed movements, so both routes must read the same rows to agree. Runs
// only with TEST_DATABASE_URL naming a disposable server (`make test-db-up`
// starts one): the suite creates its own chanchito_test_* database, applies
// every migration with migrate.mjs, and drops it at the end. Without the
// variable it is skipped. Provider rates are stubbed so nothing reaches the
// network.
//
// Every name, date and amount is synthetic (see the repo's personal data
// policy): fake cards ending in 1234 and 0000, $ 1.000.000, $ 999.999 and
// $ 2.500.000 movements.

vi.mock("@/lib/rate-book", () => ({
  getRateBook: async () => ({ provider: { CLP: 1 }, overrides: [] }),
}));

const ADMIN_URL = process.env.TEST_DATABASE_URL;
const MIGRATE = path.resolve(__dirname, "../../../../../packages/db-schema/migrate.mjs");

type ProductRoute =
  typeof import("@/app/api/institutions/[slug]/products/[product]/route");
type DetailRoute = typeof import("@/app/api/transactions/[id]/route");

interface ListedRow {
  id: string;
  transactionDate: string;
  billing: unknown;
}

describe.skipIf(!ADMIN_URL)("detail panel billing vs the card's list (real PostgreSQL)", () => {
  const dbName = `chanchito_test_${randomUUID().replace(/-/g, "")}`;
  let admin: ReturnType<typeof postgres>;
  let sql: ReturnType<typeof postgres>;
  let productRoute: ProductRoute;
  let detailRoute: DetailRoute;
  let olderBilledId: string;

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
    productRoute = await import("@/app/api/institutions/[slug]/products/[product]/route");
    detailRoute = await import("@/app/api/transactions/[id]/route");

    const [user] = await sql`INSERT INTO users (name) VALUES ('Usuario de prueba') RETURNING id`;
    const [inst] = await sql`
      INSERT INTO institutions (slug, name) VALUES ('banco-de-prueba', 'Banco de Prueba')
      RETURNING id`;
    const [account] = await sql`
      INSERT INTO accounts (user_id, institution_id) VALUES (${user.id}, ${inst.id})
      RETURNING id`;
    // No statement in the metrics and no billing day in the attributes: only
    // the movements' statement dates tell the cycle.
    const metrics = { kind: "credit_card", available: 2500000 };
    const card = async (slug: string, last4: string) => {
      const attributes = { kind: "credit_card", last4, due_day: 5 };
      // A distinct external_ref keeps two cards of one account apart.
      const [row] = await sql`
        INSERT INTO products (account_id, kind, name, default_name, slug, currency,
          external_ref, attributes, metrics)
        VALUES (${account.id}, 'credit_card', 'Tarjeta de prueba', 'Tarjeta de prueba',
          ${slug}, 'CLP', ${`tarjeta-${last4}`}, ${sql.json(attributes)},
          ${sql.json(metrics)})
        RETURNING id`;
      return row.id as string;
    };

    const shortCard = await card("tarjeta", "1234");
    await sql`
      INSERT INTO transactions (product_id, description, amount, transaction_date,
        source, external_id, billing_status, statement_date)
      VALUES
        (${shortCard}, 'COMPRA DE PRUEBA', -1000000, '2026-10-10', 'scraper', 'p-1',
          'billed', '2026-10-15'),
        (${shortCard}, 'COMPRA MANUAL', -999999, '2026-10-12', 'manual', NULL,
          NULL, NULL),
        (${shortCard}, 'COMPRA DE PRUEBA', -999999, '2026-10-20', 'scraper', 'p-2',
          'unbilled', NULL),
        (${shortCard}, 'COMPRA MANUAL', -1000000, '2026-10-21', 'manual', NULL,
          NULL, NULL),
        (${shortCard}, 'PAGO DE PRUEBA', 2500000, '2026-10-25', 'scraper', 'p-3',
          'unbilled', NULL)`;

    // A card whose only statement date sits on a movement older than the
    // newest 500 the page lists: the list knows no cycle, so neither may the
    // panel.
    const longCard = await card("tarjeta-larga", "0000");
    const [older] = await sql`
      INSERT INTO transactions (product_id, description, amount, transaction_date,
        source, external_id, billing_status, statement_date)
      VALUES (${longCard}, 'COMPRA DE PRUEBA', -1000000, '2026-01-10', 'scraper',
        'q-1', 'billed', '2026-01-15')
      RETURNING id`;
    olderBilledId = older.id;
    await sql`
      INSERT INTO transactions (product_id, description, amount, transaction_date,
        source)
      SELECT ${longCard}, 'COMPRA MANUAL', -999999, '2026-10-01', 'manual'
      FROM generate_series(1, 500)`;
  });

  afterAll(async () => {
    await sql?.end();
    if (admin) {
      await admin.unsafe(`DROP DATABASE IF EXISTS ${dbName} WITH (FORCE)`);
      await admin.end();
    }
  });

  async function listed(slug: string): Promise<{
    transactions: ListedRow[];
    transactionsTruncated: boolean;
  }> {
    const res = await productRoute.GET(
      new Request(`https://dashboard.example/api/institutions/banco-de-prueba/products/${slug}`),
      { params: Promise.resolve({ slug: "banco-de-prueba", product: slug }) }
    );
    expect(res.status).toBe(200);
    return res.json();
  }

  async function detailBilling(id: string): Promise<unknown> {
    const res = await detailRoute.GET(
      new Request(`https://dashboard.example/api/transactions/${id}`),
      { params: Promise.resolve({ id }) }
    );
    expect(res.status).toBe(200);
    return (await res.json()).billing;
  }

  async function onOctober31<T>(run: () => Promise<T>): Promise<T> {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-31T15:00:00Z"));
    try {
      return await run();
    } finally {
      vi.useRealTimers();
    }
  }

  it("shows every movement with the status the card's list gives it", async () => {
    await onOctober31(async () => {
      const { transactions } = await listed("tarjeta");
      expect(
        transactions.map((t) => [t.transactionDate, t.billing])
      ).toEqual([
        ["2026-10-10", { status: "billed", estimated: false, statementDate: "2026-10-15" }],
        // Billed only by the cycle the listed statement date tells.
        ["2026-10-12", { status: "billed", estimated: true, statementDate: null }],
        ["2026-10-20", { status: "unbilled", estimated: false, statementDate: null }],
        ["2026-10-21", { status: "unbilled", estimated: true, statementDate: null }],
        ["2026-10-25", { status: "unbilled", estimated: false, statementDate: null }],
      ]);
      for (const t of transactions) {
        expect(await detailBilling(t.id)).toEqual(t.billing);
      }
    });
  });

  it("reads the cycle from the listed movements only", async () => {
    await onOctober31(async () => {
      const { transactions, transactionsTruncated } = await listed("tarjeta-larga");
      expect(transactionsTruncated).toBe(true);
      expect(transactions).toHaveLength(500);
      expect(transactions.every((t) => t.billing === null)).toBe(true);
      expect(await detailBilling(transactions[0].id)).toBeNull();
      expect(await detailBilling(transactions[499].id)).toBeNull();
      // The bank's own status still shows on the movement the list left out.
      expect(await detailBilling(olderBilledId)).toEqual({
        status: "billed",
        estimated: false,
        statementDate: "2026-01-15",
      });
    });
  });
});
