"use client";

import { useMemo, useState } from "react";

export type SortDirection = "asc" | "desc";

export interface SortState<K extends string> {
  key: K;
  direction: SortDirection;
}

export interface UseSortableData<T, K extends string> {
  sorted: T[];
  sort: SortState<K> | null;
  toggleSort: (key: K) => void;
}

/**
 * Client-side, three-state column sorting for a table.
 *
 * The cycle per column is unsorted -> "asc" -> "desc" -> unsorted; clearing
 * restores the original `items` order. Clicking a different column starts it
 * at "asc".
 *
 * `getValue` maps (item, key) to the sortable value (number, string, or null);
 * it is a memo dependency, so callers must pass a stable reference (wrap it in
 * `useCallback`) to avoid re-sorting on every render.
 */
export function useSortableData<T, K extends string>(
  items: T[],
  getValue: (item: T, key: K) => string | number | null,
  initial: SortState<K> | null = null
): UseSortableData<T, K> {
  const [sort, setSort] = useState<SortState<K> | null>(initial);

  const toggleSort = (key: K) => {
    setSort((prev) => {
      if (!prev || prev.key !== key) return { key, direction: "asc" };
      if (prev.direction === "asc") return { key, direction: "desc" };
      return null; // was "desc": clear back to server order.
    });
  };

  const sorted = useMemo(() => {
    if (!sort) return items; // No sort: preserve server order.
    const { key, direction } = sort;
    const factor = direction === "asc" ? 1 : -1;
    return [...items].sort((a, b) => {
      const av = getValue(a, key);
      const bv = getValue(b, key);
      // Nulls always sort last regardless of direction.
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      let cmp: number;
      if (typeof av === "number" && typeof bv === "number") {
        cmp = av - bv;
      } else {
        cmp = String(av).localeCompare(String(bv), "es");
      }
      return cmp * factor;
    });
  }, [items, sort, getValue]);

  return { sorted, sort, toggleSort };
}
