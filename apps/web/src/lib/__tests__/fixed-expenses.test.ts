import { describe, it, expect } from "vitest";
import {
  DEFAULT_SHARED_RATIO,
  fixedExpenseStatus,
  fixedExpenseTotals,
  formatActiveWindow,
  isCalendarDate,
  mergeFixedExpenseUpdate,
  parseFixedExpenseForm,
  ratioInput,
  sortByStatus,
  validateFixedExpenseId,
  validateFixedExpenseInput,
  type FixedExpense,
  type FixedExpenseFormInput,
  type StoredFixedExpense,
} from "@/lib/fixed-expenses";

// All figures and identifiers below are synthetic (see the repo's personal
// data policy): fake uuids, fake CLP amounts, generic expense names.
const ID = "00000000-0000-4000-8000-000000000001";
const TODAY = "2026-09-15";

function expectFailure<T>(result: { ok: true; value: T } | { ok: false }) {
  if (result.ok) throw new Error("expected a validation failure");
  return result as { ok: false; status: 400; error: string; field?: string };
}

function expectOk<T>(result: { ok: true; value: T } | { ok: false }): T {
  if (!result.ok) throw new Error(`unexpected failure: ${JSON.stringify(result)}`);
  return result.value;
}

function row(overrides: Partial<FixedExpense> = {}): FixedExpense {
  return {
    id: ID,
    name: "Internet",
    amount: 1_000_000,
    isShared: false,
    sharedRatio: null,
    activeFrom: null,
    activeTo: null,
    ...overrides,
  };
}

describe("fixedExpenseStatus", () => {
  it.each([
    ["no bounds", null, null, "active"],
    ["starting today", TODAY, null, "active"],
    ["ending today", null, TODAY, "active"],
    ["starting and ending today", TODAY, TODAY, "active"],
    ["starting tomorrow", "2026-09-16", null, "scheduled"],
    ["ended yesterday", null, "2026-09-14", "ended"],
    ["inside its window", "2026-01-01", "2026-12-31", "active"],
  ])("%s -> %s", (_label, activeFrom, activeTo, expected) => {
    expect(fixedExpenseStatus({ activeFrom, activeTo }, TODAY)).toBe(expected);
  });
});

describe("sortByStatus", () => {
  it("puts active first, then scheduled, then ended, stable within each", () => {
    const expenses = [
      { name: "Gimnasio", activeFrom: null, activeTo: "2026-01-31" },
      { name: "Arriendo", activeFrom: null, activeTo: null },
      { name: "Seguro", activeFrom: "2027-01-01", activeTo: null },
      { name: "Internet", activeFrom: "2026-01-01", activeTo: null },
    ];
    expect(sortByStatus(expenses, TODAY).map((e) => e.name)).toEqual([
      "Arriendo",
      "Internet",
      "Seguro",
      "Gimnasio",
    ]);
  });
});

describe("fixedExpenseTotals", () => {
  it("sums only the expenses active today and applies the shared ratio", () => {
    const totals = fixedExpenseTotals(
      [
        row({ amount: 1_000_000 }),
        row({ amount: 2_500_000, isShared: true, sharedRatio: "0.6000" }),
        row({ amount: 999_999, activeTo: "2026-09-14" }),
        row({ amount: 999_999, activeFrom: "2026-09-16" }),
      ],
      TODAY
    );
    expect(totals).toEqual({
      personal: 1_000_000 + 1_500_000,
      full: 1_000_000 + 2_500_000,
      activeCount: 2,
    });
  });

  it("counts a shared expense without a ratio as zero personal amount", () => {
    const totals = fixedExpenseTotals(
      [row({ isShared: true, sharedRatio: null })],
      TODAY
    );
    expect(totals).toEqual({ personal: 0, full: 1_000_000, activeCount: 1 });
  });
});

describe("isCalendarDate", () => {
  it.each(["2026-02-28", "2028-02-29", "2026-12-31"])("accepts %s", (value) => {
    expect(isCalendarDate(value)).toBe(true);
  });

  it.each([
    "2026-02-30",
    "2027-02-29",
    "2026-13-01",
    "2026-9-1",
    "2026-09-01T00:00:00Z",
    "",
    20260901,
    null,
  ])("rejects %s", (value) => {
    expect(isCalendarDate(value)).toBe(false);
  });
});

