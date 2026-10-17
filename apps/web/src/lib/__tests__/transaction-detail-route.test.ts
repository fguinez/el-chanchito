import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PgDialect } from "drizzle-orm/pg-core";
import type { SQL } from "drizzle-orm";

// Where conditions recorded off the select chain, newest query first unused:
// index 0 is the first `.where()` of the request (the detail/lookup query).
const selectWheres: unknown[] = [];
const updateWheres: unknown[] = [];

/** The SQL text of a recorded `where`, for asserting a filter is applied. */
function whereSql(index: number): string {
  return new PgDialect().sqlToQuery(selectWheres[index] as SQL).sql;
}
function updateWhereSql(index: number): string {
  return new PgDialect().sqlToQuery(updateWheres[index] as SQL).sql;
}

// Fake db: every query-builder step returns the same chain, and the final
// `.limit()` resolves to whatever rows the test queued (or rejects with a
// queued Error). No Postgres needed.
const queuedRows: (unknown[] | Error)[] = [];
const selectMock = vi.fn();
const setMock = vi.fn();
const deleteMock = vi.fn();

function shiftRows() {
  const next = queuedRows.shift() ?? [];
  return next instanceof Error ? Promise.reject(next) : Promise.resolve(next);
}

vi.mock("@/lib/db", () => {
  const chain: Record<string, unknown> = {};
  for (const step of ["from", "innerJoin", "leftJoin"]) {
    chain[step] = () => chain;
  }
  chain.where = (condition: unknown) => {
    selectWheres.push(condition);
    return chain;
  };
  chain.limit = () => shiftRows();
  // PATCH: update(table).set(values).where(...) either `.returning(...)` (the
  // location-only reply) or is awaited directly; both resolve to queued rows.
  const updateChain: Record<string, unknown> = {
    set: (values: unknown) => {
      setMock(values);
      return updateChain;
    },
    where: (condition: unknown) => {
      updateWheres.push(condition);
      return updateChain;
    },
    returning: () => shiftRows(),
    // Awaiting the chain without `.returning()` resolves like a write.
    then: (onFulfilled: (value: unknown) => unknown) =>
      shiftRows().then(onFulfilled),
  };
  // DELETE hard path: delete(table).where(...) is awaited directly.
  const deleteChain: Record<string, unknown> = {
    where: (...args: unknown[]) => {
      deleteMock(...args);
      return shiftRows();
    },
  };
  return {
    db: {
      select: (...args: unknown[]) => {
        selectMock(...args);
        return chain;
      },
      update: () => updateChain,
      delete: () => deleteChain,
    },
  };
});

const { GET, PATCH, DELETE } = await import(
  "@/app/api/transactions/[id]/route"
);

// Synthetic fixtures only: fake ids, descriptions and amounts.
const TXN_ID = "00000000-0000-4000-8000-000000000001";
const PRODUCT_ID = "00000000-0000-4000-8000-000000000002";
const INSTITUTION_ID = "00000000-0000-4000-8000-000000000003";
const CATEGORY_ID = "00000000-0000-4000-8000-000000000004";

function joinedRow(overrides: Record<string, unknown> = {}) {
  return {
    id: TXN_ID,
    productId: PRODUCT_ID,
    categoryId: CATEGORY_ID,
    description: "Compra de prueba",
    amount: -999999,
    transactionDate: "2026-01-15",
    accountingDate: "2026-01-16",
    scheduledMonth: "2026-01-01",
    source: "synthetic",
    externalId: "SYNTH-0001",
    isInternalTransfer: false,
    isManuallyCategorized: true,
    notes: "Nota de prueba",
    locationName: null,
    locationCity: null,
    locationCountry: null,
    locationLatitude: null,
    locationLongitude: null,
    locationIsManual: false,
    createdAt: new Date("2026-01-15T12:00:00.000Z"),
    updatedAt: new Date("2026-01-16T12:00:00.000Z"),
    productName: "Cuenta de prueba",
    productKind: "checking",
    productSlug: "cuenta-prueba",
    productCurrency: "CLP",
    institutionId: INSTITUTION_ID,
    institutionName: "Banco de Prueba",
    institutionSlug: "banco-prueba",
    categoryName: "Categoría de prueba",
    ...overrides,
  };
}

