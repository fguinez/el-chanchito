import { describe, expect, it } from "vitest";
import {
  categoryPath,
  categoryTree,
  childrenByParent,
  effectiveColor,
  matchesCategorySearch,
  type TreeCategory,
} from "@/lib/category-tree";

// Every id and name is synthetic (see the repo's personal data policy).

const TRAVEL: TreeCategory = { id: "t", name: "Viajes", parentId: null, color: "#336699" };
const TRIP_A: TreeCategory = { id: "a", name: "Japón", parentId: "t", color: null };
const TRIP_B: TreeCategory = { id: "b", name: "Playa", parentId: "t", color: "#00ff00" };
const FOOD: TreeCategory = { id: "f", name: "Comida", parentId: null, color: null };
const ORPHAN: TreeCategory = { id: "o", name: "Suelta", parentId: "gone", color: null };
const byId = new Map([TRAVEL, TRIP_A, TRIP_B, FOOD].map((c) => [c.id, c]));

describe("categoryTree", () => {
  it("lists each parent followed by its subcategories, keeping the order", () => {
    const tree = categoryTree([TRIP_B, FOOD, TRIP_A, TRAVEL, ORPHAN]);
    expect(tree.map((e) => [e.category.id, e.depth, e.parent?.id ?? null])).toEqual([
      ["f", 0, null],
      ["t", 0, null],
      ["b", 1, "t"],
      ["a", 1, "t"],
      ["o", 0, null],
    ]);
  });

  it("groups children by parent", () => {
    const children = childrenByParent([TRAVEL, TRIP_A, TRIP_B, FOOD]);
    expect(children.get("t")?.map((c) => c.id)).toEqual(["a", "b"]);
    expect(children.has("f")).toBe(false);
  });
});

describe("categoryPath / effectiveColor", () => {
  it("prefixes a subcategory with its parent", () => {
    expect(categoryPath(TRIP_A, byId)).toBe("Viajes / Japón");
    expect(categoryPath(FOOD, byId)).toBe("Comida");
    expect(categoryPath(ORPHAN, byId)).toBe("Suelta");
  });

  it("inherits the parent's color unless the subcategory has one", () => {
    expect(effectiveColor(TRIP_A, byId)).toBe("#336699");
    expect(effectiveColor(TRIP_B, byId)).toBe("#00ff00");
    expect(effectiveColor(FOOD, byId)).toBeNull();
  });
});

describe("matchesCategorySearch", () => {
  const [travel, , tripA] = categoryTree([TRAVEL, TRIP_B, TRIP_A]);

  it("matches either segment, ignoring accents and case", () => {
    expect(matchesCategorySearch(tripA, "japon")).toBe(true);
    expect(matchesCategorySearch(tripA, "VIAJES")).toBe(true);
    expect(matchesCategorySearch(tripA, "viajes jap")).toBe(true);
    expect(matchesCategorySearch(tripA, "playa")).toBe(false);
  });

  it("keeps a parent whose subcategory matches", () => {
    expect(matchesCategorySearch(travel, "japon")).toBe(false);
    expect(matchesCategorySearch(travel, "japon", [TRIP_A, TRIP_B])).toBe(true);
  });

  it("matches everything on a blank query", () => {
    expect(matchesCategorySearch(tripA, "   ")).toBe(true);
  });
});
