import { beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";
import { PgDialect } from "drizzle-orm/pg-core";
import type { SQL } from "drizzle-orm";
import {
  createFakeDb,
  argOf,
  whereParams,
  type RecordedQuery,
} from "@/lib/__tests__/fake-db";

// The aliases routes run against the recording fake db (no Postgres). We read
// back the recorded `values`/`set`/`where` to prove each route wires the query
// it should. All descriptions, aliases and ids below are synthetic.
const fake = createFakeDb();
vi.mock("@/lib/db", () => ({ db: fake.db }));

const { GET: LIST, POST } = await import("@/app/api/aliases/route");
const { PATCH, DELETE } = await import("@/app/api/aliases/[id]/route");
const { GET: PREVIEW } = await import("@/app/api/aliases/preview/route");

const ALIAS_ID = "00000000-0000-4000-8000-000000000001";

function jsonRequest(url: string, method: string, body: unknown) {
  return new NextRequest(url, {
    method,
    headers: { "content-type": "application/json" },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
}

/** The recorded query for an op, or throw if the route never ran it. */
function queryOf(op: RecordedQuery["op"]): RecordedQuery {
  const query = fake.queries.find((q) => q.op === op);
  if (!query) throw new Error(`no ${op} query recorded`);
  return query;
}

/** The SQL text of a recorded `where`, lowercased for assertions. */
function whereSql(query: RecordedQuery): string {
  return new PgDialect()
    .sqlToQuery(argOf(query, "where") as SQL)
    .sql.toLowerCase();
}

beforeEach(() => fake.reset());

describe("POST /api/aliases", () => {
  it("inserts the normalized values and answers 201", async () => {
    const res = await POST(
      jsonRequest("https://dashboard.example/api/aliases", "POST", {
        matchDescription: "  COMPRA WEB 1234  ",
        alias: "  Tienda del barrio  ",
        matchType: "substring",
        priority: 5,
      })
    );

    expect(res.status).toBe(201);
    // Create mode always carries the amount condition, defaulting it to null.
    expect(argOf(queryOf("insert"), "values")).toEqual({
      matchDescription: "COMPRA WEB 1234",
      matchType: "substring",
      alias: "Tienda del barrio",
      priority: 5,
      amountMin: null,
      amountMax: null,
      currency: null,
    });
  });

  it("defaults matchType and priority when omitted", async () => {
    const res = await POST(
      jsonRequest("https://dashboard.example/api/aliases", "POST", {
        matchDescription: "COMPRA WEB",
        alias: "Tienda",
      })
    );

    expect(res.status).toBe(201);
    expect(argOf(queryOf("insert"), "values")).toMatchObject({
      matchType: "exact",
      priority: 0,
    });
  });

  it("answers 400 naming the field and never inserts for an invalid body", async () => {
    const res = await POST(
      jsonRequest("https://dashboard.example/api/aliases", "POST", {
        alias: "Tienda",
      })
    );

    expect(res.status).toBe(400);
    await expect(res.json()).resolves.toMatchObject({
      field: "matchDescription",
    });
    expect(fake.queries.some((q) => q.op === "insert")).toBe(false);
  });

  it("inserts the normalized amount condition", async () => {
    const res = await POST(
      jsonRequest("https://dashboard.example/api/aliases", "POST", {
        matchDescription: "COMPRA WEB",
        alias: "Rango",
        amountMin: 1000000,
        amountMax: 2500000,
        currency: "clp",
      })
    );

    expect(res.status).toBe(201);
    expect(argOf(queryOf("insert"), "values")).toMatchObject({
      amountMin: 1000000,
      amountMax: 2500000,
      currency: "CLP",
    });
  });

  it("answers 400 for an amount bound without a currency and never inserts", async () => {
    const res = await POST(
      jsonRequest("https://dashboard.example/api/aliases", "POST", {
        matchDescription: "COMPRA WEB",
        alias: "Rango",
        amountMin: 1000000,
      })
    );

    expect(res.status).toBe(400);
    await expect(res.json()).resolves.toMatchObject({ field: "currency" });
    expect(fake.queries.some((q) => q.op === "insert")).toBe(false);
  });
});

describe("GET /api/aliases", () => {
  it("returns the amount/currency fields of each row", async () => {
    const row = {
      id: ALIAS_ID,
      matchDescription: "COMPRA WEB",
      matchType: "exact",
      alias: "Rango",
      priority: 0,
      amountMin: 1000000,
      amountMax: 2500000,
      currency: "CLP",
      createdAt: "2026-01-01T00:00:00.000Z",
      updatedAt: "2026-01-01T00:00:00.000Z",
    };
    fake.respondWith((query) => (query.op === "select" ? [row] : []));

    const res = await LIST();
    expect(res.status).toBe(200);
    await expect(res.json()).resolves.toEqual([row]);
  });
});

describe("PATCH /api/aliases/[id]", () => {
  it("writes only the changed fields plus updatedAt", async () => {
    fake.respondWith((query) => {
      if (query.op === "select") return [{ id: ALIAS_ID }];
      if (query.op === "update") {
        return [{ id: ALIAS_ID, ...(argOf(query, "set") as object) }];
      }
      return [];
    });

    const res = await PATCH(
      jsonRequest(`https://dashboard.example/api/aliases/${ALIAS_ID}`, "PATCH", {
        alias: "Nuevo nombre",
        priority: 3,
      }),
      { params: Promise.resolve({ id: ALIAS_ID }) }
    );

    expect(res.status).toBe(200);
    const set = argOf(queryOf("update"), "set") as Record<string, unknown>;
    expect(Object.keys(set).sort()).toEqual(["alias", "priority", "updatedAt"]);
    expect(set).toMatchObject({ alias: "Nuevo nombre", priority: 3 });
    expect(set.updatedAt).toBeInstanceOf(Date);
    // The update is scoped to the row's id.
    expect(whereParams(queryOf("update"))).toContain(ALIAS_ID);
  });

  it("rejects a merged condition that leaves a bound without a currency (400)", async () => {
    // Stored row has no currency; the patch sets a bound and omits currency, so
    // the MERGED row is invalid. The route answers 400 instead of a DB CHECK 500.
    fake.respondWith((query) => {
      if (query.op === "select") {
        return [
          { id: ALIAS_ID, amountMin: null, amountMax: null, currency: null },
        ];
      }
      return [];
    });

    const res = await PATCH(
      jsonRequest(`https://dashboard.example/api/aliases/${ALIAS_ID}`, "PATCH", {
        amountMin: 1000000,
      }),
      { params: Promise.resolve({ id: ALIAS_ID }) }
    );

    expect(res.status).toBe(400);
    await expect(res.json()).resolves.toMatchObject({ field: "currency" });
    expect(fake.queries.some((q) => q.op === "update")).toBe(false);
  });

  it("writes a valid amount condition merged over the stored row", async () => {
    // Stored row already carries the currency; the patch narrows a bound.
    fake.respondWith((query) => {
      if (query.op === "select") {
        return [
          { id: ALIAS_ID, amountMin: null, amountMax: null, currency: "CLP" },
        ];
      }
      if (query.op === "update") {
        return [{ id: ALIAS_ID, ...(argOf(query, "set") as object) }];
      }
      return [];
    });

    const res = await PATCH(
      jsonRequest(`https://dashboard.example/api/aliases/${ALIAS_ID}`, "PATCH", {
        amountMin: 1000000,
      }),
      { params: Promise.resolve({ id: ALIAS_ID }) }
    );

    expect(res.status).toBe(200);
    expect(argOf(queryOf("update"), "set")).toMatchObject({ amountMin: 1000000 });
  });

  it("rejects a merged bound order that puts the minimum above the stored maximum (400)", async () => {
    // Stored row has only a maximum; the patch sets a higher minimum, so the
    // MERGED range is inverted. Caught before the write, not by a DB CHECK 500.
    fake.respondWith((query) => {
      if (query.op === "select") {
        return [
          { id: ALIAS_ID, amountMin: null, amountMax: 1000000, currency: "CLP" },
        ];
      }
      return [];
    });

    const res = await PATCH(
      jsonRequest(`https://dashboard.example/api/aliases/${ALIAS_ID}`, "PATCH", {
        amountMin: 2500000,
      }),
      { params: Promise.resolve({ id: ALIAS_ID }) }
    );

    expect(res.status).toBe(400);
    await expect(res.json()).resolves.toMatchObject({ field: "amountMax" });
    expect(fake.queries.some((q) => q.op === "update")).toBe(false);
  });

  it("answers 404 for an unknown id and never updates", async () => {
    fake.respondWith(() => []); // the existence select finds nothing

    const res = await PATCH(
      jsonRequest(`https://dashboard.example/api/aliases/${ALIAS_ID}`, "PATCH", {
        alias: "Nuevo",
      }),
      { params: Promise.resolve({ id: ALIAS_ID }) }
    );

    expect(res.status).toBe(404);
    expect(fake.queries.some((q) => q.op === "update")).toBe(false);
  });

  it("answers 400 to a non-uuid id without touching the db", async () => {
    const res = await PATCH(
      jsonRequest("https://dashboard.example/api/aliases/nope", "PATCH", {
        alias: "Nuevo",
      }),
      { params: Promise.resolve({ id: "nope" }) }
    );

    expect(res.status).toBe(400);
    expect(fake.queries).toHaveLength(0);
  });
});

describe("DELETE /api/aliases/[id]", () => {
  it("hard-deletes the row and returns its id", async () => {
    fake.respondWith((query) => (query.op === "delete" ? [{ id: ALIAS_ID }] : []));

    const res = await DELETE(
      jsonRequest(`https://dashboard.example/api/aliases/${ALIAS_ID}`, "DELETE", {}),
      { params: Promise.resolve({ id: ALIAS_ID }) }
    );

    expect(res.status).toBe(200);
    await expect(res.json()).resolves.toEqual({ id: ALIAS_ID });
    expect(whereParams(queryOf("delete"))).toContain(ALIAS_ID);
  });

  it("answers 404 when nothing was deleted", async () => {
    fake.respondWith(() => []); // the delete returns no row

    const res = await DELETE(
      jsonRequest(`https://dashboard.example/api/aliases/${ALIAS_ID}`, "DELETE", {}),
      { params: Promise.resolve({ id: ALIAS_ID }) }
    );

    expect(res.status).toBe(404);
  });
});

describe("GET /api/aliases/preview", () => {
  // A count select (no limit) and a rows select (with limit) run together.
  function previewResponder() {
    fake.respondWith((query) => {
      if (query.op !== "select") return [];
      const hasLimit = query.calls.some((c) => c.method === "limit");
      return hasLimit ? [] : [{ total: 0 }];
    });
  }

  it("builds the exact predicate, filters soft-deleted, and matches the value", async () => {
    previewResponder();
    const res = await PREVIEW(
      new NextRequest(
        "https://dashboard.example/api/aliases/preview?matchDescription=COMPRA%20WEB&matchType=exact"
      )
    );

    expect(res.status).toBe(200);
    const sql = whereSql(queryOf("select"));
    expect(sql).toContain("btrim");
    expect(sql).toContain("lower");
    expect(sql).toContain("=");
    expect(sql).not.toContain("strpos");
    expect(sql).toContain("deleted_at");
    expect(sql).toContain("is null");
    expect(whereParams(queryOf("select"))).toContain("COMPRA WEB");
  });

  it("applies the amount and currency predicate with their params", async () => {
    previewResponder();
    const res = await PREVIEW(
      new NextRequest(
        "https://dashboard.example/api/aliases/preview?matchDescription=COMPRA%20WEB" +
          "&matchType=exact&amountMin=1000000&amountMax=2500000&currency=clp"
      )
    );

    expect(res.status).toBe(200);
    const sql = whereSql(queryOf("select"));
    // amount bounds on transactions.amount, currency equality on products.
    expect(sql).toContain(">=");
    expect(sql).toContain("<=");
    expect(sql).toContain("currency");
    const params = whereParams(queryOf("select"));
    expect(params).toContain(1000000);
    expect(params).toContain(2500000);
    expect(params).toContain("CLP");
  });

  it("answers 400 to an amount bound without a currency and never queries", async () => {
    const res = await PREVIEW(
      new NextRequest(
        "https://dashboard.example/api/aliases/preview?matchDescription=COMPRA%20WEB&amountMin=1000000"
      )
    );

    expect(res.status).toBe(400);
    await expect(res.json()).resolves.toMatchObject({ field: "currency" });
    expect(fake.queries).toHaveLength(0);
  });

  it("builds the substring predicate with strpos", async () => {
    previewResponder();
    const res = await PREVIEW(
      new NextRequest(
        "https://dashboard.example/api/aliases/preview?matchDescription=compra&matchType=substring"
      )
    );

    expect(res.status).toBe(200);
    const sql = whereSql(queryOf("select"));
    expect(sql).toContain("strpos");
    expect(sql).toContain("> 0");
    expect(sql).toContain("deleted_at");
    expect(sql).toContain("is null");
  });

  it("defaults matchType to exact when absent", async () => {
    previewResponder();
    const res = await PREVIEW(
      new NextRequest(
        "https://dashboard.example/api/aliases/preview?matchDescription=compra"
      )
    );

    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.matchType).toBe("exact");
  });

  /** Every recorded select (the count and the rows) with its where SQL. */
  function previewSelects() {
    return fake.queries
      .filter((q) => q.op === "select")
      .map((query) => ({ query, sql: whereSql(query) }));
  }

  it("narrows both the count and the rows with q, on either description", async () => {
    previewResponder();
    const res = await PREVIEW(
      new NextRequest(
        "https://dashboard.example/api/aliases/preview?matchDescription=compra" +
          "&matchType=substring&q=%20Caf%C3%A9%20PRUEBA%20"
      )
    );

    expect(res.status).toBe(200);
    const selects = previewSelects();
    expect(selects).toHaveLength(2);
    for (const { query, sql } of selects) {
      // The alias's own strpos, then one per search word and field.
      expect(sql.match(/strpos\(/g)).toHaveLength(5);
      expect(sql).toContain("normalize(");
      expect(sql).toContain("alias_for_transaction(");
      expect(whereParams(query)).toEqual(
        expect.arrayContaining(["compra", "cafe", "prueba"])
      );
    }
    // The rows carry what they show today, for the tooltip.
    const rows = selects.find(({ query }) =>
      query.calls.some((c) => c.method === "limit")
    )!.query;
    expect(rows.calls[0].args[0]).toHaveProperty("displayDescription");
  });

  it("ignores a blank q", async () => {
    previewResponder();
    const res = await PREVIEW(
      new NextRequest(
        "https://dashboard.example/api/aliases/preview?matchDescription=compra&q=%20%20"
      )
    );

    expect(res.status).toBe(200);
    for (const { sql } of previewSelects()) expect(sql).not.toContain("normalize(");
  });

  it("answers 400 to a q over the length cap without querying", async () => {
    const res = await PREVIEW(
      new NextRequest(
        `https://dashboard.example/api/aliases/preview?matchDescription=compra&q=${"a".repeat(201)}`
      )
    );

    expect(res.status).toBe(400);
    await expect(res.json()).resolves.toMatchObject({ field: "q" });
    expect(fake.queries).toHaveLength(0);
  });

  it("answers 400 to a blank matchDescription without querying", async () => {
    const res = await PREVIEW(
      new NextRequest(
        "https://dashboard.example/api/aliases/preview?matchDescription=%20%20"
      )
    );

    expect(res.status).toBe(400);
    await expect(res.json()).resolves.toMatchObject({
      field: "matchDescription",
    });
    expect(fake.queries).toHaveLength(0);
  });
});
