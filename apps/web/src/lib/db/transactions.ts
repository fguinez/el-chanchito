import { isNull, sql } from "drizzle-orm";
import { products, transactions } from "./schema";

/** A visible (not soft-deleted, V033) transaction. Centralizes the
 *  deleted_at IS NULL filter that every dashboard read applies. */
export const notDeleted = isNull(transactions.deletedAt);

/** The description to SHOW for a transaction: the one-time custom override
 *  (V034), else a matching reusable alias resolved live (V035,
 *  `alias_for_transaction`), else the scraped/manual description. Use this only
 *  for DISPLAY, never for matching (category rules and alias matching key on the
 *  raw scraped `description`). The alias layer reads `products.currency`, so
 *  EVERY query selecting this must join `products`. */
export const displayDescription = sql<string>`COALESCE(${transactions.customDescription}, alias_for_transaction(${transactions.description}, ${transactions.amount}, ${products.currency}), ${transactions.description})`;
