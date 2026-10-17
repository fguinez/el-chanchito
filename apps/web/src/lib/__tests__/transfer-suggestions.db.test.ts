import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import path from "node:path";
import postgres from "postgres";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

// The suggestions SQL against a real PostgreSQL. Runs only with
// TEST_DATABASE_URL naming a disposable server (`make test-db-up` starts one;
// the database in the URL is only the maintenance connection): the suite
// creates its own chanchito_test_* database, applies every migration with
// migrate.mjs, and drops it at the end. Without the variable it is skipped.
//
// Every name, description, date and amount is synthetic (see the repo's
// personal data policy): fake products at fake institutions, $ 1.000.000,
// $ 999.999 and $ 2.500.000 movements.

const ADMIN_URL = process.env.TEST_DATABASE_URL;
const TODAY = "2026-09-30";
const MIGRATE = path.resolve(__dirname, "../../../../../packages/db-schema/migrate.mjs");

type Suggestions = typeof import("@/lib/transfer-suggestions-db");

describe.skipIf(!ADMIN_URL)("transfer suggestions (real PostgreSQL)", () => {
  const dbName = `chanchito_test_${randomUUID().replace(/-/g, "")}`;
  let admin: ReturnType<typeof postgres>;
  let sql: ReturnType<typeof postgres>;
  let lib: Suggestions;
  const ids: Record<string, string> = {};

  async function txn(
    key: string,
    productKey: string,
    date: string,
    amount: number,
    description: string
  ) {
    const [row] = await sql`
      INSERT INTO transactions (product_id, description, amount, transaction_date, source)
      VALUES (${ids[productKey]}, ${description}, ${amount}, ${date}, 'test')
      RETURNING id`;
    ids[key] = row.id;
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
    sql = postgres(url.toString(), { max: 1 });

    const [user] = await sql`INSERT INTO users (name) VALUES ('Usuario de prueba') RETURNING id`;
    const institution = async (slug: string, name: string) => {
      const [row] = await sql`
        INSERT INTO institutions (slug, name) VALUES (${slug}, ${name}) RETURNING id`;
      const [account] = await sql`
        INSERT INTO accounts (user_id, institution_id) VALUES (${user.id}, ${row.id})
        RETURNING id`;
      return account.id as string;
    };
    const bank = await institution("banco-de-prueba", "Banco de Prueba");
    const funds = await institution("fondos-de-prueba", "Fondos de Prueba");
    const product = async (
      key: string,
      account: string,
      kind: string,
      name: string,
      currency = "CLP"
    ) => {
      const [row] = await sql`
        INSERT INTO products (account_id, kind, name, slug, currency)
        VALUES (${account}, ${kind}, ${name}, ${key}, ${currency}) RETURNING id`;
      ids[key] = row.id;
    };
    await product("checking", bank, "checking", "Cuenta corriente 00-000-00000-01");
    await product("card", bank, "credit_card", "Tarjeta 1234");
    await product("usd", bank, "checking", "Cuenta USD 00-000-00000-02", "USD");
    await product("fund", funds, "investment", "Fondo Ahorro Viaje");

    // A pair, one day apart.
    await txn("payDebit", "checking", "2026-09-15", -1_000_000, "Pago TC 1234");
    await txn("payCredit", "card", "2026-09-16", 1_000_000, "Pago recibido");
    // A pair the user dismissed.
    await txn("dismissedDebit", "checking", "2026-09-10", -999_999, "Traspaso a terceros");
    await txn("dismissedCredit", "card", "2026-09-10", 999_999, "Abono");
    await sql`
      INSERT INTO dismissed_transfer_suggestions (source_transaction_id, target_transaction_id)
      VALUES (${ids.dismissedDebit}, ${ids.dismissedCredit})`;
    // A pair whose debit a transfer already links.
    await txn("linkedDebit", "checking", "2026-09-12", -2_500_000, "Traspaso");
    await txn("linkedCredit", "card", "2026-09-12", 2_500_000, "Abono");
    await sql`
      INSERT INTO internal_transfers (description, amount, from_product_id, to_product_id, transfer_date, from_transaction_id)
      VALUES ('Traspaso', 2500000, ${ids.checking}, ${ids.card}, '2026-09-12', ${ids.linkedDebit})`;
    // Seven days apart: outside the window.
    await txn("farDebit", "checking", "2026-09-01", -1_234, "Traspaso");
    await txn("farCredit", "card", "2026-09-08", 1_234, "Abono");
    // Both legs on the same product.
    await txn("sameDebit", "checking", "2026-09-20", -5_000, "Compra");
    await txn("sameCredit", "checking", "2026-09-20", 5_000, "Compra devuelta");
    // Older than the lookback.
    await txn("oldDebit", "checking", "2026-05-01", -7_000, "Traspaso");
    await txn("oldCredit", "card", "2026-05-01", 7_000, "Abono");
    // A debit naming the fund, whose balance rose by about as much.
    await txn("fundDebit", "checking", "2026-09-22", -500_000, "Transf a Fondo Ahorro Viaje");
    await sql`
      INSERT INTO product_snapshots (product_id, balance, as_of) VALUES
        (${ids.fund}, 1000000, '2026-09-20T12:00:00-03:00'),
        (${ids.fund}, 1500500, '2026-09-23T12:00:00-03:00')`;

    lib = await import("@/lib/transfer-suggestions-db");
  });

  afterAll(async () => {
    await sql?.end();
    if (admin) {
      await admin.unsafe(`DROP DATABASE IF EXISTS ${dbName} WITH (FORCE)`);
      await admin.end();
    }
  });

  function keyOf(suggestion: { source: { transactionId: string }; target: { transactionId: string | null; productId: string } }) {
    const name = (id: string | null) =>
      Object.keys(ids).find((key) => ids[key] === id) ?? id;
    return `${name(suggestion.source.transactionId)}>${name(
      suggestion.target.transactionId ?? suggestion.target.productId
    )}`;
  }

  it("lists only the open pairs and the one-sided match", async () => {
    const suggestions = await lib.loadTransferSuggestions(TODAY);

    expect(suggestions.map(keyOf).sort()).toEqual(
      ["fundDebit>fund", "payDebit>payCredit"].sort()
    );
  });

  it("explains the pair and backs the one-sided match with the balance", async () => {
    const suggestions = await lib.loadTransferSuggestions(TODAY);
    const pair = suggestions.find((s) => s.kind === "pair");
    const oneSided = suggestions.find((s) => s.kind === "one_sided");

    expect(pair).toMatchObject({
      amount: 1_000_000,
      source: { productName: "Cuenta corriente 00-000-00000-01", date: "2026-09-15" },
      target: { productName: "Tarjeta 1234", amount: 1_000_000 },
      evidence: ["Mismo monto", "1 día de diferencia", "Glosa 'Pago TC 1234'"],
    });
    expect(oneSided).toMatchObject({
      kind: "one_sided",
      matchedName: "Fondo Ahorro Viaje",
      balanceMatch: { date: "2026-09-23", delta: 500_500 },
      target: { productName: "Fondo Ahorro Viaje", transactionId: null },
    });
  });

  it("confirms a pair: a resolved transfer, flagged rows and a pattern", async () => {
    const outcome = await lib.confirmTransferSuggestion({
      sourceTransactionId: ids.payDebit,
      targetTransactionId: ids.payCredit,
      targetProductId: null,
    });

    expect(outcome.ok).toBe(true);
    const [transfer] = await sql`
      SELECT amount, status, transfer_date::text AS date, from_transaction_id, to_transaction_id
      FROM internal_transfers WHERE from_transaction_id = ${ids.payDebit}`;
    expect(transfer).toMatchObject({
      amount: 1_000_000,
      status: "resolved",
      date: "2026-09-15",
      to_transaction_id: ids.payCredit,
    });
    const flagged = await sql`
      SELECT count(*)::int AS n FROM transactions
      WHERE id IN (${ids.payDebit}, ${ids.payCredit}) AND is_internal_transfer`;
    expect(flagged[0].n).toBe(2);
    const patterns = await lib.loadConfirmedPatterns();
    expect(patterns).toEqual([
      { sourceProductId: ids.checking, targetProductId: ids.card, descriptionPrefix: "pago tc" },
    ]);
    const after = await lib.loadTransferSuggestions(TODAY);
    expect(after.map(keyOf)).toEqual(["fundDebit>fund"]);
  });

  it("answers 409 when the pair is confirmed twice", async () => {
    const outcome = await lib.confirmTransferSuggestion({
      sourceTransactionId: ids.payDebit,
      targetTransactionId: ids.payCredit,
      targetProductId: null,
    });

    expect(outcome).toMatchObject({ ok: false, status: 409, field: "sourceTransactionId" });
  });

  it("refuses a one-sided target with a transaction feed and writes nothing", async () => {
    const before = await sql`SELECT count(*)::int AS n FROM internal_transfers`;

    const outcome = await lib.confirmTransferSuggestion({
      sourceTransactionId: ids.fundDebit,
      targetTransactionId: null,
      targetProductId: ids.card,
    });

    expect(outcome).toMatchObject({ ok: false, status: 400, field: "targetProductId" });
    const after = await sql`SELECT count(*)::int AS n FROM internal_transfers`;
    expect(after[0].n).toBe(before[0].n);
  });

  it("refuses a target product in another currency", async () => {
    const outcome = await lib.confirmTransferSuggestion({
      sourceTransactionId: ids.fundDebit,
      targetTransactionId: null,
      targetProductId: ids.usd,
    });

    expect(outcome).toMatchObject({ ok: false, status: 400, field: "targetProductId" });
  });

  it("dismisses a one-sided suggestion idempotently", async () => {
    const input = { sourceTransactionId: ids.fundDebit, targetTransactionId: null };

    await lib.dismissTransferSuggestion(input);
    const again = await lib.dismissTransferSuggestion(input);

    expect(again).toEqual({ ok: true });
    const rows = await sql`
      SELECT count(*)::int AS n FROM dismissed_transfer_suggestions
      WHERE source_transaction_id = ${ids.fundDebit}`;
    expect(rows[0].n).toBe(1);
    expect(await lib.loadTransferSuggestions(TODAY)).toEqual([]);
  });

  it("confirms a one-sided suggestion with only the source linked", async () => {
    const outcome = await lib.confirmTransferSuggestion({
      sourceTransactionId: ids.fundDebit,
      targetTransactionId: null,
      targetProductId: ids.fund,
    });

    expect(outcome.ok).toBe(true);
    const [transfer] = await sql`
      SELECT status, to_product_id, to_transaction_id
      FROM internal_transfers WHERE from_transaction_id = ${ids.fundDebit}`;
    expect(transfer).toEqual({
      status: "resolved",
      to_product_id: ids.fund,
      to_transaction_id: null,
    });
  });
});
