import { NextRequest } from "next/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  argOf,
  createFakeDb,
  whereParams,
  type RecordedQuery,
} from "./fake-db";

// Route-level checks of the request validation: bodies and query strings that
// fail are answered with a 400 before any query runs, and legitimate values
// (zeros above all) reach the database. The db and the product resolver are
// recording doubles; every id, amount and date is synthetic (see the repo's
// personal data policy).

const fake = createFakeDb();
const resolveProductId = vi.fn();

vi.mock("@/lib/db", () => ({ db: fake.db }));
vi.mock("@/lib/db/resolve", () => ({
  resolveProductId: (...args: unknown[]) => resolveProductId(...args),
}));

const categories = await import("@/app/api/categories/route");
const fixedExpenses = await import("@/app/api/fixed-expenses/route");
const csvImport = await import("@/app/api/import/route");
const transactions = await import("@/app/api/transactions/route");
const transfers = await import("@/app/api/transfers/route");
const wealth = await import("@/app/api/wealth/route");

const PRODUCT_ID = "11111111-1111-4111-8111-111111111111";
const CATEGORY_ID = "22222222-2222-4222-8222-222222222222";
const ROW_ID = "33333333-3333-4333-8333-333333333333";

type Handler = (request: NextRequest) => Promise<Response>;

function rawRequest(method: string, body?: string, path = "/api/test") {
  return new NextRequest(new URL(`http://localhost:3000${path}`), {
    method,
    headers: { "content-type": "application/json" },
    body,
  });
}

function jsonRequest(method: string, body: unknown) {
  return rawRequest(method, JSON.stringify(body));
}

function onlyQuery(op: RecordedQuery["op"]): RecordedQuery {
  const matching = fake.queries.filter((query) => query.op === op);
  expect(matching).toHaveLength(1);
  return matching[0];
}

async function expectRejected(response: Response, field: string) {
  expect(response.status).toBe(400);
  const body = await response.json();
  expect(body.field).toBe(field);
  expect(body.issues.length).toBeGreaterThan(0);
  expect(fake.queries).toHaveLength(0);
}

beforeEach(() => {
  fake.reset();
  resolveProductId.mockReset();
  resolveProductId.mockResolvedValue(PRODUCT_ID);
});

const MUTATING_ROUTES: [string, string, Handler][] = [
  ["POST /api/categories", "POST", categories.POST],
  ["POST /api/fixed-expenses", "POST", fixedExpenses.POST],
  ["PUT /api/fixed-expenses", "PUT", fixedExpenses.PUT],
  ["DELETE /api/fixed-expenses", "DELETE", fixedExpenses.DELETE],
  ["POST /api/import", "POST", csvImport.POST],
  ["POST /api/transactions", "POST", transactions.POST],
  ["POST /api/transfers", "POST", transfers.POST],
  ["PUT /api/transfers", "PUT", transfers.PUT],
  ["DELETE /api/transfers", "DELETE", transfers.DELETE],
  ["POST /api/wealth", "POST", wealth.POST],
  ["DELETE /api/wealth", "DELETE", wealth.DELETE],
];

describe.each(MUTATING_ROUTES)("%s", (_route, method, handler) => {
  it.each([
    ["an empty body", undefined],
    ["malformed JSON", "{not json"],
  ])("answers 400 to %s without querying", async (_case, body) => {
    const response = await handler(rawRequest(method, body));

    expect(response.status).toBe(400);
    await expect(response.json()).resolves.toEqual({
      error: "Invalid JSON body",
    });
    expect(fake.queries).toHaveLength(0);
    expect(resolveProductId).not.toHaveBeenCalled();
  });

  it.each([["null"], ['"a string"'], ["[]"]])(
    "answers 400 with issues to a %s body",
    async (body) => {
      const response = await handler(rawRequest(method, body));

      expect(response.status).toBe(400);
      const json = await response.json();
      expect(json.error).toEqual(expect.any(String));
      expect(json.issues.length).toBeGreaterThan(0);
      expect(fake.queries).toHaveLength(0);
    }
  );
});

describe("POST /api/categories", () => {
  it("stores a trimmed, lowercased keyword with the default priority", async () => {
    const response = await categories.POST(
      jsonRequest("POST", { keyword: "  UBER ", categoryId: CATEGORY_ID })
    );

    expect(response.status).toBe(201);
    expect(argOf(onlyQuery("insert"), "values")).toEqual({
      keyword: "uber",
      categoryId: CATEGORY_ID,
      priority: 0,
    });
  });

  it("treats a null priority as the default", async () => {
    await categories.POST(
      jsonRequest("POST", { keyword: "uber", categoryId: CATEGORY_ID, priority: null })
    );

    expect(argOf(onlyQuery("insert"), "values")).toMatchObject({ priority: 0 });
  });

  it.each([
    [{ keyword: "   ", categoryId: CATEGORY_ID }, "keyword"],
    [{ keyword: "uber", categoryId: "transport" }, "categoryId"],
    [{ keyword: "uber", categoryId: CATEGORY_ID, priority: 1.5 }, "priority"],
  ])("rejects %j on %s", async (body, field) => {
    await expectRejected(await categories.POST(jsonRequest("POST", body)), field);
  });
});

