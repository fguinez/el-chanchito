import { describe, expect, it } from "vitest";
import {
  DRAFT_ITEM_ID,
  budgetDraft,
  budgetSummary,
  formatIncomeInput,
  incomeShare,
  isIncomePrefix,
  parseIncomeInput,
} from "@/lib/budget";
import type { FixedExpense } from "@/lib/fixed-expenses";

// All figures and identifiers below are synthetic (see the repo's personal
// data policy): fake uuids, the canonical fake CLP amounts, generic names.
const TODAY = "2026-09-15";
const INCOME = 2_500_000;

let nextId = 0;
function item(overrides: Partial<FixedExpense> = {}): FixedExpense {
  nextId += 1;
  return {
    id: `00000000-0000-4000-8000-${String(nextId).padStart(12, "0")}`,
    name: "Arriendo",
    amount: 1_000_000,
    kind: "fixed",
    isShared: false,
    sharedRatio: null,
    activeFrom: null,
    activeTo: null,
    ...overrides,
  };
}

function kindOf(summary: ReturnType<typeof budgetSummary>, kind: string) {
  const found = summary.kinds.find((k) => k.kind === kind);
  if (!found) throw new Error(`no ${kind} in summary`);
  return found;
}

const FORM = {
  name: "Viaje",
  kind: "savings" as const,
  amount: "999999",
  isShared: false,
  sharedRatio: "0.69",
  activeFrom: "",
  activeTo: "",
};

describe("budgetSummary", () => {
  it("splits the income by kind and leaves the rest as remaining", () => {
    const summary = budgetSummary(
      [
        item({ name: "Arriendo", amount: 1_000_000 }),
        item({ name: "Supermercado", kind: "estimated", amount: 500_000 }),
        item({ name: "Viaje", kind: "savings", amount: 250_000 }),
      ],
      INCOME,
      TODAY
    );

    expect(summary.kinds.map((k) => [k.kind, k.total, k.share])).toEqual([
      ["fixed", 1_000_000, 40],
      ["estimated", 500_000, 20],
      ["savings", 250_000, 10],
    ]);
    expect(summary.allocated).toBe(1_750_000);
    expect(summary.allocatedShare).toBe(70);
    expect(summary.remaining).toBe(750_000);
    expect(summary.remainingShare).toBe(30);
    expect(summary.deficit).toBe(0);
  });

  it("always lists the three kinds, empty ones at 0", () => {
    const summary = budgetSummary([], INCOME, TODAY);

    expect(summary.kinds.map((k) => [k.kind, k.total, k.items])).toEqual([
      ["fixed", 0, []],
      ["estimated", 0, []],
      ["savings", 0, []],
    ]);
    expect(summary.remaining).toBe(INCOME);
    expect(summary.remainingShare).toBe(100);
  });

  it("allocates the personal amount of a shared item", () => {
    const summary = budgetSummary(
      [item({ amount: 1_000_000, isShared: true, sharedRatio: "0.5000" })],
      INCOME,
      TODAY
    );

    expect(kindOf(summary, "fixed").items[0]).toMatchObject({
      amount: 500_000,
      share: 20,
    });
  });

  it("leaves scheduled and ended items out", () => {
    const summary = budgetSummary(
      [
        item({ amount: 1_000_000 }),
        item({ amount: 999_999, activeFrom: "2026-10-01" }),
        item({ amount: 999_999, activeTo: "2026-09-14" }),
        item({ amount: 500_000, activeTo: TODAY }),
      ],
      INCOME,
      TODAY
    );

    expect(kindOf(summary, "fixed").total).toBe(1_500_000);
    expect(kindOf(summary, "fixed").items).toHaveLength(2);
  });

  it("lists each kind's items largest first", () => {
    const summary = budgetSummary(
      [
        item({ name: "Luz", amount: 250_000 }),
        item({ name: "Arriendo", amount: 1_000_000 }),
        item({ name: "Agua", amount: 500_000 }),
      ],
      INCOME,
      TODAY
    );

    expect(kindOf(summary, "fixed").items.map((i) => i.name)).toEqual([
      "Arriendo",
      "Agua",
      "Luz",
    ]);
  });

  it("reports a deficit and no remaining when allocations exceed the income", () => {
    const summary = budgetSummary(
      [item({ amount: 2_500_000 }), item({ kind: "savings", amount: 1_000_000 })],
      INCOME,
      TODAY
    );

    expect(summary.allocated).toBe(3_500_000);
    expect(summary.allocatedShare).toBe(140);
    expect(summary.remaining).toBe(0);
    expect(summary.deficit).toBe(1_000_000);
    expect(summary.deficitShare).toBe(40);
  });

  it("counts a negative legacy amount as 0, so Sobra never passes the income", () => {
    const summary = budgetSummary(
      [item({ amount: 1_000_000 }), item({ amount: -999_999 })],
      INCOME,
      TODAY
    );

    expect(kindOf(summary, "fixed").total).toBe(1_000_000);
    expect(kindOf(summary, "fixed").items.map((i) => i.amount)).toEqual([1_000_000, 0]);
    expect(summary.remaining).toBe(1_500_000);
    expect(summary.remainingShare).toBe(60);
  });

  it("has no shares while the income is 0, but still the totals", () => {
    const summary = budgetSummary([item({ amount: 1_000_000 })], 0, TODAY);

    expect(kindOf(summary, "fixed")).toMatchObject({ total: 1_000_000, share: null });
    expect(kindOf(summary, "fixed").items[0].share).toBeNull();
    expect(summary.allocatedShare).toBeNull();
    expect(summary.remaining).toBe(0);
    expect(summary.deficit).toBe(1_000_000);
    expect(summary.deficitShare).toBeNull();
  });

  it("includes a draft in its kind, marked as such", () => {
    const draft = budgetDraft(FORM);
    const summary = budgetSummary([item({ amount: 1_000_000 })], INCOME, TODAY, draft);

    const savings = kindOf(summary, "savings");
    expect(savings).toMatchObject({ total: 999_999, draftAmount: 999_999 });
    expect(savings.items).toEqual([
      {
        id: DRAFT_ITEM_ID,
        name: "Viaje",
        kind: "savings",
        amount: 999_999,
        share: incomeShare(999_999, INCOME),
        draft: true,
      },
    ]);
    expect(kindOf(summary, "fixed").draftAmount).toBe(0);
    expect(summary.allocated).toBe(1_999_999);
  });

  it("leaves out a draft that is not active today", () => {
    const draft = budgetDraft({ ...FORM, activeFrom: "2026-10-01" });
    const summary = budgetSummary([], INCOME, TODAY, draft);

    expect(kindOf(summary, "savings").total).toBe(0);
  });
});

