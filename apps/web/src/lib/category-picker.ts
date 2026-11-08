// The list logic of CategoryPicker (components/categories): which lines a
// query shows, where the highlight starts when the picker opens, and how the
// keys move it. Pure, so it unit-tests without a DOM.

import {
  categoryTree,
  childrenByParent,
  effectiveColor,
  matchesCategorySearch,
  type TreeCategory,
} from "@/lib/category-tree";

/** One choosable line: a category, or the empty choice (id ""). */
export interface PickerOption {
  id: string;
  label: string;
  depth: 0 | 1;
  color: string | null;
}

/** The picker's lines for `query`: the empty choice (when `emptyLabel` is
 *  given and nothing is typed), then the tree by name, subcategories under
 *  their parent, narrowed to the matches (see matchesCategorySearch). */
export function pickerOptions(
  categories: readonly TreeCategory[],
  query: string,
  emptyLabel?: string
): PickerOption[] {
  const byId = new Map(categories.map((c) => [c.id, c]));
  const sorted = [...categories].sort((a, b) => a.name.localeCompare(b.name, "es"));
  const children = childrenByParent(sorted);
  const list: PickerOption[] = [];
  if (emptyLabel && !query.trim()) {
    list.push({ id: "", label: emptyLabel, depth: 0, color: null });
  }
  for (const entry of categoryTree(sorted)) {
    const kids = entry.depth === 0 ? (children.get(entry.category.id) ?? []) : [];
    if (!matchesCategorySearch(entry, query, kids)) continue;
    list.push({
      id: entry.category.id,
      label: entry.category.name,
      depth: entry.depth,
      color: effectiveColor(entry.category, byId),
    });
  }
  return list;
}

export interface PickerState {
  query: string;
  /** Index of the highlighted line in pickerOptions(…, query, …). */
  active: number;
}

/** The state a closed picker rests in: no query, so the next open starts
 *  from the full list. */
export const CLOSED_PICKER_STATE: PickerState = { query: "", active: 0 };

/** The state on opening: no query, and the highlight on `value` within the
 *  full list (the first line when it is not there). Computed from the
 *  categories, never from a list a previous search narrowed, so Enter right
 *  after opening keeps the current choice. */
export function openPickerState(
  categories: readonly TreeCategory[],
  value: string,
  emptyLabel?: string
): PickerState {
  const index = pickerOptions(categories, "", emptyLabel).findIndex(
    (o) => o.id === value
  );
  return { query: "", active: Math.max(index, 0) };
}

/** Where a navigation key moves the highlight over `count` lines; null for
 *  any other key. */
export function nextActiveIndex(
  key: string,
  active: number,
  count: number
): number | null {
  if (count === 0) return null;
  const last = count - 1;
  switch (key) {
    case "ArrowDown":
      return Math.min(active + 1, last);
    case "ArrowUp":
      return Math.max(active - 1, 0);
    case "Home":
      return 0;
    case "End":
      return last;
    default:
      return null;
  }
}
