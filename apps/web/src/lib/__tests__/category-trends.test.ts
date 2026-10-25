import { describe, expect, it } from "vitest";
import {
  categoryTrends,
  monthOf,
  percentChange,
  shiftMonth,
  topThisMonth,
  trailingMonths,
  trailingMonthsWindow,
} from "@/lib/category-trends";
import type { CategorySpend, CategorySpendGroup } from "@/lib/category-spend";

// Every id, name and amount is synthetic (see the repo's personal data policy).

const FOOD = "11111111-1111-4111-8111-111111111111";
const FUN = "22222222-2222-4222-8222-222222222222";

function spend(categoryId: string | null, amount: number): CategorySpend {
  return {
    categoryId,
    name: categoryId === FOOD ? "Comida" : categoryId === FUN ? "Ocio" : "Sin categoría",
    color: "#ff0000",
    amount,
    percent: 0,
  };
}

function group(key: string, categories: CategorySpend[]): CategorySpendGroup {
  return {
    key,
    total: categories.reduce((s, c) => s + c.amount, 0),
    categories,
  };
}

describe("month helpers", () => {
  it("shifts across year boundaries", () => {
    expect(shiftMonth("2026-01", -1)).toBe("2025-12");
    expect(shiftMonth("2025-12", 1)).toBe("2026-01");
    expect(shiftMonth("2026-03", -14)).toBe("2025-01");
  });

  it("lists trailing months oldest first", () => {
    expect(trailingMonths("2026-02", 4)).toEqual([
      "2025-11",
      "2025-12",
      "2026-01",
      "2026-02",
    ]);
  });

  it("covers whole months in the trailing window", () => {
    expect(trailingMonthsWindow("2026-02", 3)).toEqual({
      from: "2025-12-01",
      to: "2026-02-28",
    });
  });

  it("reads the local month of a date", () => {
    expect(monthOf(new Date(2026, 0, 31))).toBe("2026-01");
  });
});

describe("categoryTrends", () => {
  const groups = [
    group("2025-09", [spend(FOOD, 999_999)]),
    group("2025-10", [spend(FOOD, 1_000_000)]),
    // 2025-11 has no spend at all and is missing from the groups.
    group("2025-12", [spend(FOOD, 2_000_000), spend(FUN, 1_000_000)]),
    group("2026-01", [spend(FOOD, 2_500_000), spend(null, 1_000_000)]),
  ];

  it("reads this month, last month, the 3-month average and the series", () => {
    const trends = categoryTrends(groups, "2026-01", 6);
    const food = trends.get(FOOD)!;
    expect(food.series).toEqual([0, 999_999, 1_000_000, 0, 2_000_000, 2_500_000]);
    expect(food.thisMonth).toBe(2_500_000);
    expect(food.lastMonth).toBe(2_000_000);
    // 2025-12, 2025-11 (empty) and 2025-10.
    expect(food.average).toBe(1_000_000);
  });

  it("counts months a category skipped as zero", () => {
    const fun = categoryTrends(groups, "2026-01", 6).get(FUN)!;
    expect(fun.thisMonth).toBe(0);
    expect(fun.lastMonth).toBe(1_000_000);
    expect(fun.average).toBeCloseTo(1_000_000 / 3);
  });

  it("keys uncategorized spend as none", () => {
    expect(categoryTrends(groups, "2026-01", 6).get("none")?.thisMonth).toBe(1_000_000);
  });

  it("ignores groups outside the window", () => {
    const trends = categoryTrends(groups, "2026-01", 2);
    expect(trends.get(FOOD)!.series).toEqual([2_000_000, 2_500_000]);
    // The window is too short for the full average: missing months are zero.
    expect(trends.get(FOOD)!.average).toBe(2_000_000 / 3);
  });
});

describe("percentChange", () => {
  it("is relative to the previous value", () => {
    expect(percentChange(2_500_000, 2_000_000)).toBe(25);
    expect(percentChange(1_000_000, 2_000_000)).toBe(-50);
  });

  it("is null with nothing before", () => {
    expect(percentChange(1_000_000, 0)).toBeNull();
  });
});

describe("topThisMonth", () => {
  it("ranks this month's spenders and drops the rest", () => {
    const trends = categoryTrends(
      [
        group("2025-12", [spend(FUN, 2_500_000)]),
        group("2026-01", [spend(FOOD, 1_000_000), spend(null, 2_500_000)]),
      ],
      "2026-01",
      2
    );
    expect(topThisMonth(trends.values(), 5).map((t) => t.categoryId)).toEqual([
      null,
      FOOD,
    ]);
    expect(topThisMonth(trends.values(), 1)).toHaveLength(1);
  });
});