describe("POST /api/fixed-expenses", () => {
  it("accepts a zero amount", async () => {
    const response = await fixedExpenses.POST(
      jsonRequest("POST", { name: "Gasto de prueba", amount: 0 })
    );

    expect(response.status).toBe(201);
    expect(argOf(onlyQuery("insert"), "values")).toMatchObject({
      name: "Gasto de prueba",
      amount: 0,
      isShared: false,
      sharedRatio: null,
    });
  });

  it("treats a null isShared as not shared", async () => {
    await fixedExpenses.POST(
      jsonRequest("POST", { name: "Gasto de prueba", amount: 1_000_000, isShared: null })
    );

    expect(argOf(onlyQuery("insert"), "values")).toMatchObject({
      isShared: false,
      sharedRatio: null,
    });
  });

  it("fills a blank shared ratio with the default", async () => {
    await fixedExpenses.POST(
      jsonRequest("POST", {
        name: "Gasto de prueba",
        amount: 1_000_000,
        isShared: true,
        sharedRatio: null,
      })
    );

    expect(argOf(onlyQuery("insert"), "values")).toMatchObject({
      isShared: true,
      sharedRatio: "0.6900",
    });
  });

  it("still rounds a fractional amount", async () => {
    await fixedExpenses.POST(
      jsonRequest("POST", { name: "Gasto de prueba", amount: 999_998.5 })
    );

    expect(argOf(onlyQuery("insert"), "values")).toMatchObject({
      amount: 999_999,
    });
  });

  it.each([
    // JSON.stringify(NaN) is null: what a form sent after parseInt("").
    [{ name: "Gasto de prueba", amount: null }, "amount"],
    [{ name: "Gasto de prueba", amount: "1000000" }, "amount"],
    [{ amount: 1_000_000 }, "name"],
    [{ name: "Gasto de prueba", amount: 1_000_000, isShared: true, sharedRatio: 1.5 }, "sharedRatio"],
    [{ name: "Gasto de prueba", amount: 1_000_000, activeFrom: "01/03/2026" }, "activeFrom"],
  ])("rejects %j on %s", async (body, field) => {
    await expectRejected(await fixedExpenses.POST(jsonRequest("POST", body)), field);
  });
});

describe("PUT /api/fixed-expenses", () => {
  it("writes only the named fields, zeros included", async () => {
    const response = await fixedExpenses.PUT(
      jsonRequest("PUT", {
        id: ROW_ID,
        amount: 0,
        createdAt: "not a column you may write",
      })
    );

    expect(response.status).toBe(200);
    const set = argOf(onlyQuery("update"), "set") as Record<string, unknown>;
    expect(set.amount).toBe(0);
    expect(set).not.toHaveProperty("createdAt");
    expect(set.updatedAt).toBeInstanceOf(Date);
  });

  it.each([
    [{ sharedRatio: 0.5 }, "0.5"],
    [{ sharedRatio: null }, null],
    [{ name: "Gasto de prueba" }, undefined],
  ])("maps %j to a sharedRatio of %j", async (fields, expected) => {
    await fixedExpenses.PUT(jsonRequest("PUT", { id: ROW_ID, ...fields }));

    const set = argOf(onlyQuery("update"), "set") as Record<string, unknown>;
    expect(set.sharedRatio).toBe(expected);
    expect(whereParams(onlyQuery("update"))).toEqual([ROW_ID]);
  });

  it("answers 404 for an unknown id", async () => {
    fake.respondWith(() => []);

    const response = await fixedExpenses.PUT(
      jsonRequest("PUT", { id: ROW_ID, name: "Gasto de prueba" })
    );

    expect(response.status).toBe(404);
  });

  it.each([
    [{ name: "Gasto de prueba" }, "id"],
    [{ id: "7", name: "Gasto de prueba" }, "id"],
    [{ id: ROW_ID, name: "" }, "name"],
  ])("rejects %j on %s", async (body, field) => {
    await expectRejected(await fixedExpenses.PUT(jsonRequest("PUT", body)), field);
  });
});

describe("DELETE /api/fixed-expenses", () => {
  it("rejects an id that is not a uuid", async () => {
    await expectRejected(
      await fixedExpenses.DELETE(jsonRequest("DELETE", { id: 7 })),
      "id"
    );
  });
});

