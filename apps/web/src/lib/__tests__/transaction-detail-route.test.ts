import { beforeEach, describe, expect, it, vi } from "vitest";

// Fake db: every query-builder step returns the same chain, and the final
// `.limit()` resolves to whatever rows the test queued. No Postgres needed.
const queuedRows: unknown[][] = [];
const selectMock = vi.fn();

vi.mock("@/lib/db", () => {
  const chain: Record<string, unknown> = {};
  for (const step of ["from", "innerJoin", "leftJoin", "where"]) {
    chain[step] = () => chain;
  }
  chain.limit = () => Promise.resolve(queuedRows.shift() ?? []);
  return {
    db: {
      select: (...args: unknown[]) => {
        selectMock(...args);
        return chain;
      },
    },
  };
});

const { GET } = await import("@/app/api/transactions/[id]/route");

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
  selectMock.mockReset();
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
});
