// The single place that decides whether a product row is a "retired ghost"
// and must be treated as nonexistent by the web UI.
//
// Background: the scrapers' writer (apps/scrapers/db/writer.py,
// `_retire_rollup_sibling`) retires legacy roll-up products (one summed
// aggregate that got replaced by per-holding products) instead of deleting
// them. Retiring deletes the row's snapshots and blanks the row:
// `is_active = false, current_balance = NULL, metrics = NULL,
// balance_as_of = NULL`. The row is kept on purpose so a fallback scrape can
// never resurrect a double-counting aggregate; it is not something the user
// should ever see, since it carries no data at all.
//
// Only that exact shape is hidden. An inactive product that still has a
// balance stays visible (the UI marks it with an "Inactivo" badge), and so
// does an active product that has not been scraped yet.

/** The columns the ghost rule looks at. `currentBalance` may arrive as a
 *  string: postgres-js returns numeric columns as strings. */
export interface RetiredGhostCandidate {
  isActive: boolean;
  currentBalance: string | number | null;
}

/**
 * True when the product is a retired roll-up ghost: inactive AND stripped of
 * its balance. Such products are hidden from listings and 404 on their detail
 * route.
 */
export function isRetiredGhost(product: RetiredGhostCandidate): boolean {
  return !product.isActive && product.currentBalance == null;
}
