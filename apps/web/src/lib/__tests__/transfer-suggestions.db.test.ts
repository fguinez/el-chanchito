import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import path from "node:path";
import postgres from "postgres";
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { SCORE_SHARED_INSTITUTION_MATCH } from "@/lib/transfer-suggestions";

// The suggestions SQL against a real PostgreSQL. Runs only with
// TEST_DATABASE_URL naming a disposable server (`make test-db-up` starts one;
// the database in the URL is only the maintenance connection): the suite
// creates its own chanchito_test_* database, applies every migration with
// migrate.mjs, and drops it at the end. Without the variable it is skipped.
// Every test starts from the same products and no transactions, and seeds
// the rows it needs.
//
// Every name, description, date and amount is synthetic (see the repo's
// personal data policy): fake products at fake institutions, $ 1.000.000,
// $ 999.999 and $ 2.500.000 movements.

const ADMIN_URL = process.env.TEST_DATABASE_URL;
const TODAY = "2026-09-30";
const MIGRATE = path.resolve(__dirname, "../../../../../packages/db-schema/migrate.mjs");

type Suggestions = typeof import("@/lib/transfer-suggestions-db");
type Transfers = typeof import("@/lib/transfers-db");

describe.skipIf(!ADMIN_URL)("transfer suggestions (real PostgreSQL)", () => {
  const dbName = `chanchito_test_${randomUUID().replace(/-/g, "")}`;
  let admin: ReturnType<typeof postgres>;
  let sql: ReturnType<typeof postgres>;
  let lib: Suggestions;
  let transfersLib: Transfers;
  let ids: Record<string, string>;

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

  /** A snapshot at local noon of `day`, so its local date is `day` in any
   *  time zone the suite runs in. */
  async function snapshot(productKey: string, day: string, balance: number) {
    const [y, m, d] = day.split("-").map(Number);
    const asOf = new Date(y, m - 1, d, 12).toISOString();
    await sql`
      INSERT INTO product_snapshots (product_id, balance, as_of)
      VALUES (${ids[productKey]}, ${balance}, ${asOf})`;
  }

  async function seedProducts() {
    ids = {};
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
    await product("bankDeposit", bank, "term_deposit", "Depósito Plazo Uno");
    await product("fund", funds, "investment", "Fondo Ahorro Viaje");
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
    lib = await import("@/lib/transfer-suggestions-db");
    transfersLib = await import("@/lib/transfers-db");
  });

  beforeEach(async () => {
    await sql`
      TRUNCATE dismissed_transfer_suggestions, confirmed_transfer_patterns,
        internal_transfers, product_snapshots, transactions, products, accounts,
        institutions, users CASCADE`;
    await seedProducts();
  });

  afterAll(async () => {
    await sql?.end();
    if (admin) {
      await admin.unsafe(`DROP DATABASE IF EXISTS ${dbName} WITH (FORCE)`);
      await admin.end();
    }
  });

  function keyOf(suggestion: {
    source: { transactionId: string };
    target: { transactionId: string | null; productId: string };
  }) {
    const name = (id: string | null) =>
      Object.keys(ids).find((key) => ids[key] === id) ?? id;
    return `${name(suggestion.source.transactionId)}>${name(
      suggestion.target.transactionId ?? suggestion.target.productId
    )}`;
  }

  async function listed() {
    return (await lib.loadTransferSuggestions(TODAY)).map(keyOf).sort();
  }

  /** A card payment from checking, one day apart. */
  async function seedPair() {
    await txn("payDebit", "checking", "2026-09-15", -1_000_000, "Pago TC 1234");
    await txn("payCredit", "card", "2026-09-16", 1_000_000, "Pago recibido");
  }

  /** Keeps the feedless products out of a pair test: a row far outside the
   *  lookback gives each a transaction feed. */
  async function giveFeedlessProductsAFeed() {
    await txn("oldDeposit", "bankDeposit", "2025-01-01", 1, "Saldo inicial");
    await txn("oldFund", "fund", "2025-01-01", 1, "Saldo inicial");
  }

  it("lists an open pair with its evidence", async () => {
    await giveFeedlessProductsAFeed();
    await seedPair();

    const suggestions = await lib.loadTransferSuggestions(TODAY);

    expect(suggestions.map(keyOf)).toEqual(["payDebit>payCredit"]);
    expect(suggestions[0]).toMatchObject({
      kind: "pair",
      amount: 1_000_000,
      source: { productName: "Cuenta corriente 00-000-00000-01", date: "2026-09-15" },
      target: { productName: "Tarjeta 1234", amount: 1_000_000 },
      evidence: ["Mismo monto", "1 día de diferencia", "Glosa 'Pago TC 1234'"],
    });
  });

  it.each([
    [
      "a dismissed pair",
      async () => {
        await sql`
          INSERT INTO dismissed_transfer_suggestions (source_transaction_id, target_transaction_id)
          VALUES (${ids.payDebit}, ${ids.payCredit})`;
      },
    ],
    [
      "a debit a transfer links",
      async () => {
        await sql`
          INSERT INTO internal_transfers (description, amount, from_product_id, to_product_id, transfer_date, from_transaction_id)
          VALUES ('Pago TC', 1000000, ${ids.checking}, ${ids.card}, '2026-09-15', ${ids.payDebit})`;
      },
    ],
    [
      "a credit a transfer links",
      async () => {
        await sql`
          INSERT INTO internal_transfers (description, amount, from_product_id, to_product_id, transfer_date, to_transaction_id)
          VALUES ('Pago TC', 1000000, ${ids.checking}, ${ids.card}, '2026-09-15', ${ids.payCredit})`;
      },
    ],
    [
      "a row flagged without a link",
      async () => {
        await sql`
          UPDATE transactions SET is_internal_transfer = true WHERE id = ${ids.payCredit}`;
      },
    ],
  ])("leaves out %s", async (_, arrange) => {
    await giveFeedlessProductsAFeed();
    await seedPair();
    await arrange();

    expect(await listed()).toEqual([]);
  });

  it.each([
    ["seven days apart", "checking", "2026-09-01", "card", "2026-09-08"],
    ["on the same product", "checking", "2026-09-20", "checking", "2026-09-20"],
    ["older than the lookback", "checking", "2026-05-01", "card", "2026-05-01"],
    ["in another currency", "usd", "2026-09-20", "card", "2026-09-20"],
  ])("leaves out a pair %s", async (_, fromKey, fromDate, toKey, toDate) => {
    await giveFeedlessProductsAFeed();
    await txn("debit", fromKey, fromDate, -999_999, "Traspaso");
    await txn("credit", toKey, toDate, 999_999, "Abono");

    expect(await listed()).toEqual([]);
  });

  it("matches a debit naming a feedless product, backed by its balance", async () => {
    await txn("fundDebit", "checking", "2026-09-22", -1_000_000, "Transf a Fondo Ahorro Viaje");
    await snapshot("fund", "2026-09-20", 2_500_000);
    await snapshot("fund", "2026-09-23", 3_500_500);

    const suggestions = await lib.loadTransferSuggestions(TODAY);

    expect(suggestions.map(keyOf)).toEqual(["fundDebit>fund"]);
    expect(suggestions[0]).toMatchObject({
      kind: "one_sided",
      matchedName: "Fondo Ahorro Viaje",
      matchedBy: "product",
      balanceMatch: { date: "2026-09-23", delta: 1_000_500 },
      target: { productName: "Fondo Ahorro Viaje", transactionId: null },
    });
  });

  it("matches the institution name only for a debit at another institution", async () => {
    // Another institution: "Fondos de Prueba" names the fund's institution.
    await txn("elsewhere", "checking", "2026-09-22", -999_999, "Pago a Fondos de Prueba");
    // The deposit's own institution: "Banco de Prueba" in a Banco de Prueba row.
    await txn("sameBank", "checking", "2026-09-23", -999_999, "Banco de Prueba cargo");

    const suggestions = await lib.loadTransferSuggestions(TODAY);

    expect(suggestions.map(keyOf)).toEqual(["elsewhere>fund"]);
    expect(suggestions[0]).toMatchObject({
      matchedName: "Fondos de Prueba",
      matchedBy: "institution",
      institutionSiblings: 0,
    });
  });

  it("scores an institution name shared by several feedless products lower", async () => {
    const [fundsAccount] = await sql`
      SELECT account_id FROM products WHERE id = ${ids.fund}`;
    const addFund = async (key: string, name: string) => {
      const [row] = await sql`
        INSERT INTO products (account_id, kind, name, slug, currency, external_ref)
        VALUES (${fundsAccount.account_id}, 'investment', ${name}, ${key}, 'CLP', ${key})
        RETURNING id`;
      ids[key] = row.id;
    };
    await addFund("fund2", "Fondo Casa Propia");
    // Has a transaction feed: neither a target nor a sibling.
    await addFund("fedFund", "Fondo Con Movimientos");
    await txn("fedRow", "fedFund", "2025-01-01", 1, "Saldo inicial");
    await txn("elsewhere", "checking", "2026-09-22", -999_999, "Pago a Fondos de Prueba");

    const suggestions = await lib.loadTransferSuggestions(TODAY);

    expect(suggestions.map(keyOf).sort()).toEqual(
      ["elsewhere>fund", "elsewhere>fund2"].sort()
    );
    for (const suggestion of suggestions) {
      expect(suggestion).toMatchObject({
        matchedBy: "institution",
        institutionSiblings: 1,
        score: SCORE_SHARED_INSTITUTION_MATCH,
      });
    }
  });

  it("confirms a pair: a resolved transfer, flagged rows and a pattern", async () => {
    await giveFeedlessProductsAFeed();
    await seedPair();

    const outcome = await lib.confirmTransferSuggestion({
      sourceTransactionId: ids.payDebit,
      targetTransactionId: ids.payCredit,
      targetProductId: null,
    });

    expect(outcome.ok).toBe(true);
    const [transfer] = await sql`
      SELECT amount, status, transfer_date::text AS date, to_transaction_id
      FROM internal_transfers WHERE from_transaction_id = ${ids.payDebit}`;
    expect(transfer).toEqual({
      amount: 1_000_000,
      status: "resolved",
      date: "2026-09-15",
      to_transaction_id: ids.payCredit,
    });
    const [flagged] = await sql`
      SELECT count(*)::int AS n FROM transactions
      WHERE id IN (${ids.payDebit}, ${ids.payCredit}) AND is_internal_transfer`;
    expect(flagged.n).toBe(2);
    expect(await lib.loadConfirmedPatterns()).toEqual([
      { sourceProductId: ids.checking, targetProductId: ids.card, descriptionPrefix: "pago tc" },
    ]);
    expect(await listed()).toEqual([]);
  });

  it("answers 409 when the pair is confirmed twice", async () => {
    await seedPair();
    const input = {
      sourceTransactionId: ids.payDebit,
      targetTransactionId: ids.payCredit,
      targetProductId: null,
    };
    await lib.confirmTransferSuggestion(input);

    const outcome = await lib.confirmTransferSuggestion(input);

    expect(outcome).toMatchObject({ ok: false, status: 409, field: "sourceTransactionId" });
  });

  it.each([
    ["a product with a transaction feed", "card"],
    ["a product in another currency", "usd"],
  ])("refuses a one-sided target that is %s, writing nothing", async (_, target) => {
    await txn("fundDebit", "checking", "2026-09-22", -1_000_000, "Transf a Fondo Ahorro Viaje");
    await txn("cardRow", "card", "2026-09-01", -999_999, "Compra");
    await txn("usdRow", "usd", "2026-09-01", -1, "Comisión");

    const outcome = await lib.confirmTransferSuggestion({
      sourceTransactionId: ids.fundDebit,
      targetTransactionId: null,
      targetProductId: ids[target],
    });

    expect(outcome).toMatchObject({ ok: false, status: 400, field: "targetProductId" });
    const [count] = await sql`SELECT count(*)::int AS n FROM internal_transfers`;
    expect(count.n).toBe(0);
  });

  it("confirms a one-sided suggestion with only the source linked", async () => {
    await txn("fundDebit", "checking", "2026-09-22", -1_000_000, "Transf a Fondo Ahorro Viaje");

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

  it("dismisses a one-sided suggestion idempotently", async () => {
    await txn("fundDebit", "checking", "2026-09-22", -1_000_000, "Transf a Fondo Ahorro Viaje");
    const input = { sourceTransactionId: ids.fundDebit, targetTransactionId: null };

    await lib.dismissTransferSuggestion(input);
    const again = await lib.dismissTransferSuggestion(input);

    expect(again).toEqual({ ok: true });
    const [count] = await sql`
      SELECT count(*)::int AS n FROM dismissed_transfer_suggestions
      WHERE source_transaction_id = ${ids.fundDebit}`;
    expect(count.n).toBe(1);
    expect(await listed()).toEqual([]);
  });

  /** A card payment from checking on the same day: 85 points, at the bar. */
  async function seedSameDayPair() {
    await txn("payDebit", "checking", "2026-09-15", -1_000_000, "Pago TC 1234");
    await txn("payCredit", "card", "2026-09-15", 1_000_000, "Pago recibido");
  }

  async function autoTransfers() {
    return sql`
      SELECT id, status, auto_linked, from_transaction_id, to_transaction_id
      FROM internal_transfers WHERE auto_linked`;
  }

  it("links a confident pair on its own, without a pattern, once", async () => {
    await giveFeedlessProductsAFeed();
    await seedSameDayPair();

    const first = await lib.autoLinkSuggestions(TODAY);
    const again = await lib.autoLinkSuggestions(TODAY);

    const [transfer] = await autoTransfers();
    expect(first).toEqual({ suggestions: [], autoLinked: [transfer.id] });
    expect(again).toEqual({ suggestions: [], autoLinked: [] });
    expect(transfer).toMatchObject({
      status: "resolved",
      from_transaction_id: ids.payDebit,
      to_transaction_id: ids.payCredit,
    });
    expect(await lib.loadConfirmedPatterns()).toEqual([]);
  });

  it("leaves a pair below the bar or with a rival partner to the user", async () => {
    await giveFeedlessProductsAFeed();
    await seedPair();
    await txn("rivalDebit", "checking", "2026-09-20", -2_500_000, "Pago TC 1234");
    await txn("rivalA", "card", "2026-09-20", 2_500_000, "Pago recibido");
    await txn("rivalB", "card", "2026-09-20", 2_500_000, "Pago recibido");

    const result = await lib.autoLinkSuggestions(TODAY);

    expect(result.autoLinked).toEqual([]);
    expect(result.suggestions.map(keyOf).sort()).toEqual([
      "payDebit>payCredit",
      "rivalDebit>rivalA",
      "rivalDebit>rivalB",
    ]);
  });

  it("undoes an automatic link for good by deleting it", async () => {
    await giveFeedlessProductsAFeed();
    await seedSameDayPair();
    const { autoLinked } = await lib.autoLinkSuggestions(TODAY);

    expect(await transfersLib.deleteTransfer(autoLinked[0])).toBe(true);

    const [flagged] = await sql`
      SELECT count(*)::int AS n FROM transactions WHERE is_internal_transfer`;
    expect(flagged.n).toBe(0);
    const dismissed = await sql`
      SELECT source_transaction_id, target_transaction_id
      FROM dismissed_transfer_suggestions`;
    expect(dismissed).toEqual([
      { source_transaction_id: ids.payDebit, target_transaction_id: ids.payCredit },
    ]);
    expect(await lib.autoLinkSuggestions(TODAY)).toEqual({
      suggestions: [],
      autoLinked: [],
    });
  });

  it("hands an automatic transfer to the user once its links are edited", async () => {
    await giveFeedlessProductsAFeed();
    await seedSameDayPair();
    const { autoLinked } = await lib.autoLinkSuggestions(TODAY);

    const { db } = await import("@/lib/db");
    const outcome = await db.transaction((tx) =>
      transfersLib.applyTransferLinks(tx, autoLinked[0], { toTransactionId: null })
    );

    expect(outcome).toEqual({ ok: true });
    expect(await autoTransfers()).toEqual([]);
    expect(await lib.autoLinkSuggestions(TODAY)).toEqual({
      suggestions: [],
      autoLinked: [],
    });
  });
});