describe("POST /api/import", () => {
  const validRow = { description: "Compra de prueba", amount: -999_999, date: "2026-03-05" };

  it("imports valid rows and skips the rest", async () => {
    const response = await csvImport.POST(
      jsonRequest("POST", {
        rows: [
          validRow,
          { ...validRow, amount: "abc" },
          { ...validRow, date: "05/03/2026" },
          { ...validRow, date: "2026-02-30" },
          { ...validRow, description: "" },
          "not a row",
        ],
      })
    );

    await expect(response.json()).resolves.toEqual({
      imported: 1,
      skipped: 5,
      total: 6,
    });
    expect(resolveProductId).toHaveBeenCalledWith("csv_import", "checking");
    expect(argOf(onlyQuery("insert"), "values")).toMatchObject({
      productId: PRODUCT_ID,
      amount: -999_999,
      transactionDate: "2026-03-05",
    });
  });

  it("books a first-of-month row into its own month", async () => {
    await csvImport.POST(
      jsonRequest("POST", { rows: [{ ...validRow, date: "2026-03-01" }] })
    );

    expect(argOf(onlyQuery("insert"), "values")).toMatchObject({
      scheduledMonth: "2026-03-01",
    });
  });

  it("falls back to the default institution and kind on nulls", async () => {
    await csvImport.POST(
      jsonRequest("POST", { rows: [validRow], institution: null, kind: null })
    );

    expect(resolveProductId).toHaveBeenCalledWith("csv_import", "checking");
  });

  it("uses the kind and institution it is given", async () => {
    await csvImport.POST(
      jsonRequest("POST", { rows: [validRow], institution: "banco_demo", kind: "credit_card" })
    );

    expect(resolveProductId).toHaveBeenCalledWith("banco_demo", "credit_card");
  });

  it.each([
    [{ rows: [validRow], kind: "piggy_bank" }, "kind"],
    [{ rows: [validRow], accountType: "piggy_bank" }, "accountType"],
    [{ rows: [] }, "rows"],
    [{ rows: "a,b,c" }, "rows"],
  ])("rejects %j on %s", async (body, field) => {
    await expectRejected(await csvImport.POST(jsonRequest("POST", body)), field);
    expect(resolveProductId).not.toHaveBeenCalled();
  });
});

describe("POST /api/transactions", () => {
  const validBody = {
    description: "Gasto de prueba",
    amount: -1_000_000,
    transactionDate: "2026-03-05",
  };

  it("accepts a zero amount on the manual product", async () => {
    const response = await transactions.POST(
      jsonRequest("POST", { ...validBody, amount: 0 })
    );

    expect(response.status).toBe(201);
    expect(resolveProductId).toHaveBeenCalledWith("manual", "checking");
    expect(argOf(onlyQuery("insert"), "values")).toMatchObject({
      productId: PRODUCT_ID,
      amount: 0,
      source: "manual",
    });
  });

  it("books a first-of-month date into its own month", async () => {
    await transactions.POST(
      jsonRequest("POST", { ...validBody, transactionDate: "2026-03-01" })
    );

    expect(argOf(onlyQuery("insert"), "values")).toMatchObject({
      transactionDate: "2026-03-01",
      scheduledMonth: "2026-03-01",
    });
  });

  it("stores a given scheduledMonth as the first of its month", async () => {
    await transactions.POST(
      jsonRequest("POST", { ...validBody, scheduledMonth: "2026-04-15" })
    );

    expect(argOf(onlyQuery("insert"), "values")).toMatchObject({
      scheduledMonth: "2026-04-01",
    });
  });

  it("takes the legacy accountId as the product", async () => {
    await transactions.POST(jsonRequest("POST", { ...validBody, accountId: PRODUCT_ID }));

    expect(resolveProductId).not.toHaveBeenCalled();
    expect(argOf(onlyQuery("insert"), "values")).toMatchObject({
      productId: PRODUCT_ID,
    });
  });

  it.each([
    [{ ...validBody, transactionDate: "2026-02-30" }, "transactionDate"],
    [{ ...validBody, transactionDate: undefined }, "transactionDate"],
    [{ ...validBody, scheduledMonth: "marzo" }, "scheduledMonth"],
    [{ ...validBody, amount: null }, "amount"],
    [{ ...validBody, description: " " }, "description"],
    [{ ...validBody, productId: "cuenta-corriente" }, "productId"],
  ])("rejects %j on %s", async (body, field) => {
    await expectRejected(await transactions.POST(jsonRequest("POST", body)), field);
    expect(resolveProductId).not.toHaveBeenCalled();
  });
});

