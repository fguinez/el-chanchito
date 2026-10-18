import { beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";
import { TRANSACTION_NOTES_MAX_LENGTH } from "@/lib/transactions";

// Fake db: insert(table).values(v).returning() echoes the values back.
const valuesMock = vi.fn();
vi.mock("@/lib/db", () => ({
  db: {
    insert: () => ({
      values: (values: Record<string, unknown>) => {
        valuesMock(values);
        return { returning: () => Promise.resolve([{ id: "new", ...values }]) };
      },
    }),
  },
}));
vi.mock("@/lib/db/resolve", () => ({
  resolveProductId: () => Promise.resolve("00000000-0000-4000-8000-000000000002"),
}));

const { POST } = await import("@/app/api/transactions/route");

// Synthetic fixture only.
const BASE = {
  description: "Compra de prueba",
  amount: -999999,
  transactionDate: "2026-01-15",
};

function post(body: unknown) {
  return POST(
    new NextRequest("https://dashboard.example/api/transactions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
  );
}

beforeEach(() => valuesMock.mockReset());

describe("POST /api/transactions location", () => {
  it("creates without a location, leaving it scraper-fillable", async () => {
    const res = await post(BASE);
    expect(res.status).toBe(201);
    expect(valuesMock).toHaveBeenCalledWith(
      expect.objectContaining({
        locationName: null,
        locationLatitude: null,
        locationIsManual: false,
      })
    );
  });

  it("stores a typed-in location as the user's", async () => {
    const res = await post({ ...BASE, location: { name: " Tienda Ficticia " } });
    expect(res.status).toBe(201);
    expect(valuesMock).toHaveBeenCalledWith(
      expect.objectContaining({
        locationName: "Tienda Ficticia",
        locationCity: null,
        locationIsManual: true,
      })
    );
  });

  it("rejects an invalid location with 400 before inserting", async () => {
    const res = await post({ ...BASE, location: { latitude: -33 } });
    expect(res.status).toBe(400);
    expect(await res.json()).toEqual({
      error: expect.any(String),
      field: "location.longitude",
    });
    expect(valuesMock).not.toHaveBeenCalled();
  });
});

describe("POST /api/transactions notes", () => {
  it("stores a whitespace-only note as null", async () => {
    const res = await post({ ...BASE, notes: "  \n\t " });
    expect(res.status).toBe(201);
    expect(valuesMock).toHaveBeenCalledWith(
      expect.objectContaining({ notes: null })
    );
  });

  it("rejects an overlong note with 400 on the notes field", async () => {
    const res = await post({
      ...BASE,
      notes: "x".repeat(TRANSACTION_NOTES_MAX_LENGTH + 1),
    });
    expect(res.status).toBe(400);
    expect((await res.json()).field).toBe("notes");
    expect(valuesMock).not.toHaveBeenCalled();
  });
});
