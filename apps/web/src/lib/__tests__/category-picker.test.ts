import { describe, expect, it } from "vitest";
import {
  CLOSED_PICKER_STATE,
  nextActiveIndex,
  openPickerState,
  pickerOptions,
} from "@/lib/category-picker";
import type { TreeCategory } from "@/lib/category-tree";

// Every id and name is synthetic (see the repo's personal data policy).

const categories: TreeCategory[] = [
  { id: "t", name: "Viajes", parentId: null, color: "#336699" },
  { id: "a", name: "Japón", parentId: "t", color: null },
  { id: "b", name: "Playa", parentId: "t", color: null },
  { id: "f", name: "Comida", parentId: null, color: "#ff0000" },
];

describe("pickerOptions", () => {
  it("lists the empty choice, then each parent with its subcategories", () => {
    expect(
      pickerOptions(categories, "", "Sin categoría").map((o) => [o.id, o.depth])
    ).toEqual([
      ["", 0],
      ["f", 0],
      ["t", 0],
      ["a", 1],
      ["b", 1],
    ]);
  });

  it("narrows to matches, keeping a matching subcategory's parent", () => {
    expect(
      pickerOptions(categories, "japon", "Sin categoría").map((o) => o.id)
    ).toEqual(["t", "a"]);
  });

  it("gives a colorless subcategory its parent's color", () => {
    const japan = pickerOptions(categories, "").find((o) => o.id === "a");
    expect(japan?.color).toBe("#336699");
  });
});

describe("openPickerState", () => {
  it("highlights the current choice in the full list, whatever was searched", () => {
    // A previous search narrowed the list to ["t", "a"], where "f" is
    // missing; reopening must not reuse that list's indexes.
    const narrowed = pickerOptions(categories, "japon", "Sin categoría");
    expect(narrowed.findIndex((o) => o.id === "f")).toBe(-1);

    const state = openPickerState(categories, "f", "Sin categoría");
    expect(state.query).toBe("");
    const full = pickerOptions(categories, state.query, "Sin categoría");
    // Enter right after opening keeps the current category.
    expect(full[state.active].id).toBe("f");
  });

  it("starts on the first line when the value is not listed", () => {
    expect(openPickerState(categories, "gone")).toEqual({ query: "", active: 0 });
  });

  it("closes with no query", () => {
    expect(CLOSED_PICKER_STATE).toEqual({ query: "", active: 0 });
  });
});

describe("nextActiveIndex", () => {
  it("moves with the arrows within bounds, and jumps with Home and End", () => {
    expect(nextActiveIndex("ArrowDown", 0, 3)).toBe(1);
    expect(nextActiveIndex("ArrowDown", 2, 3)).toBe(2);
    expect(nextActiveIndex("ArrowUp", 0, 3)).toBe(0);
    expect(nextActiveIndex("Home", 2, 3)).toBe(0);
    expect(nextActiveIndex("End", 0, 3)).toBe(2);
  });

  it("ignores other keys and an empty list", () => {
    expect(nextActiveIndex("Enter", 0, 3)).toBeNull();
    expect(nextActiveIndex("ArrowDown", 0, 0)).toBeNull();
  });
});
