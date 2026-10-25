import { describe, expect, it } from "vitest";
import { parseExpensesFilters } from "@/lib/expenses-filters";
import { categoryExpensesHref } from "@/lib/category-spend";

// Synthetic ids (see the repo's personal data policy).
const CATEGORY = "11111111-1111-4111-8111-111111111111";
const PRODUCT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";

const parse = (query: string) => parseExpensesFilters(new URLSearchParams(query));

describe("parseExpensesFilters", () => {
  it("reads every filter", () => {
    expect(
      parse(
        `category=${CATEGORY.toUpperCase()}&from=2026-01-01&to=2026-01-31&product=${PRODUCT}&institution=banco-uno`
      )
    ).toEqual({
      category: CATEGORY,
      from: "2026-01-01",
      to: "2026-01-31",
      product: PRODUCT,
      institution: "banco-uno",
    });
  });

  it("keeps none for uncategorized rows", () => {
    expect(parse("category=none")).toEqual({ category: "none" });
  });

  it("ignores malformed values", () => {
    expect(
      parse("category=food&from=2026-13-01&to=yesterday&product=1&institution=a%20b")
    ).toEqual({});
  });

  it("accepts underscore slugs", () => {
    expect(parse("institution=banco_uno")).toEqual({ institution: "banco_uno" });
  });

  it("lowercases the institution slug", () => {
    expect(parse("institution=Banco-Uno")).toEqual({ institution: "banco-uno" });
  });

  it("drops an end before the start", () => {
    expect(parse("from=2026-02-01&to=2026-01-01")).toEqual({ from: "2026-02-01" });
  });

  it("round-trips a chart link", () => {
    const href = categoryExpensesHref({
      categoryId: null,
      from: "2026-01-01",
      to: "2026-01-31",
      productId: PRODUCT,
    });
    expect(parse(href.split("?")[1])).toEqual({
      category: "none",
      from: "2026-01-01",
      to: "2026-01-31",
      product: PRODUCT,
    });
  });
});
