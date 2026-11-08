import { describe, expect, it } from "vitest";
import {
  categoryDetailHref,
  categoryExpensesHref,
  categoryInfoFor,
  groupSpend,
  lightenColor,
  rollUpToParents,
  SPLIT_DIRECT_NAME,
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
    expect(result).toEqual({
      total: 0,
      expenses: 0,
      income: 0,
      net: 0,
      categories: [],
    });
  });

  it("reports gross flows of a category that nets to spending", () => {
    const result = summarizeSpend(
      [
        row({ categoryId: FOOD, amount: -2_500_000 }),
        row({ categoryId: FOOD, amount: 1_000_000 }),
      ],
      info
    );
    expect(result.categories[0]).toMatchObject({
      amount: 1_500_000,
      expenses: 2_500_000,
      income: 1_000_000,
      net: 1_500_000,
    });
    expect(result).toMatchObject({
      total: 1_500_000,
      expenses: 2_500_000,
      income: 1_000_000,
      net: 1_500_000,
    });
  });

  it("keeps the flows of categories that net to income or zero in the summary", () => {
    const result = summarizeSpend(
      [
        // Nets to income.
        row({ categoryId: SALARY, amount: -1_000_000 }),
        row({ categoryId: SALARY, amount: 2_500_000 }),
        // Nets to exactly zero.
        row({ categoryId: FUN, amount: -999_999 }),
        row({ categoryId: FUN, amount: 999_999 }),
      ],
      info
    );
    expect(result.categories).toEqual([]);
    expect(result.total).toBe(0);
    expect(result).toMatchObject({
      expenses: 1_999_999,
      income: 3_499_999,
      net: -1_500_000,
    });
  });

  it("reports uncategorized income in flows but not in the spend", () => {
    const result = summarizeSpend(
      [
        row({ categoryId: null, amount: -1_000_000 }),
        row({ categoryId: null, amount: 2_500_000 }),
      ],
      info
    );
    expect(result.categories).toEqual([
      expect.objectContaining({
        categoryId: null,
        amount: 1_000_000,
        expenses: 1_000_000,
        income: 2_500_000,
        net: -1_500_000,
        percent: 100,
      }),
    ]);
    expect(result).toMatchObject({
      total: 1_000_000,
      expenses: 1_000_000,
      income: 2_500_000,
      net: -1_500_000,
    });
  });

  it("leaves internal transfers out of the flows", () => {
    const result = summarizeSpend(
      [
        row({ categoryId: FOOD, amount: -1_000_000 }),
        row({ categoryId: FOOD, amount: 2_500_000, isInternalTransfer: true }),
        row({ categoryId: null, amount: 999_999, isInternalTransfer: true }),
      ],
      info
    );
    expect(result).toMatchObject({
      total: 1_000_000,
      expenses: 1_000_000,
      income: 0,
      net: 1_000_000,
    });
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

  it("carries flows per month, including a month that nets to income", () => {
    const groups = groupSpend(
      [
        row({ month: "2026-01", categoryId: FOOD, amount: -2_500_000 }),
        row({ month: "2026-01", categoryId: FOOD, amount: 1_000_000 }),
        row({ month: "2026-02", categoryId: FOOD, amount: -1_000_000 }),
        row({ month: "2026-02", categoryId: FOOD, amount: 2_500_000 }),
      ],
      info,
      "month",
      { from: "2026-01-01", to: "2026-03-31" }
    );
    expect(
      groups.map((g) => [g.key, g.total, g.expenses, g.income, g.net, g.categories.length])
    ).toEqual([
      ["2026-01", 1_500_000, 2_500_000, 1_000_000, 1_500_000, 1],
      ["2026-02", 0, 1_000_000, 2_500_000, -1_500_000, 0],
      ["2026-03", 0, 0, 0, 0, 0],
    ]);
  });

  it("keeps groups with income alone when asked", () => {
    const rows = [
      row({ institutionSlug: "banco-uno", amount: -1_000_000 }),
      row({ institutionSlug: "banco-dos", amount: 2_500_000 }),
      row({ institutionSlug: "banco-tres", amount: 999_999 }),
    ];
    const groups = groupSpend(rows, info, "institution", undefined, {
      keepIncomeGroups: true,
    });
    expect(groups.map((g) => [g.key, g.total, g.net])).toEqual([
      ["banco-uno", 1_000_000, 1_000_000],
      ["banco-dos", 0, -2_500_000],
      ["banco-tres", 0, -999_999],
    ]);
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

  it("carries a search when set", () => {
    expect(
      categoryExpensesHref({
        categoryId: FOOD,
        from: "2026-01-01",
        to: "2026-01-31",
        q: " café de prueba ",
      })
    ).toBe(
      `/expenses?category=${FOOD}&from=2026-01-01&to=2026-01-31&q=caf%C3%A9+de+prueba`
    );
    expect(
      categoryExpensesHref({ categoryId: FOOD, from: "2026-01-01", to: "2026-01-31", q: "  " })
    ).toBe(`/expenses?category=${FOOD}&from=2026-01-01&to=2026-01-31`);
  });
});

describe("subcategories", () => {
  const TRAVEL = "55555555-5555-4555-8555-555555555555";
  const TRIP_A = "66666666-6666-4666-8666-666666666666";
  const TRIP_B = "77777777-7777-4777-8777-777777777777";
  const tree: SpendCategoryInfo[] = [
    { id: TRAVEL, name: "Viajes", color: "#336699", parentId: null },
    { id: TRIP_A, name: "Viaje A", color: null, parentId: TRAVEL },
    { id: TRIP_B, name: "Viaje B", color: "#00ff00", parentId: TRAVEL },
    { id: FOOD, name: "Comida", color: "#ff0000", parentId: null },
  ];
  const rows = [
    row({ categoryId: TRAVEL, amount: -1_000_000 }),
    row({ categoryId: TRIP_A, amount: -2_500_000 }),
    row({ categoryId: TRIP_A, amount: 999_999 }),
    row({ categoryId: TRIP_B, amount: -1_000_000 }),
    row({ categoryId: FOOD, amount: -999_999 }),
    row({ categoryId: null, amount: -1_000_000 }),
  ];

  it("rolls subcategories into one slice per top-level category", () => {
    const lookup = categoryInfoFor(tree);
    const result = summarizeSpend(rollUpToParents(rows, lookup), lookup);
    expect(result.categories.map((c) => [c.categoryId, c.name, c.amount])).toEqual([
      [TRAVEL, "Viajes", 3_500_001],
      [FOOD, "Comida", 999_999],
      [null, UNCATEGORIZED_NAME, 1_000_000],
    ]);
    const travel = result.categories[0];
    expect(travel).toMatchObject({ expenses: 4_500_000, income: 999_999 });
    expect(result.total).toBe(5_500_000);
  });

  it("leaves rows of unknown or top-level categories alone", () => {
    const lookup = categoryInfoFor(tree);
    const unknown = row({ categoryId: NO_COLOR });
    const [rolled] = rollUpToParents([unknown], lookup);
    expect(rolled).toBe(unknown);
  });

  it("splits a parent into its subcategories and its own rows", () => {
    const lookup = categoryInfoFor(tree, { splitParentId: TRAVEL });
    const scoped = rows.filter((r) =>
      [TRAVEL, TRIP_A, TRIP_B].includes(r.categoryId ?? "")
    );
    const result = summarizeSpend(scoped, lookup);
    expect(result.categories.map((c) => [c.categoryId, c.name, c.amount])).toEqual([
      [TRIP_A, "Viaje A", 1_500_001],
      [TRAVEL, SPLIT_DIRECT_NAME, 1_000_000],
      [TRIP_B, "Viaje B", 1_000_000],
    ]);
    // The colorless subcategory gets a shade of the parent, not its exact color.
    const tripA = result.categories[0];
    expect(tripA.color).not.toBe("#336699");
    expect(tripA.color).toMatch(/^#[0-9a-f]{6}$/);
    expect(result.categories[2].color).toBe("#00ff00");
  });

  it("gives a colorless subcategory its parent's color outside a split", () => {
    const lookup = categoryInfoFor(tree);
    expect(lookup.get(TRIP_A)?.color).toBe("#336699");
    expect(lookup.get(TRAVEL)?.name).toBe("Viajes");
  });

  it("lightens a color toward white", () => {
    expect(lightenColor("#000000", 0.5)).toBe("#808080");
    expect(lightenColor("#336699", 0)).toBe("#336699");
    expect(lightenColor("#336699", 1)).toBe("#ffffff");
  });
});

describe("categoryDetailHref", () => {
  it("opens a category, or none for uncategorized", () => {
    expect(categoryDetailHref(FOOD)).toBe(`/categories?category=${FOOD}`);
    expect(categoryDetailHref(null)).toBe("/categories?category=none");
  });
});
