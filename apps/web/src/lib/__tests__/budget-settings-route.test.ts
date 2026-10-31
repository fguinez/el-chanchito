import { NextRequest } from "next/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { budgetSettings } from "@/lib/db/schema";
import { argOf, createFakeDb } from "./fake-db";

// /api/budget-settings over a recording double of the db: GET reads the
// single row (0 without one), PUT validates before upserting it. Every
// figure is synthetic (see the repo's personal data policy).

const fake = createFakeDb();
vi.mock("@/lib/db", () => ({ db: fake.db }));

const { GET, PUT } = await import("@/app/api/budget-settings/route");

function put(body: unknown) {
  return PUT(
    new NextRequest(new URL("https://dashboard.example/api/budget-settings"), {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    })
  );
}

beforeEach(() => {
  fake.reset();
});

describe("GET /api/budget-settings", () => {
  it("answers an income of 0 while no row exists", async () => {
    const response = await GET();

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual({ monthlyIncome: 0 });
  });

  it("answers the stored income", async () => {
    fake.respondWith(() => [
      { id: true, monthlyIncome: 2_500_000, updatedAt: new Date() },
    ]);

    const response = await GET();

    await expect(response.json()).resolves.toEqual({ monthlyIncome: 2_500_000 });
    expect(argOf(fake.queries[0], "from")).toBe(budgetSettings);
  });
});

describe("PUT /api/budget-settings", () => {
  it("upserts the single row and answers the saved income", async () => {
    const response = await put({ monthlyIncome: 2_500_000, id: false });

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual({ monthlyIncome: 2_500_000 });
    expect(fake.queries).toHaveLength(1);
    const [query] = fake.queries;
    expect(query.op).toBe("insert");
    expect(argOf(query, "insert")).toBe(budgetSettings);
    expect(argOf(query, "values")).toMatchObject({
      id: true,
      monthlyIncome: 2_500_000,
    });
    const conflict = argOf(query, "onConflictDoUpdate") as {
      target: unknown;
      set: Record<string, unknown>;
    };
    expect(conflict.target).toBe(budgetSettings.id);
    expect(conflict.set).toMatchObject({ monthlyIncome: 2_500_000 });
    expect(conflict.set.updatedAt).toBeInstanceOf(Date);
    expect(conflict.set).not.toHaveProperty("id");
  });

  it("accepts an income of 0", async () => {
    const response = await put({ monthlyIncome: 0 });

    expect(response.status).toBe(200);
    expect(argOf(fake.queries[0], "values")).toMatchObject({ monthlyIncome: 0 });
  });

  it.each([
    ["a negative income", { monthlyIncome: -1 }],
    ["a fractional income", { monthlyIncome: 999_999.5 }],
    ["an income past int32", { monthlyIncome: 2_147_483_648 }],
    ["a string income", { monthlyIncome: "2500000" }],
    ["a missing income", {}],
  ])("answers 400 to %s without querying", async (_case, body) => {
    const response = await put(body);

    expect(response.status).toBe(400);
    await expect(response.json()).resolves.toMatchObject({
      field: "monthlyIncome",
      issues: [expect.objectContaining({ path: "monthlyIncome" })],
    });
    expect(fake.queries).toHaveLength(0);
  });
});
