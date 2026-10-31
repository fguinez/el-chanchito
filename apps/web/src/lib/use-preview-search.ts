"use client";

import { useState } from "react";
import { useDebouncedValue } from "@/lib/use-debounced-value";

/**
 * The search box of a server-backed match preview: the typed `search`, the
 * debounced `q` to fetch with, and, against the result on screen (`shown`:
 * the search it answers and its match count; null until one arrives),
 * whether that result is `stale` (it answers an older search while a new one
 * is typed or loads: keep it, dimmed) and whether to `showSearch` (once there
 * is something to narrow, and kept while a search is on).
 */
export function usePreviewSearch(shown: { q: string; total: number } | null) {
  const [search, setSearch] = useState("");
  const q = useDebouncedValue(search.trim());
  const stale = shown !== null && (shown.q !== q || search.trim() !== q);
  const showSearch =
    search !== "" || (shown !== null && (shown.total > 0 || shown.q !== ""));
  return { search, setSearch, q, stale, showSearch };
}
