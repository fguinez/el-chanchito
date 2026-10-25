import { describe, expect, it } from "vitest";
import {
  categoryDetailHref,
  categoryExpensesHref,
  groupSpend,
  monthWindow,
  monthsBetween,
  summarizeSpend,
  UNCATEGORIZED_COLOR,
  UNCATEGORIZED_NAME,
  type SpendCategoryInfo,
  type SpendRow,
} from "@/lib/category-spend";
import { DEFAULT_CATEGORY_COLOR } from "@/lib/categories";

// Every id, name and amount is synthetic (see the repo's personal data policy).

const FOOD = "11111111-1111-4111-8111-111111111111";
const FUN = "22222222-2222-4222-8222-222222222222";
const SALARY = "33333333-3333-4333-8333-333333333333";
const NO_COLOR = "44444444-4444-4444-8444-444444444444";
const PRODUCT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const PRODUCT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

const info = new Map<string, SpendCategoryInfo>([
  [FOOD, { id: FOOD, name: "Comida", color: "#ff0000" }],
  [FUN, { id: FUN, name: "Ocio", color: "#00ff00" }],
  [SALARY, { id: SALARY, name: "Sueldo", color: "#0000ff" }],
  [NO_COLOR, { id: NO_COLOR, name: "Varios", color: null }],
]);

function row(over: Partial<SpendRow>): SpendRow {
  return {
    categoryId: FOOD,
    amount: -1_000_000,
    month: "2026-01",
    productId: PRODUCT_A,
    institutionSlug: "banco-uno",
    ...over,
  };
}

describe("summarizeSpend", () => {
  it("ranks categories by spend with shares of the total", () => {
    const result = summarizeSpend(
      [
        row({ categoryId: FOOD, amount: -2_500_000 }),
        row({ categoryId: FUN, amount: -1_000_000 }),
        row({ categoryId: FOOD, amount: -1_000_000 }),
      ],
      info
    );
    expect(result.total).toBe(4_500_000);
    expect(result.categories.map((c) => [c.name, c.amount])).toEqual([
      ["Comida", 3_500_000],
      ["Ocio", 1_000_000],
    ]);
    expect(result.categories[0].color).toBe("#ff0000");
    expect(result.categories[0].percent).toBeCloseTo((3_500_000 / 4_500_000) * 100);
    expect(result.categories.reduce((s, c) => s + c.percent, 0)).toBeCloseTo(100);
  });

  it("subtracts refunds from their category", () => {
    const result = summarizeSpend(
      [
        row({ categoryId: FOOD, amount: -2_500_000 }),
        row({ categoryId: FOOD, amount: 1_000_000 }),
      ],
      info
    );
    expect(result.categories).toEqual([
      expect.objectContaining({ categoryId: FOOD, amount: 1_500_000, percent: 100 }),
    ]);
  });

  it("drops categories that net to zero or income", () => {
    const result = summarizeSpend(
      [
        row({ categoryId: SALARY, amount: 2_500_000 }),
        row({ categoryId: FUN, amount: -999_999 }),
        row({ categoryId: FUN, amount: 999_999 }),
        row({ categoryId: FOOD, amount: -1_000_000 }),
      ],
      info
    );
    expect(result.categories.map((c) => c.categoryId)).toEqual([FOOD]);
    expect(result.total).toBe(1_000_000);
  });

  it("counts only expenses toward Sin categoría and lists it last", () => {
    const result = summarizeSpend(
      [
        row({ categoryId: null, amount: -2_500_000 }),
        row({ categoryId: null, amount: 1_000_000 }),
        row({ categoryId: FUN, amount: -1_000_000 }),
      ],
      info
    );
    expect(result.categories.map((c) => [c.categoryId, c.amount])).toEqual([
      [FUN, 1_000_000],
      [null, 2_500_000],
    ]);
    expect(result.categories[1]).toMatchObject({
      name: UNCATEGORIZED_NAME,
      color: UNCATEGORIZED_COLOR,
    });
  });

  it("skips internal transfers", () => {
    const result = summarizeSpend(
      [
        row({ amount: -2_500_000, isInternalTransfer: true }),
        row({ categoryId: null, amount: -1_000_000, isInternalTransfer: true }),
      ],
      info
    );
    expect(result).toEqual({ total: 0, categories: [] });
  });

  it("falls back to the default color", () => {
    const [only] = summarizeSpend([row({ categoryId: NO_COLOR })], info).categories;
    expect(only.color).toBe(DEFAULT_CATEGORY_COLOR);
  });
});

