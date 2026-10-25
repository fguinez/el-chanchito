"use client";

import { useEffect, useState } from "react";
import type { CategorySpendResponse } from "@/lib/category-spend";

/**
 * GET /api/categories/spend for `query` (a URL query string). Each result
 * remembers the query it answers, so a new query reads as loading (or as its
 * own error) instead of showing the previous one's figures; bumping
 * `refreshKey` refetches the same query and keeps its figures meanwhile.
 */
export function useCategorySpend(
  query: string,
  refreshKey = 0
): { data: CategorySpendResponse | null; error: boolean } {
  const [result, setResult] = useState<{
    query: string;
    data: CategorySpendResponse | null;
    error: boolean;
  } | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    fetch(`/api/categories/spend?${query}`, { signal: controller.signal })
      .then((res) => {
        if (!res.ok) throw new Error(`GET /api/categories/spend ${res.status}`);
        return res.json();
      })
      .then((body: CategorySpendResponse) => {
        if (!controller.signal.aborted) setResult({ query, data: body, error: false });
      })
      .catch(() => {
        if (!controller.signal.aborted) setResult({ query, data: null, error: true });
      });
    return () => controller.abort();
  }, [query, refreshKey]);

  const current = result?.query === query ? result : null;
  return { data: current?.data ?? null, error: current?.error ?? false };
}
