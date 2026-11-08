// Subcategories (V046) on the client: ordering the one-level tree, the
// "Viajes / Japón" path pickers and labels show, the color a subcategory
// inherits, and the picker search. Pure, so it unit-tests apart from React.

import { normalizeSearchText } from "@/lib/transaction-search";

export interface TreeCategory {
  id: string;
  name: string;
  parentId: string | null;
  color: string | null;
}

/** Separator between a parent's name and its subcategory's in a path. */
export const CATEGORY_PATH_SEPARATOR = " / ";

export interface TreeEntry<T extends TreeCategory> {
  category: T;
  /** 0 for a top-level category, 1 for a subcategory. */
  depth: 0 | 1;
  parent: T | null;
}

/**
 * `categories` as a tree in display order: each top-level category followed
 * by its subcategories. Order follows `categories` (sort it first), so a
 * sorted table keeps its order within each level. A subcategory whose parent
 * is missing from the list shows as top-level.
 */
export function categoryTree<T extends TreeCategory>(
  categories: readonly T[]
): TreeEntry<T>[] {
  const ids = new Set(categories.map((c) => c.id));
  const children = new Map<string, T[]>();
  for (const c of categories) {
    if (c.parentId && ids.has(c.parentId)) {
      const list = children.get(c.parentId) ?? [];
      list.push(c);
      children.set(c.parentId, list);
    }
  }
  const entries: TreeEntry<T>[] = [];
  for (const c of categories) {
    if (c.parentId && ids.has(c.parentId)) continue;
    entries.push({ category: c, depth: 0, parent: null });
    for (const child of children.get(c.id) ?? []) {
      entries.push({ category: child, depth: 1, parent: c });
    }
  }
  return entries;
}

/** Every parent's subcategories, keyed by the parent's id, in list order. */
export function childrenByParent<T extends TreeCategory>(
  categories: readonly T[]
): Map<string, T[]> {
  const children = new Map<string, T[]>();
  for (const c of categories) {
    if (!c.parentId) continue;
    const list = children.get(c.parentId) ?? [];
    list.push(c);
    children.set(c.parentId, list);
  }
  return children;
}

/** "Viajes / Japón" for a subcategory, the name alone otherwise. */
export function categoryPath(
  category: Pick<TreeCategory, "name" | "parentId">,
  byId: ReadonlyMap<string, Pick<TreeCategory, "name">>
): string {
  const parent = category.parentId ? byId.get(category.parentId) : undefined;
  return parent
    ? `${parent.name}${CATEGORY_PATH_SEPARATOR}${category.name}`
    : category.name;
}

/** The color a category shows: its own, else its parent's, else null. */
export function effectiveColor(
  category: Pick<TreeCategory, "color" | "parentId">,
  byId: ReadonlyMap<string, Pick<TreeCategory, "color">>
): string | null {
  if (category.color) return category.color;
  return category.parentId ? (byId.get(category.parentId)?.color ?? null) : null;
}

/**
 * The picker's search over a tree entry: every word of `query` must appear,
 * accent- and case-insensitively, in the category's name or its parent's, so
 * "japon" finds "Viajes / Japón" and "viajes" lists every trip. A parent also
 * matches when one of its subcategories does, so the match keeps its group.
 */
export function matchesCategorySearch<T extends TreeCategory>(
  entry: TreeEntry<T>,
  query: string,
  children: readonly T[] = []
): boolean {
  const words = normalizeSearchText(query).split(/\s+/).filter(Boolean);
  if (words.length === 0) return true;
  const own = normalizeSearchText(
    [entry.parent?.name, entry.category.name].filter(Boolean).join(" ")
  );
  if (words.every((w) => own.includes(w))) return true;
  return children.some((child) =>
    matchesCategorySearch({ category: child, depth: 1, parent: entry.category }, query)
  );
}