describe("budgetDraft", () => {
  it("builds the draft from the form", () => {
    expect(budgetDraft({ ...FORM, name: " Viaje ", amount: "999998.6" })).toEqual({
      name: "Viaje",
      kind: "savings",
      amount: 999_999,
      isShared: false,
      sharedRatio: null,
      activeFrom: null,
      activeTo: null,
    });
  });

  it("names an unnamed draft", () => {
    expect(budgetDraft({ ...FORM, name: "  " })?.name).toBe("Nuevo item");
  });

  it.each(["", "  ", "abc", "0", "-1000000", "9999999999"])(
    "has no draft for the amount %j",
    (amount) => {
      expect(budgetDraft({ ...FORM, amount })).toBeNull();
    }
  );

  it("keeps a valid ratio and falls back to the default for a half-typed one", () => {
    expect(budgetDraft({ ...FORM, isShared: true, sharedRatio: "0.5" })?.sharedRatio).toBe(
      "0.5"
    );
    expect(budgetDraft({ ...FORM, isShared: true, sharedRatio: "" })?.sharedRatio).toBe(
      "0.6900"
    );
    expect(budgetDraft({ ...FORM, isShared: true, sharedRatio: "2" })?.sharedRatio).toBe(
      "0.6900"
    );
  });

  it("treats an invalid date as no bound", () => {
    expect(budgetDraft({ ...FORM, activeFrom: "2026-02-30", activeTo: "2026-12-31" })).toMatchObject({
      activeFrom: null,
      activeTo: "2026-12-31",
    });
  });
});

describe("parseIncomeInput", () => {
  it.each([
    ["", 0],
    ["   ", 0],
    ["2500000", 2_500_000],
    ["2.500.000", 2_500_000],
    ["$ 2.500.000", 2_500_000],
    ["999.999", 999_999],
    ["0", 0],
    ["2.147.483.647", 2_147_483_647],
  ])("reads %j as %d", (raw, expected) => {
    expect(parseIncomeInput(raw)).toEqual({ ok: true, value: expected });
  });

  it.each([
    "abc",
    "-1000",
    "2,5",
    "2500000,50",
    "25.00",
    "2.500.00",
    "0.500",
    "0500",
    "2.5",
    "2500.000",
  ])("refuses %j as a format error", (raw) => {
    expect(parseIncomeInput(raw)).toEqual({ ok: false, reason: "format" });
  });

  it.each(["2147483648", "99.999.999.999"])("refuses %j as out of range", (raw) => {
    expect(parseIncomeInput(raw)).toEqual({ ok: false, reason: "range" });
  });

  it("reads back what formatIncomeInput writes", () => {
    expect(parseIncomeInput(formatIncomeInput(2_500_000))).toEqual({
      ok: true,
      value: 2_500_000,
    });
    expect(parseIncomeInput(formatIncomeInput(999_999))).toEqual({
      ok: true,
      value: 999_999,
    });
  });
});

describe("isIncomePrefix", () => {
  it.each(["", "2", "2.", "2.5", "2.50", "2.500", "2.500.", "2.500.0", "$ 2.500.000", "0"])(
    "lets %j through while typing",
    (raw) => {
      expect(isIncomePrefix(raw)).toBe(true);
    }
  );

  it.each(["abc", "2,5", "0.5", "05", "2.5000", "-1", "2.500.0000"])(
    "flags %j at once",
    (raw) => {
      expect(isIncomePrefix(raw)).toBe(false);
    }
  );
});
