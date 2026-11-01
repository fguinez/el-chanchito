import { beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";
import { argOf, createFakeDb, type RecordedQuery } from "./fake-db";
import { institutions, products } from "@/lib/db/schema";

// PATCH /api/institutions/[slug]/products/[product] on the recording fake db
// (no Postgres): the billing days a credit card takes by hand, merged into
// its attributes. Every id, slug and figure below is synthetic (see the
// repo's personal data policy).
const fake = createFakeDb();
vi.mock("@/lib/db", () => ({ db: fake.db }));

const { PATCH } = await import(
  "@/app/api/institutions/[slug]/products/[product]/route"
);

const INSTITUTION_ID = "00000000-0000-4000-8000-0000000000a1";
const CARD_ID = "00000000-0000-4000-8000-0000000000a2";
const CHECKING_ID = "00000000-0000-4000-8000-0000000000a3";

/** The product row the PATCH locks, as the route selects it. */
function productRow(overrides: Record<string, unknown> = {}) {
  return {
    id: CARD_ID,
    slug: "tarjeta-prueba",
    defaultName: "Tarjeta de prueba",
    parentProductId: null,
    isActive: true,
    currentBalance: "999999",
    kind: "credit_card",
    attributes: {
      kind: "credit_card",
      last4: "1234",
      brand: "Visa Sintetica",
      due_day: 5,
    },
    ...overrides,
  };
}

let row: ReturnType<typeof productRow>;

/** `source` cut down to the columns a select or `.returning` asked for, as
 *  Postgres would answer, so a column the route forgets to read comes back
 *  missing instead of leaking in from the fixture. */
function project(fields: unknown, source: Record<string, unknown>) {
  return Object.fromEntries(
    Object.keys(fields as object).map((key) => [key, source[key]])
  );
}

function respond(query: RecordedQuery): unknown {
  if (query.op === "select") {
    const from = argOf(query, "from");
    if (from === institutions) return [{ id: INSTITUTION_ID }];
    // Both the locked lookup and the institution's product list see only
    // this product.
    if (from === products) return [project(argOf(query, "select"), row)];
    return [];
  }
  if (query.op === "update") {
    const written = { ...row, ...(argOf(query, "set") as object) };
    return [project(argOf(query, "returning"), written)];
  }
  return [];
}

/** The field map of the select that locks the product (`FOR NO KEY UPDATE`). */
function lockedProductFields(): Record<string, unknown> {
  const locked = fake.queries.find(
    (q) =>
      q.op === "select" &&
      argOf(q, "from") === products &&
      q.calls.some((call) => call.method === "for")
  );
  return argOf(locked!, "select") as Record<string, unknown>;
}

beforeEach(() => {
  fake.reset();
  fake.respondWith(respond);
  row = productRow();
});

function patch(body: unknown) {
  return PATCH(
    new NextRequest(
      "https://dashboard.example/api/institutions/banco-prueba/products/tarjeta-prueba",
      {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      }
    ),
    {
      params: Promise.resolve({
        slug: "banco-prueba",
        product: "tarjeta-prueba",
      }),
    }
  );
}

/** The `set` of the route's update, or undefined when it never wrote. */
function updateSet(): Record<string, unknown> | undefined {
  const update = fake.queries.find((q) => q.op === "update");
  return update ? (argOf(update, "set") as Record<string, unknown>) : undefined;
}

describe("PATCH /api/institutions/[slug]/products/[product] billing days", () => {
  it("merges a statement day into a card's attributes, keeping the rest", async () => {
    const res = await patch({ statementDay: 20 });

    expect(res.status).toBe(200);
    const merged = {
      kind: "credit_card",
      last4: "1234",
      brand: "Visa Sintetica",
      statement_day: 20,
      due_day: 5,
    };
    const { updatedAt, ...set } = updateSet()!;
    expect(updatedAt).toBeInstanceOf(Date);
    expect(set).toEqual({ attributes: merged });
    const body = await res.json();
    expect(body.product.attributes).toEqual(merged);
  });

  it("locks the product reading the kind and attributes the merge needs", async () => {
    await patch({ statementDay: 20 });

    const fields = lockedProductFields();
    expect(fields.kind).toBe(products.kind);
    expect(fields.attributes).toBe(products.attributes);
  });

  it("returns the product's attributes after the update", async () => {
    await patch({ statementDay: 20 });

    const update = fake.queries.find((q) => q.op === "update")!;
    const returning = argOf(update, "returning") as Record<string, unknown>;
    expect(returning.attributes).toBe(products.attributes);
  });

  it("clears a day sent as null and leaves the other attributes", async () => {
    const res = await patch({ dueDay: null });

    expect(res.status).toBe(200);
    expect(updateSet()?.attributes).toEqual({
      kind: "credit_card",
      last4: "1234",
      brand: "Visa Sintetica",
    });
  });

  it("writes billing days next to a rename in one update", async () => {
    const res = await patch({ name: "Tarjeta renombrada", statementDay: 1, dueDay: 31 });

    expect(res.status).toBe(200);
    expect(updateSet()).toMatchObject({
      name: "Tarjeta renombrada",
      attributes: { last4: "1234", statement_day: 1, due_day: 31 },
    });
  });

  it.each([
    ["an out-of-range statement day", { statementDay: 32 }, "statementDay"],
    ["a zero due day", { dueDay: 0 }, "dueDay"],
    ["a fractional statement day", { statementDay: 7.5 }, "statementDay"],
    ["a string due day", { dueDay: "5" }, "dueDay"],
  ])("answers 400 for %s before any query", async (_label, body, field) => {
    const res = await patch(body);

    expect(res.status).toBe(400);
    expect(await res.json()).toEqual({
      error: expect.stringContaining(field),
      field,
      issues: [{ path: field, message: expect.any(String) }],
    });
    expect(fake.queries).toHaveLength(0);
  });

  it("answers 400 and never writes for a product that is not a credit card", async () => {
    row = productRow({ id: CHECKING_ID, kind: "checking", attributes: {} });

    const res = await patch({ statementDay: 20 });

    expect(res.status).toBe(400);
    const error = "Field 'statementDay' only applies to credit cards";
    expect(await res.json()).toEqual({
      error,
      field: "statementDay",
      issues: [{ path: "statementDay", message: error }],
    });
    expect(updateSet()).toBeUndefined();
  });

  it("leaves the attributes alone when the days already hold their values", async () => {
    const res = await patch({ statementDay: null, dueDay: 5 });

    expect(res.status).toBe(200);
    expect(updateSet()).not.toHaveProperty("attributes");
  });
});