describe("validateFixedExpenseInput (create)", () => {
  it("applies the defaults and trims the name", () => {
    const value = expectOk(
      validateFixedExpenseInput({ name: "  Arriendo  ", amount: 1_000_000 }, "create")
    );
    expect(value).toEqual({
      name: "Arriendo",
      amount: 1_000_000,
      isShared: false,
      sharedRatio: null,
      activeFrom: null,
      activeTo: null,
    });
  });

  it("rounds the amount", () => {
    const value = expectOk(
      validateFixedExpenseInput({ name: "Internet", amount: 999_999.4 }, "create")
    );
    expect(value.amount).toBe(999_999);
  });

  it("stores the given ratio, or the default, only when shared", () => {
    const given = expectOk(
      validateFixedExpenseInput(
        { name: "Arriendo", amount: 2_500_000, isShared: true, sharedRatio: 0.5 },
        "create"
      )
    );
    expect(given.sharedRatio).toBe("0.5");

    const fallback = expectOk(
      validateFixedExpenseInput(
        { name: "Arriendo", amount: 2_500_000, isShared: true },
        "create"
      )
    );
    expect(fallback.sharedRatio).toBe(DEFAULT_SHARED_RATIO);

    const unshared = expectOk(
      validateFixedExpenseInput(
        { name: "Arriendo", amount: 2_500_000, isShared: false, sharedRatio: 0.5 },
        "create"
      )
    );
    expect(unshared.sharedRatio).toBeNull();
  });

  it("keeps a valid active window", () => {
    const value = expectOk(
      validateFixedExpenseInput(
        {
          name: "Internet",
          amount: 999_999,
          activeFrom: "2026-01-01",
          activeTo: "2026-12-31",
        },
        "create"
      )
    );
    expect(value).toMatchObject({ activeFrom: "2026-01-01", activeTo: "2026-12-31" });
  });

  it("drops unknown keys", () => {
    const value = expectOk(
      validateFixedExpenseInput(
        { name: "Internet", amount: 999_999, createdAt: "2020-01-01", id: ID },
        "create"
      )
    );
    expect(value).not.toHaveProperty("createdAt");
    expect(value).not.toHaveProperty("id");
  });

  it.each<[string, unknown, string]>([
    ["a non-object body", [], "(none)"],
    ["a missing name", { amount: 1_000_000 }, "name"],
    ["a blank name", { name: "   ", amount: 1_000_000 }, "name"],
    ["a non-string name", { name: 42, amount: 1_000_000 }, "name"],
    ["a missing amount", { name: "Arriendo" }, "amount"],
    ["a string amount", { name: "Arriendo", amount: "1000000" }, "amount"],
    ["an amount over int32", { name: "Arriendo", amount: 2_147_483_648 }, "amount"],
    ["an amount under int32", { name: "Arriendo", amount: -2_147_483_649 }, "amount"],
    ["a non-boolean isShared", { name: "Arriendo", amount: 1_000_000, isShared: "yes" }, "isShared"],
    ["a ratio above 1", { name: "Arriendo", amount: 1_000_000, sharedRatio: 1.5 }, "sharedRatio"],
    ["a negative ratio", { name: "Arriendo", amount: 1_000_000, sharedRatio: -0.1 }, "sharedRatio"],
    ["a string ratio", { name: "Arriendo", amount: 1_000_000, sharedRatio: "0.5" }, "sharedRatio"],
    ["an impossible date", { name: "Arriendo", amount: 1_000_000, activeFrom: "2026-02-30" }, "activeFrom"],
    ["an unpadded date", { name: "Arriendo", amount: 1_000_000, activeTo: "2026-9-1" }, "activeTo"],
    ["a numeric date", { name: "Arriendo", amount: 1_000_000, activeTo: 20260901 }, "activeTo"],
    [
      "a window that ends before it starts",
      { name: "Arriendo", amount: 1_000_000, activeFrom: "2026-09-02", activeTo: "2026-09-01" },
      "activeTo",
    ],
  ])("refuses %s", (_label, body, field) => {
    const failure = expectFailure(validateFixedExpenseInput(body, "create"));
    expect(failure.status).toBe(400);
    expect(failure.field ?? "(none)").toBe(field);
  });
});

