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
    kind: "fixed",
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

describe("parseFixedExpenseForm", () => {
  function form(overrides: Partial<FixedExpenseFormInput> = {}): FixedExpenseFormInput {
    return {
      name: "Arriendo",
      kind: "fixed",
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
        kind: "fixed",
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

  it("carries the kind into the body", () => {
    expect(parseFixedExpenseForm(form({ kind: "savings" }))).toMatchObject({
      ok: true,
      body: { kind: "savings" },
    });
  });

  it("ignores a bad ratio when the expense is not shared", () => {
    expect(parseFixedExpenseForm(form({ sharedRatio: "abc" })).ok).toBe(true);
  });

  it.each<[Partial<FixedExpenseFormInput>, string]>([
    [{ name: "  " }, "El nombre es obligatorio"],
    [{ kind: "income" as FixedExpenseFormInput["kind"] }, "El tipo no es válido"],
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
