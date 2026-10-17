import { describe, expect, it } from "vitest";
import { z } from "zod";
import {
  formatPath,
  parseJsonBody,
  parseSearchParams,
} from "@/lib/api/validation";
import { clpAmount, isoDate, monthStart, rowId } from "@/lib/api/schemas";

// All figures below are synthetic (see the repo's personal data policy).

function post(body: string | undefined) {
  return new Request("http://localhost:3000/api/test", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body,
  });
}

const schema = z.object({ name: z.string().min(1), amount: clpAmount });

describe("formatPath", () => {
  it.each([
    [[], ""],
    [["amount"], "amount"],
    [["rows", 0, "amount"], "rows[0].amount"],
    [[2], "[2]"],
  ] as [PropertyKey[], string][])("formats %j as %j", (path, expected) => {
    expect(formatPath(path)).toBe(expected);
  });
});

describe("parseJsonBody", () => {
  it("returns the parsed, transformed body", async () => {
    const result = await parseJsonBody(
      post(JSON.stringify({ name: "Arriendo", amount: 999_999.4 })),
      schema
    );

    expect(result).toEqual({
      ok: true,
      data: { name: "Arriendo", amount: 999_999 },
    });
  });

  it.each([
    ["an empty body", undefined],
    ["malformed JSON", "{not json"],
  ])("turns %s into a 400", async (_case, body) => {
    const result = await parseJsonBody(post(body), schema);

    if (result.ok) throw new Error("expected a failure");
    expect(result.response.status).toBe(400);
    await expect(result.response.json()).resolves.toEqual({
      error: "Invalid JSON body",
    });
  });

  it("names the first failing field and lists every issue", async () => {
    const result = await parseJsonBody(
      post(JSON.stringify({ name: "", amount: null })),
      schema
    );

    if (result.ok) throw new Error("expected a failure");
    expect(result.response.status).toBe(400);
    const body = await result.response.json();
    expect(body.field).toBe("name");
    expect(body.error).toMatch(/^name: /);
    expect(body.issues.map((issue: { path: string }) => issue.path)).toEqual([
      "name",
      "amount",
    ]);
  });

  it("reports a root-level failure without a field", async () => {
    const result = await parseJsonBody(post("null"), schema);

    if (result.ok) throw new Error("expected a failure");
    const body = await result.response.json();
    expect(body.field).toBeUndefined();
    expect(body.error).toMatch(/expected object/);
  });
});

describe("parseSearchParams", () => {
  it("checks the query string against the schema", async () => {
    const querySchema = z.object({ limit: z.coerce.number().int() });

    const ok = parseSearchParams(new URLSearchParams("limit=10"), querySchema);
    const bad = parseSearchParams(new URLSearchParams("limit=ten"), querySchema);

    expect(ok).toEqual({ ok: true, data: { limit: 10 } });
    if (bad.ok) throw new Error("expected a failure");
    await expect(bad.response.json()).resolves.toMatchObject({ field: "limit" });
  });
});

describe("clpAmount", () => {
  it.each([
    [0, 0],
    [-2_500_000, -2_500_000],
    [12.5, 13],
    [1_000_000.4, 1_000_000],
  ])("accepts %d as %d", (input, expected) => {
    expect(clpAmount.parse(input)).toBe(expected);
  });

  it.each([[null], ["2500000"], [Number.NaN], [3_000_000_000], [undefined]])(
    "rejects %j",
    (input) => {
      expect(clpAmount.safeParse(input).success).toBe(false);
    }
  );
});

describe("isoDate", () => {
  it.each(["2024-02-29", "2026-12-31"])("accepts %s", (day) => {
    expect(isoDate.safeParse(day).success).toBe(true);
  });

  it.each(["2023-02-29", "2026-02-30", "05/03/2026", "2026-3-5", "", 20260305])(
    "rejects %j",
    (day) => {
      expect(isoDate.safeParse(day).success).toBe(false);
    }
  );
});

describe("rowId", () => {
  it("accepts seeded, non-RFC 4122 ids", () => {
    expect(rowId.safeParse("00000000-0000-0000-0000-000000000001").success).toBe(
      true
    );
  });

  it.each(["", "not-a-uuid", "computed-2026-03-01", 42])("rejects %j", (id) => {
    expect(rowId.safeParse(id).success).toBe(false);
  });
});

describe("monthStart", () => {
  it.each([
    ["2026-03-01", "2026-03-01"],
    ["2026-03-31", "2026-03-01"],
    ["2024-12-15", "2024-12-01"],
  ])("maps %s to %s", (day, expected) => {
    expect(monthStart(day)).toBe(expected);
  });
});