describe("validateFixedExpenseInput (update)", () => {
  it("returns only the recognized fields present", () => {
    const value = expectOk(
      validateFixedExpenseInput(
        {
          id: ID,
          amount: 2_500_000.6,
          sharedRatio: 0.5,
          activeTo: null,
          createdAt: "2020-01-01T00:00:00Z",
          updatedAt: "2020-01-01T00:00:00Z",
        },
        "update"
      )
    );
    expect(value).toEqual({
      id: ID,
      fields: { amount: 2_500_001, sharedRatio: "0.5", activeTo: null },
    });
  });

  it("accepts a uuid-shaped id that is not RFC 4122", () => {
    const seeded = "00000000-0000-0000-0000-000000000001";
    const value = expectOk(
      validateFixedExpenseInput({ id: seeded, name: "Internet" }, "update")
    );
    expect(value.id).toBe(seeded);
  });

  it.each<[string, unknown, string]>([
    ["a missing id", { name: "Internet" }, "id"],
    ["a non-uuid id", { id: "1", name: "Internet" }, "id"],
    ["a numeric id", { id: 1, name: "Internet" }, "id"],
    ["no recognized field", { id: ID, createdAt: "2020-01-01" }, "(none)"],
    ["a blank name", { id: ID, name: "" }, "name"],
    [
      "a window that ends before it starts",
      { id: ID, activeFrom: "2026-09-02", activeTo: "2026-09-01" },
      "activeTo",
    ],
  ])("refuses %s", (_label, body, field) => {
    const failure = expectFailure(validateFixedExpenseInput(body, "update"));
    expect(failure.field ?? "(none)").toBe(field);
  });
});

describe("mergeFixedExpenseUpdate", () => {
  const stored: StoredFixedExpense = {
    isShared: false,
    sharedRatio: null,
    activeFrom: "2026-03-01",
    activeTo: null,
  };

  it("refuses an end date before the stored start date", () => {
    const failure = expectFailure(
      mergeFixedExpenseUpdate(stored, { activeTo: "2026-02-28" })
    );
    expect(failure.field).toBe("activeTo");
  });

  it("refuses a start date after the stored end date", () => {
    const failure = expectFailure(
      mergeFixedExpenseUpdate(
        { ...stored, activeFrom: null, activeTo: "2026-06-30" },
        { activeFrom: "2026-07-01" }
      )
    );
    expect(failure.field).toBe("activeFrom");
  });

  it("allows moving both bounds past the stored ones at once", () => {
    const values = expectOk(
      mergeFixedExpenseUpdate(
        { ...stored, activeTo: "2026-06-30" },
        { activeFrom: "2027-01-01", activeTo: "2027-12-31" }
      )
    );
    expect(values).toEqual({ activeFrom: "2027-01-01", activeTo: "2027-12-31" });
  });

  it("defaults the ratio when an expense without one becomes shared", () => {
    const values = expectOk(mergeFixedExpenseUpdate(stored, { isShared: true }));
    expect(values).toEqual({ isShared: true, sharedRatio: DEFAULT_SHARED_RATIO });
  });

  it("keeps the stored ratio when a shared expense changes something else", () => {
    const values = expectOk(
      mergeFixedExpenseUpdate(
        { ...stored, isShared: true, sharedRatio: "0.5000" },
        { name: "Arriendo" }
      )
    );
    expect(values).toEqual({ name: "Arriendo" });
  });

  it("writes the default ratio when a legacy shared row without one is renamed", () => {
    const values = expectOk(
      mergeFixedExpenseUpdate(
        { ...stored, isShared: true, sharedRatio: null },
        { name: "Arriendo" }
      )
    );
    expect(values).toEqual({ name: "Arriendo", sharedRatio: DEFAULT_SHARED_RATIO });
  });

  it("clears a leftover ratio when a legacy unshared row is renamed", () => {
    const values = expectOk(
      mergeFixedExpenseUpdate(
        { ...stored, isShared: false, sharedRatio: "0.5000" },
        { name: "Arriendo" }
      )
    );
    expect(values).toEqual({ name: "Arriendo", sharedRatio: null });
  });

  it("clears the ratio when the expense stops being shared", () => {
    const values = expectOk(
      mergeFixedExpenseUpdate(
        { ...stored, isShared: true, sharedRatio: "0.5000" },
        { isShared: false, sharedRatio: "0.5" }
      )
    );
    expect(values).toEqual({ isShared: false, sharedRatio: null });
  });
});