function call(id: string) {
  return GET(new Request(`https://dashboard.example/api/transactions/${id}`), {
    params: Promise.resolve({ id }),
  });
}

beforeEach(() => {
  queuedRows.length = 0;
  selectWheres.length = 0;
  updateWheres.length = 0;
  selectMock.mockReset();
  setMock.mockReset();
  deleteMock.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("GET /api/transactions/[id]", () => {
  it.each(["not-a-uuid", "00000000-0000-4000-8000-00000000000", "1234"])(
    "rejects malformed id %s with 400 before querying",
    async (id) => {
      const res = await call(id);
      expect(res.status).toBe(400);
      expect(await res.json()).toEqual({
        error: expect.any(String),
        field: "id",
      });
      expect(selectMock).not.toHaveBeenCalled();
    }
  );

  it("accepts non-RFC (seeded) guids and uppercase hex", async () => {
    queuedRows.push([], []);
    expect((await call("00000000-0000-0000-0000-000000000000")).status).toBe(404);
    expect((await call("ABCDEF00-0000-0000-0000-00000000000A")).status).toBe(404);
  });

  it("returns 404 for an unknown id", async () => {
    queuedRows.push([]);
    const res = await call(TXN_ID);
    expect(res.status).toBe(404);
    expect(await res.json()).toEqual({ error: expect.any(String) });
    expect(selectMock).toHaveBeenCalledOnce();
  });

  it("returns the full row with product, institution and category", async () => {
    queuedRows.push([joinedRow()]);
    const res = await call(TXN_ID);
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({
      id: TXN_ID,
      productId: PRODUCT_ID,
      categoryId: CATEGORY_ID,
      description: "Compra de prueba",
      amount: -999999,
      transactionDate: "2026-01-15",
      accountingDate: "2026-01-16",
      scheduledMonth: "2026-01-01",
      source: "synthetic",
      externalId: "SYNTH-0001",
      isInternalTransfer: false,
      isManuallyCategorized: true,
      notes: "Nota de prueba",
      location: null,
      locationIsManual: false,
      createdAt: "2026-01-15T12:00:00.000Z",
      updatedAt: "2026-01-16T12:00:00.000Z",
      product: {
        id: PRODUCT_ID,
        name: "Cuenta de prueba",
        kind: "checking",
        slug: "cuenta-prueba",
        currency: "CLP",
      },
      institution: {
        id: INSTITUTION_ID,
        name: "Banco de Prueba",
        slug: "banco-prueba",
      },
      category: { id: CATEGORY_ID, name: "Categoría de prueba" },
      transfer: null,
    });
  });

  it("returns the internal transfer that links the row", async () => {
    const TRANSFER_ID = "00000000-0000-4000-8000-000000000005";
    queuedRows.push(
      [joinedRow({ isInternalTransfer: true })],
      [
        {
          id: TRANSFER_ID,
          description: "Traspaso de prueba",
          amount: 999999,
          transferDate: "2026-01-14",
          status: "resolved",
          fromTransactionId: TXN_ID,
        },
      ]
    );
    const res = await call(TXN_ID);
    expect(res.status).toBe(200);
    expect((await res.json()).transfer).toEqual({
      id: TRANSFER_ID,
      description: "Traspaso de prueba",
      amount: 999999,
      transferDate: "2026-01-14",
      status: "resolved",
      side: "from",
    });
  });

  it("returns category null when the transaction is uncategorized", async () => {
    queuedRows.push([
      joinedRow({
        categoryId: null,
        categoryName: null,
        accountingDate: null,
        externalId: null,
        notes: null,
        isManuallyCategorized: false,
      }),
    ]);
    const res = await call(TXN_ID);
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.category).toBeNull();
    expect(body.categoryId).toBeNull();
    expect(body.accountingDate).toBeNull();
    expect(body.externalId).toBeNull();
    expect(body.notes).toBeNull();
  });

  it("returns 500 with a generic error when the query fails", async () => {
    const consoleError = vi
      .spyOn(console, "error")
      .mockImplementation(() => {});
    queuedRows.push(new Error("synthetic connection failure"));
    const res = await call(TXN_ID);
    expect(res.status).toBe(500);
    expect(await res.json()).toEqual({ error: "Internal error" });
    expect(consoleError).toHaveBeenCalledOnce();
  });
});

describe("GET /api/transactions/[id] location", () => {
  it("returns the stored location fields (synthetic place)", async () => {
    queuedRows.push([
      joinedRow({
        locationName: "Tienda Ficticia",
        locationCity: "Ciudad Ficticia",
        locationCountry: "XX",
        locationLatitude: -33,
        locationLongitude: -70,
        locationIsManual: true,
      }),
    ]);
    const body = await (await call(TXN_ID)).json();
    expect(body.location).toEqual({
      name: "Tienda Ficticia",
      city: "Ciudad Ficticia",
      country: "XX",
      latitude: -33,
      longitude: -70,
    });
    expect(body.locationIsManual).toBe(true);
  });

  it("returns a partial location with the other fields null", async () => {
    queuedRows.push([joinedRow({ locationCity: "Ciudad Ficticia" })]);
    const body = await (await call(TXN_ID)).json();
    expect(body.location).toEqual({
      name: null,
      city: "Ciudad Ficticia",
      country: null,
      latitude: null,
      longitude: null,
    });
  });
});

function patch(id: string, body: unknown) {
  return PATCH(
    new Request(`https://dashboard.example/api/transactions/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: typeof body === "string" ? body : JSON.stringify(body),
    }) as never,
    { params: Promise.resolve({ id }) }
  );
}

describe("PATCH /api/transactions/[id]", () => {
  it("rejects a malformed id before writing", async () => {
    const res = await patch("not-a-uuid", { location: null });
    expect(res.status).toBe(400);
    expect(await res.json()).toEqual({ error: expect.any(String), field: "id" });
    expect(setMock).not.toHaveBeenCalled();
  });

  it.each([
    ["invalid JSON", "{", undefined],
    ["a non-object body", [1], undefined],
    ["an empty body", {}, undefined],
    ["a blank description", { description: "   " }, "description"],
    ["a malformed categoryId", { categoryId: "nope" }, "categoryId"],
    ["a malformed productId", { productId: "nope" }, "productId"],
    ["a non-object location", { location: "Ciudad Ficticia" }, "location"],
    ["an unknown location key", { location: { street: "x" } }, "location.street"],
    ["a non-string city", { location: { city: 7 } }, "location.city"],
    ["an overlong name", { location: { name: "x".repeat(201) } }, "location.name"],
    ["a latitude out of range", { location: { latitude: 90.5, longitude: 0 } }, "location.latitude"],
    ["a longitude out of range", { location: { latitude: 0, longitude: -181 } }, "location.longitude"],
    ["a string coordinate", { location: { latitude: "-33", longitude: -70 } }, "location.latitude"],
    ["a latitude alone", { location: { latitude: -33 } }, "location.longitude"],
    ["a longitude alone", { location: { longitude: -70 } }, "location.latitude"],
  ])("rejects %s with 400", async (_name, body, field) => {
    const res = await patch(TXN_ID, body);
    expect(res.status).toBe(400);
    const json = await res.json();
    expect(json.error).toEqual(expect.any(String));
    expect(json.field).toBe(field);
    expect(setMock).not.toHaveBeenCalled();
  });

  it("stores a trimmed location and marks it manual", async () => {
    queuedRows.push([{ id: TXN_ID }]);
    const res = await patch(TXN_ID, {
      location: {
        name: "  Tienda Ficticia ",
        city: "Ciudad Ficticia",
        country: " ",
        latitude: -33.0000004,
        longitude: -70,
      },
    });
    expect(res.status).toBe(200);
    const location = {
      name: "Tienda Ficticia",
      city: "Ciudad Ficticia",
      country: null,
      latitude: -33,
      longitude: -70,
    };
    expect(await res.json()).toEqual({ location, locationIsManual: true });
    expect(setMock).toHaveBeenCalledWith({
      locationName: "Tienda Ficticia",
      locationCity: "Ciudad Ficticia",
      locationCountry: null,
      locationLatitude: -33,
      locationLongitude: -70,
      locationIsManual: true,
      updatedAt: expect.any(Date),
    });
  });

  it("clears the location with null, still marking it manual", async () => {
    queuedRows.push([{ id: TXN_ID }]);
    const res = await patch(TXN_ID, { location: null });
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ location: null, locationIsManual: true });
    expect(setMock).toHaveBeenCalledWith(
      expect.objectContaining({
        locationName: null,
        locationLatitude: null,
        locationIsManual: true,
      })
    );
  });

  it("treats an all-blank location as a clear", async () => {
    queuedRows.push([{ id: TXN_ID }]);
    const res = await patch(TXN_ID, { location: { city: " ", name: "" } });
    expect(await res.json()).toEqual({ location: null, locationIsManual: true });
  });

  it("returns 404 for an unknown id", async () => {
    queuedRows.push([]);
    const res = await patch(TXN_ID, { location: { city: "Ciudad Ficticia" } });
    expect(res.status).toBe(404);
  });

  it("returns 500 when the update fails", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    queuedRows.push(new Error("synthetic connection failure"));
    const res = await patch(TXN_ID, { location: null });
    expect(res.status).toBe(500);
    expect(await res.json()).toEqual({ error: "Internal error" });
  });
});

describe("GET /api/transactions/[id] excludes soft-deleted rows", () => {
  it("filters deleted_at IS NULL in the detail lookup", async () => {
    queuedRows.push([]); // a deleted (or missing) row returns nothing
    const res = await call(TXN_ID);
    expect(res.status).toBe(404);
    const sql = whereSql(0).toLowerCase();
    expect(sql).toContain("deleted_at");
    expect(sql).toContain("is null");
  });
});

describe("PATCH /api/transactions/[id] field edits", () => {
  it("edits description and returns the full refreshed detail", async () => {
    // update.returning, then the detail + link reads of loadTransactionDetail.
    queuedRows.push([{ id: TXN_ID }], [joinedRow({ description: "Nueva" })], []);
    const res = await patch(TXN_ID, { description: "  Nueva  " });
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.description).toBe("Nueva");
    expect(body.id).toBe(TXN_ID);
    expect(setMock).toHaveBeenCalledWith({
      description: "Nueva",
      updatedAt: expect.any(Date),
    });
    // The update itself is guarded against editing a soft-deleted row.
    const sql = updateWhereSql(0).toLowerCase();
    expect(sql).toContain("deleted_at");
    expect(sql).toContain("is null");
  });

  it("edits amount, date and scheduledMonth together", async () => {
    queuedRows.push([{ id: TXN_ID }], [joinedRow()], []);
    const res = await patch(TXN_ID, {
      amount: 2500000,
      transactionDate: "2026-02-10",
      scheduledMonth: "2026-02-15",
    });
    expect(res.status).toBe(200);
    expect(setMock).toHaveBeenCalledWith({
      amount: 2500000,
      transactionDate: "2026-02-10",
      scheduledMonth: "2026-02-01", // normalized to the first of the month
      updatedAt: expect.any(Date),
    });
  });

  it("preserves cents on a non-CLP amount (does not force an integer)", async () => {
    queuedRows.push([{ id: TXN_ID }], [joinedRow()], []);
    const res = await patch(TXN_ID, { amount: -1234.56 });
    expect(res.status).toBe(200);
    expect(setMock).toHaveBeenCalledWith({
      amount: -1234.56,
      updatedAt: expect.any(Date),
    });
  });

  it("marks a non-null categoryId as manually categorized", async () => {
    queuedRows.push([{ id: TXN_ID }], [joinedRow()], []);
    const res = await patch(TXN_ID, { categoryId: CATEGORY_ID });
    expect(res.status).toBe(200);
    expect(setMock).toHaveBeenCalledWith({
      categoryId: CATEGORY_ID,
      isManuallyCategorized: true,
      updatedAt: expect.any(Date),
    });
  });

  it("clears the category with null without flipping the manual flag", async () => {
    queuedRows.push([{ id: TXN_ID }], [joinedRow()], []);
    const res = await patch(TXN_ID, { categoryId: null });
    expect(res.status).toBe(200);
    const values = setMock.mock.calls[0][0] as Record<string, unknown>;
    expect(values.categoryId).toBeNull();
    expect(values).not.toHaveProperty("isManuallyCategorized");
  });

  it("moves the row to another product", async () => {
    queuedRows.push([{ id: TXN_ID }], [joinedRow()], []);
    const res = await patch(TXN_ID, { productId: PRODUCT_ID });
    expect(res.status).toBe(200);
    expect(setMock).toHaveBeenCalledWith({
      productId: PRODUCT_ID,
      updatedAt: expect.any(Date),
    });
  });

  it("strips unknown keys instead of rejecting them", async () => {
    queuedRows.push([{ id: TXN_ID }], [joinedRow()], []);
    const res = await patch(TXN_ID, { description: "X", nope: "ignored" });
    expect(res.status).toBe(200);
    const values = setMock.mock.calls[0][0] as Record<string, unknown>;
    expect(values).not.toHaveProperty("nope");
  });

  it("returns 404 editing a missing or deleted row", async () => {
    queuedRows.push([]); // update touches no row
    const res = await patch(TXN_ID, { description: "X" });
    expect(res.status).toBe(404);
  });
});

function del(id: string) {
  return DELETE(
    new Request(`https://dashboard.example/api/transactions/${id}`, {
      method: "DELETE",
    }),
    { params: Promise.resolve({ id }) }
  );
}