describe("GET /api/transactions", () => {
  function list(query: string) {
    return transactions.GET(rawRequest("GET", undefined, `/api/transactions${query}`));
  }

  it("defaults the limit to 50", async () => {
    const response = await list("?month=2026-03-01");

    expect(response.status).toBe(200);
    expect(argOf(onlyQuery("select"), "limit")).toBe(50);
  });

  it("passes a valid limit and product through", async () => {
    await list(`?limit=10&productId=${PRODUCT_ID}`);

    expect(argOf(onlyQuery("select"), "limit")).toBe(10);
    expect(whereParams(onlyQuery("select"))).toEqual([PRODUCT_ID]);
  });

  it("filters by the legacy accountId alias", async () => {
    await list(`?accountId=${PRODUCT_ID}`);

    expect(whereParams(onlyQuery("select"))).toEqual([PRODUCT_ID]);
  });

  it("filters by the month a given day falls in", async () => {
    await list("?month=2026-03-15");

    expect(whereParams(onlyQuery("select"))).toEqual(["2026-03-01"]);
  });

  it.each([
    ["?limit=abc", "limit"],
    ["?limit=0", "limit"],
    ["?limit=501", "limit"],
    ["?month=marzo", "month"],
    ["?productId=cuenta-corriente", "productId"],
  ])("rejects %s on %s", async (query, field) => {
    await expectRejected(await list(query), field);
  });
});

describe("POST /api/transfers", () => {
  it("accepts a zero amount and the legacy account aliases", async () => {
    const response = await transfers.POST(
      jsonRequest("POST", {
        description: "Traspaso de prueba",
        amount: 0,
        transferDate: "2026-03-05",
        fromAccountId: PRODUCT_ID,
      })
    );

    expect(response.status).toBe(201);
    expect(argOf(onlyQuery("insert"), "values")).toMatchObject({
      amount: 0,
      fromProductId: PRODUCT_ID,
      toProductId: null,
      notes: null,
    });
  });

  it.each([
    [{ description: "Traspaso de prueba", amount: 2_500_000 }, "transferDate"],
    [{ description: "Traspaso de prueba", amount: 2_500_000, transferDate: "2026-03-05", toProductId: "x" }, "toProductId"],
  ])("rejects %j on %s", async (body, field) => {
    await expectRejected(await transfers.POST(jsonRequest("POST", body)), field);
  });
});

describe("PUT /api/transfers", () => {
  it("resolves a transfer and leaves its notes alone", async () => {
    const response = await transfers.PUT(
      jsonRequest("PUT", { id: ROW_ID, status: "resolved" })
    );

    expect(response.status).toBe(200);
    const set = argOf(onlyQuery("update"), "set") as Record<string, unknown>;
    expect(set.status).toBe("resolved");
    expect(set.notes).toBeUndefined();
  });

  it("clears the notes on an explicit null", async () => {
    await transfers.PUT(jsonRequest("PUT", { id: ROW_ID, notes: null }));

    const set = argOf(onlyQuery("update"), "set") as Record<string, unknown>;
    expect(set.notes).toBeNull();
    expect(set.status).toBeUndefined();
  });

  it.each([
    [{ id: ROW_ID, status: "done" }, "status"],
    [{ id: ROW_ID, status: "" }, "status"],
    [{ status: "resolved" }, "id"],
  ])("rejects %j on %s", async (body, field) => {
    await expectRejected(await transfers.PUT(jsonRequest("PUT", body)), field);
  });
});

describe("POST /api/wealth", () => {
  it("accepts zero patrimonio and deuda", async () => {
    const response = await wealth.POST(
      jsonRequest("POST", { snapshotDate: "2026-03-05", patrimonio: 0, deuda: 0 })
    );

    expect(response.status).toBe(201);
    expect(argOf(onlyQuery("insert"), "values")).toMatchObject({
      patrimonio: 0,
      deuda: 0,
      fintualBalance: null,
    });
  });

  it.each([
    [{ snapshotDate: "2026-03-05" }, "patrimonio"],
    [{ snapshotDate: "5 de marzo", patrimonio: 1_000_000 }, "snapshotDate"],
    [{ snapshotDate: "2026-03-05", patrimonio: 1_000_000, fintualBalance: "999999" }, "fintualBalance"],
  ])("rejects %j on %s", async (body, field) => {
    await expectRejected(await wealth.POST(jsonRequest("POST", body)), field);
  });
});

describe("DELETE /api/wealth", () => {
  it("explains why a computed point cannot be deleted", async () => {
    const response = await wealth.DELETE(
      jsonRequest("DELETE", { id: "computed-2026-03-05" })
    );

    await expect(response.clone().json()).resolves.toMatchObject({
      error:
        "id: Computed points are derived from product balances and cannot be deleted",
    });
    await expectRejected(response, "id");
  });

  it("deletes a manual snapshot by id", async () => {
    const response = await wealth.DELETE(jsonRequest("DELETE", { id: ROW_ID }));

    expect(response.status).toBe(200);
    expect(whereParams(onlyQuery("delete"))).toEqual([ROW_ID]);
  });
});
