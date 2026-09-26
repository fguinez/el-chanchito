import { describe, it, expect } from "vitest";
import {
  groupAdjustmentsByDay,
  parseMonthParam,
  toApiAdjustment,
  validateAdjustmentInput,
} from "@/lib/monitors/adjustments";

// All figures and identifiers below are synthetic (see the repo's personal
// data policy): fake uuids and obviously fake CLP amounts.
const MONITOR_ID = "11111111-1111-4111-8111-111111111111";
const ADJUSTMENT_ID = "22222222-2222-4222-8222-222222222222";

describe("validateAdjustmentInput: create", () => {
  it("normalizes a full body", () => {
    const result = validateAdjustmentInput({
      adjustmentDate: "2026-07-15",
      amount: -50000,
      description: "  Regalo de cumpleaños  ",
    });
    expect(result).toEqual({
      ok: true,
      value: {
        adjustmentDate: "2026-07-15",
        amount: -50000,
        description: "Regalo de cumpleaños",
      },
    });
  });

  it("defaults a missing or blank description to null", () => {
    const missing = validateAdjustmentInput({
      adjustmentDate: "2026-07-15",
      amount: 30000,
    });
    const blank = validateAdjustmentInput({
      adjustmentDate: "2026-07-15",
      amount: 30000,
      description: "   ",
    });
    expect(missing.ok && missing.value.description).toBeNull();
    expect(blank.ok && blank.value.description).toBeNull();
  });

  it("rounds the amount to the stored 8 decimals", () => {
    const result = validateAdjustmentInput({
      adjustmentDate: "2026-07-15",
      amount: 1234.123456789,
    });
    expect(result.ok && result.value.amount).toBe(1234.12345679);
  });

  it.each([
    [{ amount: 30000 }, "adjustmentDate", "is required"],
    [{ adjustmentDate: "2026-07-15" }, "amount", "is required"],
    [{ adjustmentDate: "15-07-2026", amount: 1 }, "adjustmentDate", "YYYY-MM-DD"],
    [{ adjustmentDate: "2026-02-30", amount: 1 }, "adjustmentDate", "YYYY-MM-DD"],
    [{ adjustmentDate: 20260715, amount: 1 }, "adjustmentDate", "YYYY-MM-DD"],
    [{ adjustmentDate: "2026-07-15", amount: 0 }, "amount", "must not be zero"],
    [{ adjustmentDate: "2026-07-15", amount: 1e-9 }, "amount", "must not be zero"],
    [{ adjustmentDate: "2026-07-15", amount: "30000" }, "amount", "must be a number"],
    [{ adjustmentDate: "2026-07-15", amount: Number.NaN }, "amount", "must be a number"],
    [{ adjustmentDate: "2026-07-15", amount: 1e12 }, "amount", "too large"],
    [
      { adjustmentDate: "2026-07-15", amount: 1, description: 5 },
      "description",
      "string or null",
    ],
    [
      { adjustmentDate: "2026-07-15", amount: 1, description: "x".repeat(201) },
      "description",
      "at most 200",
    ],
  ])("rejects %j on %s", (body, field, message) => {
    const result = validateAdjustmentInput(body);
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.status).toBe(400);
    expect(result.field).toBe(field);
    expect(result.error).toContain(message);
  });

  it.each([null, [], "2026-07-15"])("rejects a non-object body %j", (body) => {
    expect(validateAdjustmentInput(body).ok).toBe(false);
  });
});

describe("validateAdjustmentInput: partial", () => {
  it("returns only the fields present", () => {
    const result = validateAdjustmentInput(
      { amount: 999999 },
      { partial: true }
    );
    expect(result).toEqual({ ok: true, value: { amount: 999999 } });
  });

  it("lets a description be cleared", () => {
    const result = validateAdjustmentInput(
      { description: null },
      { partial: true }
    );
    expect(result).toEqual({ ok: true, value: { description: null } });
  });

  it("still validates the fields it gets", () => {
    const result = validateAdjustmentInput({ amount: 0 }, { partial: true });
    expect(result.ok).toBe(false);
  });

  it("rejects a body with nothing to update", () => {
    const result = validateAdjustmentInput({ note: "x" }, { partial: true });
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.error).toContain("Nothing to update");
  });
});

describe("parseMonthParam", () => {
  it.each([
    [null, { ok: true, value: null }],
    ["2026-07", { ok: true, value: "2026-07" }],
  ])("accepts %j", (raw, expected) => {
    expect(parseMonthParam(raw)).toEqual(expected);
  });

  it.each(["2026-13", "2026-7", "2026-07-01", "julio"])(
    "rejects %j",
    (raw) => {
      const result = parseMonthParam(raw);
      expect(result.ok).toBe(false);
      if (result.ok) return;
      expect(result.field).toBe("month");
    }
  );
});

describe("toApiAdjustment", () => {
  it("reads the NUMERIC amount as a number", () => {
    const createdAt = new Date("2026-07-15T12:00:00Z");
    const adjustment = toApiAdjustment({
      id: ADJUSTMENT_ID,
      monitorId: MONITOR_ID,
      adjustmentDate: "2026-07-15",
      amount: "-50000.00000000",
      description: null,
      createdAt,
      updatedAt: createdAt,
    });
    expect(adjustment.amount).toBe(-50000);
    expect(adjustment.adjustmentDate).toBe("2026-07-15");
  });
});

describe("groupAdjustmentsByDay", () => {
  const adjustments = [
    { id: "a", adjustmentDate: "2026-07-15", amount: -50000 },
    { id: "b", adjustmentDate: "2026-07-03", amount: 30000 },
    { id: "c", adjustmentDate: "2026-07-15", amount: 10000 },
    { id: "d", adjustmentDate: "2026-08-01", amount: 999999 },
  ];

  it("sums same-day adjustments and carries a month-to-date total", () => {
    const days = groupAdjustmentsByDay(adjustments, "2026-07");
    expect(
      days.map((d) => ({
        date: d.adjustmentDate,
        ids: d.entries.map((e) => e.id),
        dayTotal: d.dayTotal,
        runningTotal: d.runningTotal,
      }))
    ).toEqual([
      { date: "2026-07-03", ids: ["b"], dayTotal: 30000, runningTotal: 30000 },
      {
        date: "2026-07-15",
        ids: ["a", "c"],
        dayTotal: -40000,
        runningTotal: -10000,
      },
    ]);
  });

  it("keeps only the requested month", () => {
    expect(groupAdjustmentsByDay(adjustments, "2026-08")).toHaveLength(1);
    expect(groupAdjustmentsByDay(adjustments, "2026-09")).toEqual([]);
  });
});
