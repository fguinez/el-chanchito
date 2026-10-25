// The Gastos page's URL filters (?category=&from=&to=&product=&institution=),
// read leniently: a malformed value is ignored rather than sent to the API,
// which would 400 on it. Charts link here with categoryExpensesHref.

import { UNCATEGORIZED_PARAM } from "@/lib/category-spend";

export interface ExpensesFilters {
  /** A category uuid, or UNCATEGORIZED_PARAM for "Sin categoría". */
  category?: string;
  /** Inclusive transaction_date bounds, YYYY-MM-DD. */
  from?: string;
  to?: string;
  /** A product id. */
  product?: string;
  /** An institution slug. */
  institution?: string;
}

export const EXPENSES_FILTER_KEYS = [
  "category",
  "from",
  "to",
  "product",
  "institution",
] as const satisfies readonly (keyof ExpensesFilters)[];

const DAY = /^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const SLUG = /^[a-z0-9][a-z0-9_-]*$/i;

export function parseExpensesFilters(params: URLSearchParams): ExpensesFilters {
  const filters: ExpensesFilters = {};
  const category = params.get("category");
  if (category === UNCATEGORIZED_PARAM || (category && UUID.test(category))) {
    filters.category = category.toLowerCase();
  }
  const from = params.get("from");
  if (from && DAY.test(from)) filters.from = from;
  const to = params.get("to");
  // A window that runs backwards keeps only its start.
  if (to && DAY.test(to) && !(filters.from && to < filters.from)) filters.to = to;
  const product = params.get("product");
  if (product && UUID.test(product)) filters.product = product.toLowerCase();
  const institution = params.get("institution");
  if (institution && SLUG.test(institution)) filters.institution = institution;
  return filters;
}
