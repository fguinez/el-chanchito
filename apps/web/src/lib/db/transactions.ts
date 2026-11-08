import { and, desc, isNull, or, sql, type SQL } from "drizzle-orm";
import type { AnyPgColumn } from "drizzle-orm/pg-core";
import { searchTokens } from "@/lib/transaction-search";
import { categories, products, transactions } from "./schema";

/** A visible (not soft-deleted, V033) transaction. Centralizes the
 *  deleted_at IS NULL filter that every dashboard read applies. */
export const notDeleted = isNull(transactions.deletedAt);

/** The rows a `categoryId` filter keeps (V046): those assigned to the
 *  category or to one of its subcategories. A subcategory has none of its
 *  own, so filtering on one keeps only its rows. */
export function inCategoryScope(categoryId: string): SQL {
  return sql`${transactions.categoryId} IN (SELECT ${categories.id} FROM ${categories} WHERE ${categories.id} = ${categoryId} OR ${categories.parentId} = ${categoryId})`;
}

/** How many of a product's newest movements its page lists. A credit card's
 *  billing cycle reads the statement dates of these same rows, so whatever
 *  shows one movement's billing status loads them too (lib/card-billing). */
export const PRODUCT_TRANSACTIONS_LIMIT = 500;

/** A product's movements newest first: createdAt breaks same-day ties, and the
 *  id breaks the ties left (rows inserted in one statement share createdAt),
 *  so the order, and so which rows PRODUCT_TRANSACTIONS_LIMIT keeps, is the
 *  same on every load and in every route that reads it. */
export const newestTransactionsFirst = [
  desc(transactions.transactionDate),
  desc(transactions.createdAt),
  desc(transactions.id),
];

/** displayDescription over a given transactions table and the products table
 *  joined to it, either of them possibly an `alias()` (a query that joins
 *  transactions twice, like a transfer's two legs, needs one per leg). */
export function displayDescriptionOf(
  txn: {
    customDescription: AnyPgColumn;
    description: AnyPgColumn;
    amount: AnyPgColumn;
  },
  product: { currency: AnyPgColumn }
): SQL<string> {
  return sql<string>`COALESCE(${txn.customDescription}, alias_for_transaction(${txn.description}, ${txn.amount}, ${product.currency}), ${txn.description})`;
}

/** The description to SHOW for a transaction: the one-time custom override
 *  (V034), else a matching reusable alias resolved live (V035,
 *  `alias_for_transaction`), else the scraped/manual description. Use this only
 *  for DISPLAY, never for matching (category rules and alias matching key on the
 *  raw scraped `description`). The alias layer reads `products.currency`, so
 *  EVERY query selecting this must join `products`. */
export const displayDescription = displayDescriptionOf(transactions, products);

/** `text` folded the way normalizeSearchText (lib/transaction-search) folds a
 *  query: NFD, combining accents (U+0300 to U+036F) stripped, lowercased.
 *  Built-ins only (no `unaccent`, as V014), so it runs on the stock
 *  postgres:16-alpine image; `lower()` alone would keep the accents. */
export function foldForSearch(text: SQL | AnyPgColumn): SQL<string> {
  // The SQL text reads '[\u0300-\u036f]': a Postgres regex escape (the
  // backslash is doubled for the template literal), not a JS one.
  return sql<string>`lower(regexp_replace(normalize(${text}, NFD), '[\\u0300-\\u036f]', '', 'g'))`;
}

/** The rows a search box query keeps (see lib/transaction-search): every word
 *  of `query` must appear in one of `fields`, folded, as a literal substring
 *  (`strpos`, so `%` and `_` are plain characters). Undefined for a blank
 *  query, so `and()` drops it. `fields` default to the raw and the display
 *  description of `transactions` (the query must join `products`); pass
 *  `[txn.description, displayDescriptionOf(txn, product)]` for an alias. Raw
 *  goes first so a word it already holds skips the `alias_for_transaction`
 *  lookup. */
export function descriptionMatches(
  query: string | null | undefined,
  fields: readonly (SQL | AnyPgColumn)[] = [
    transactions.description,
    displayDescription,
  ]
): SQL | undefined {
  const tokens = searchTokens(query);
  if (tokens.length === 0 || fields.length === 0) return undefined;
  const folded = fields.map(foldForSearch);
  return and(
    ...tokens.map((token) =>
      or(...folded.map((text) => sql`strpos(${text}, ${token}) > 0`))
    )
  );
}