describe("DELETE /api/transactions/[id]", () => {
  it("rejects a malformed id before reading", async () => {
    const res = await del("not-a-uuid");
    expect(res.status).toBe(400);
    expect(await res.json()).toEqual({ error: expect.any(String), field: "id" });
    expect(selectMock).not.toHaveBeenCalled();
  });

  it("soft-deletes an imported row (external id), keeping it in the table", async () => {
    queuedRows.push([{ externalId: "SYNTH-0001" }], []);
    const res = await del(TXN_ID);
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true, softDeleted: true });
    expect(setMock).toHaveBeenCalledWith(
      expect.objectContaining({ deletedAt: expect.any(Date) })
    );
    expect(deleteMock).not.toHaveBeenCalled();
    // The lookup only considers rows that are not already soft-deleted.
    const sql = whereSql(0).toLowerCase();
    expect(sql).toContain("deleted_at");
    expect(sql).toContain("is null");
  });

  it("hard-deletes a manual row (no external id)", async () => {
    queuedRows.push([{ externalId: null }], []);
    const res = await del(TXN_ID);
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true, softDeleted: false });
    expect(deleteMock).toHaveBeenCalledOnce();
    expect(setMock).not.toHaveBeenCalled();
  });

  it("treats an empty-string external id as manual (hard delete)", async () => {
    queuedRows.push([{ externalId: "" }], []);
    const res = await del(TXN_ID);
    expect((await res.json()).softDeleted).toBe(false);
    expect(deleteMock).toHaveBeenCalledOnce();
  });

  it("returns 404 for an unknown or already-deleted row", async () => {
    queuedRows.push([]);
    const res = await del(TXN_ID);
    expect(res.status).toBe(404);
    expect(setMock).not.toHaveBeenCalled();
    expect(deleteMock).not.toHaveBeenCalled();
  });

  it("returns 500 when the lookup fails", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    queuedRows.push(new Error("synthetic connection failure"));
    const res = await del(TXN_ID);
    expect(res.status).toBe(500);
    expect(await res.json()).toEqual({ error: "Internal error" });
  });
});
