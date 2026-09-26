import { NextRequest } from "next/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

// The wiring of /api/fixed-expenses: validation runs before any query, PUT
// checks the stored row first and writes only whitelisted fields. The db is a
// recording double of the few drizzle chains the route uses.

// All identifiers and figures are synthetic (see the repo's personal data
// policy).
const ID = "00000000-0000-4000-8000-000000000001";

const state = {
  selectRows: [] as unknown[],
  updateRows: [] as unknown[],
  inserted: [] as unknown[],
  setPayloads: [] as Record<string, unknown>[],
  deletes: 0,
};

const fakeDb = {
  select: vi.fn(() => ({
    from: () => ({
      where: async () => state.selectRows,
      orderBy: async () => state.selectRows,
    }),
  })),
  insert: vi.fn(() => ({
    values: (values: Record<string, unknown>) => {
      state.inserted.push(values);
      return { returning: async () => [{ id: ID, ...values }] };
    },
  })),
  update: vi.fn(() => ({
    set: (values: Record<string, unknown>) => {
      state.setPayloads.push(values);
      return { where: () => ({ returning: async () => state.updateRows }) };
    },
  })),
  delete: vi.fn(() => ({
    where: async () => {
      state.deletes += 1;
    },
  })),
};

vi.mock("@/lib/db", () => ({ db: fakeDb }));

const { POST, PUT, DELETE } = await import("@/app/api/fixed-expenses/route");

function request(method: string, body: unknown) {
  return new NextRequest(new URL("https://dashboard.example/api/fixed-expenses"), {
    method,
    headers: { "content-type": "application/json" },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
}

const storedRow = {
  id: ID,
  name: "Internet",
  amount: 999_999,
  isShared: false,
  sharedRatio: null,
  activeFrom: "2026-03-01",
  activeTo: null,
};

beforeEach(() => {
  vi.clearAllMocks();
  state.selectRows = [];
  state.updateRows = [];
  state.inserted = [];
  state.setPayloads = [];
  state.deletes = 0;
});

describe("POST /api/fixed-expenses", () => {
  it("answers 400 to a malformed body without touching the db", async () => {
    const response = await POST(request("POST", "{not json"));

    expect(response.status).toBe(400);
    await expect(response.json()).resolves.toEqual({ error: "Invalid JSON body" });
    expect(fakeDb.insert).not.toHaveBeenCalled();
  });

  it("answers 400 naming the field when the name is missing", async () => {
    const response = await POST(request("POST", { amount: 1_000_000 }));

    expect(response.status).toBe(400);
    await expect(response.json()).resolves.toMatchObject({ field: "name" });
    expect(fakeDb.insert).not.toHaveBeenCalled();
  });

  it("inserts the normalized values", async () => {
    const response = await POST(
      request("POST", { name: " Arriendo ", amount: 2_500_000, isShared: true })
    );

    expect(response.status).toBe(201);
    expect(state.inserted).toEqual([
      {
        name: "Arriendo",
        amount: 2_500_000,
        isShared: true,
        sharedRatio: "0.6900",
        activeFrom: null,
        activeTo: null,
      },
    ]);
  });
});

describe("PUT /api/fixed-expenses", () => {
  it("answers 404 for an unknown row", async () => {
    const response = await PUT(request("PUT", { id: ID, name: "Arriendo" }));

    expect(response.status).toBe(404);
    expect(fakeDb.update).not.toHaveBeenCalled();
  });

  it("refuses an end date before the stored start date", async () => {
    state.selectRows = [storedRow];
    const response = await PUT(request("PUT", { id: ID, activeTo: "2026-02-28" }));

    expect(response.status).toBe(400);
    await expect(response.json()).resolves.toMatchObject({ field: "activeTo" });
    expect(fakeDb.update).not.toHaveBeenCalled();
  });

  it("writes only the whitelisted fields plus updatedAt", async () => {
    state.selectRows = [storedRow];
    state.updateRows = [{ ...storedRow, amount: 1_000_000 }];
    const response = await PUT(
      request("PUT", {
        id: ID,
        amount: 1_000_000,
        activeTo: "2026-12-31",
        createdAt: "2020-01-01T00:00:00Z",
      })
    );

    expect(response.status).toBe(200);
    expect(state.setPayloads).toHaveLength(1);
    const payload = state.setPayloads[0];
    expect(Object.keys(payload).sort()).toEqual(["activeTo", "amount", "updatedAt"]);
    expect(payload).toMatchObject({ amount: 1_000_000, activeTo: "2026-12-31" });
    expect(payload.updatedAt).toBeInstanceOf(Date);
    expect(payload).not.toHaveProperty("createdAt");
  });
});

describe("DELETE /api/fixed-expenses", () => {
  it("answers 400 to a non-uuid id without deleting", async () => {
    const response = await DELETE(request("DELETE", { id: "1" }));

    expect(response.status).toBe(400);
    await expect(response.json()).resolves.toMatchObject({ field: "id" });
    expect(state.deletes).toBe(0);
  });
});
