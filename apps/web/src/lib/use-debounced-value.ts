"use client";

import { useEffect, useState } from "react";

/** How long a search box waits after the last key before it queries. */
export const SEARCH_DEBOUNCE_MS = 250;

/**
 * `value`, updated only once it has held still for `ms` milliseconds, so a
 * server-backed search fetches once per pause in typing instead of per key.
 */
export function useDebouncedValue<T>(value: T, ms = SEARCH_DEBOUNCE_MS): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(timer);
  }, [value, ms]);
  return debounced;
}