describe("monthsBetween", () => {
  it("lists every month across a year boundary", () => {
    expect(monthsBetween("2025-11-15", "2026-02-01")).toEqual([
      "2025-11",
      "2025-12",
      "2026-01",
      "2026-02",
    ]);
  });

  it("is one month for a window inside it", () => {
    expect(monthsBetween("2026-03-01", "2026-03-31")).toEqual(["2026-03"]);
  });
});

describe("groupSpend", () => {
  it("fills every month of the window, oldest first", () => {
    const groups = groupSpend(
      [
        row({ month: "2026-03", amount: -1_000_000 }),
        row({ month: "2026-01", amount: -2_500_000 }),
      ],
      info,
      "month",
      { from: "2026-01-01", to: "2026-03-31" }
    );
    expect(groups.map((g) => [g.key, g.total])).toEqual([
      ["2026-01", 2_500_000],
      ["2026-02", 0],
      ["2026-03", 1_000_000],
    ]);
    expect(groups[1].categories).toEqual([]);
  });

  it("summarizes each product on its own, largest first", () => {
    const groups = groupSpend(
      [
        row({ productId: PRODUCT_A, categoryId: FOOD, amount: -1_000_000 }),
        row({ productId: PRODUCT_B, categoryId: FOOD, amount: -999_999 }),
        row({ productId: PRODUCT_B, categoryId: FUN, amount: -2_500_000 }),
      ],
      info,
      "product"
    );
    expect(groups.map((g) => g.key)).toEqual([PRODUCT_B, PRODUCT_A]);
    expect(groups[0].categories.map((c) => c.categoryId)).toEqual([FUN, FOOD]);
    expect(groups[1].categories[0].percent).toBe(100);
  });

  it("leaves out institutions with no spend", () => {
    const groups = groupSpend(
      [
        row({ institutionSlug: "banco-uno", amount: -1_000_000 }),
        row({ institutionSlug: "banco-dos", categoryId: SALARY, amount: 2_500_000 }),
      ],
      info,
      "institution"
    );
    expect(groups.map((g) => g.key)).toEqual(["banco-uno"]);
  });
});

describe("monthWindow", () => {
  it("ends on the month's last day, leap years included", () => {
    expect(monthWindow("2028-02")).toEqual({ from: "2028-02-01", to: "2028-02-29" });
    expect(monthWindow("2026-12")).toEqual({ from: "2026-12-01", to: "2026-12-31" });
  });
});

describe("categoryExpensesHref", () => {
  it("links a category, or none for Sin categoría", () => {
    expect(
      categoryExpensesHref({ categoryId: FOOD, from: "2026-01-01", to: "2026-01-31" })
    ).toBe(`/expenses?category=${FOOD}&from=2026-01-01&to=2026-01-31`);
    expect(
      categoryExpensesHref({ categoryId: null, from: "2026-01-01", to: "2026-01-31" })
    ).toBe("/expenses?category=none&from=2026-01-01&to=2026-01-31");
  });
});

describe("categoryDetailHref", () => {
  it("opens a category, or none for uncategorized", () => {
    expect(categoryDetailHref(FOOD)).toBe(`/categories?category=${FOOD}`);
    expect(categoryDetailHref(null)).toBe("/categories?category=none");
  });
});