describe("validateFixedExpenseId", () => {
  it("accepts a uuid", () => {
    expect(expectOk(validateFixedExpenseId({ id: ID }))).toEqual({ id: ID });
  });

  it.each<[string, unknown]>([
    ["a missing id", {}],
    ["a non-uuid id", { id: "abc" }],
    ["a non-object body", "abc"],
  ])("refuses %s", (_label, body) => {
    expect(expectFailure(validateFixedExpenseId(body)).status).toBe(400);
  });
});

describe("parseFixedExpenseForm", () => {
  function form(overrides: Partial<FixedExpenseFormInput> = {}): FixedExpenseFormInput {
    return {
      name: "Arriendo",
      amount: "1000000",
      isShared: false,
      sharedRatio: "0.69",
      activeFrom: "",
      activeTo: "",
      ...overrides,
    };
  }

  it("builds the API body: trimmed, rounded, empty dates as null, no ratio when unshared", () => {
    expect(parseFixedExpenseForm(form({ name: " Arriendo ", amount: "999999.4" }))).toEqual({
      ok: true,
      body: {
        name: "Arriendo",
        amount: 999_999,
        isShared: false,
        sharedRatio: null,
        activeFrom: null,
        activeTo: null,
      },
    });
  });

  it("keeps the ratio and the dates of a shared, bounded expense", () => {
    const result = parseFixedExpenseForm(
      form({
        isShared: true,
        sharedRatio: "0.5",
        activeFrom: "2026-01-01",
        activeTo: "2026-12-31",
      })
    );
    expect(result).toMatchObject({
      ok: true,
      body: { sharedRatio: 0.5, activeFrom: "2026-01-01", activeTo: "2026-12-31" },
    });
  });

  it("ignores a bad ratio when the expense is not shared", () => {
    expect(parseFixedExpenseForm(form({ sharedRatio: "abc" })).ok).toBe(true);
  });

  it.each<[Partial<FixedExpenseFormInput>, string]>([
    [{ name: "  " }, "El nombre es obligatorio"],
    [{ amount: "" }, "El monto debe ser un número"],
    [{ amount: "abc" }, "El monto debe ser un número"],
    [{ amount: "9999999999" }, "El monto está fuera de rango"],
    [{ amount: "-9999999999" }, "El monto está fuera de rango"],
    [{ isShared: true, sharedRatio: "" }, "El ratio debe estar entre 0 y 1"],
    [{ isShared: true, sharedRatio: "1.5" }, "El ratio debe estar entre 0 y 1"],
    [{ activeFrom: "2026-02-30" }, "La fecha de inicio no es válida"],
    [{ activeTo: "2026-9-1" }, "La fecha de término no es válida"],
    [
      { activeFrom: "2026-09-02", activeTo: "2026-09-01" },
      "La fecha de inicio no puede ser posterior a la de término",
    ],
  ])("refuses %o with %s", (overrides, error) => {
    expect(parseFixedExpenseForm(form(overrides))).toEqual({ ok: false, error });
  });
});

describe("ratioInput", () => {
  it.each([
    ["0.6900", "0.69"],
    ["0.6550", "0.655"],
    ["1.0000", "1"],
    [null, "0.69"],
  ])("%s -> %s", (stored, expected) => {
    expect(ratioInput(stored)).toBe(expected);
  });
});

describe("formatActiveWindow", () => {
  it.each([
    [null, null, "Sin límite"],
    ["2026-01-01", null, "Desde 01-01-2026"],
    [null, "2026-12-31", "Hasta 31-12-2026"],
    ["2026-01-01", "2026-12-31", "01-01-2026 al 31-12-2026"],
  ])("from %s to %s -> %s", (activeFrom, activeTo, expected) => {
    expect(formatActiveWindow({ activeFrom, activeTo })).toBe(expected);
  });
});
