import { isNull, sql } from "drizzle-orm";
import { transactions } from "./schema";

/** A visible (not soft-deleted, V033) transaction. Centralizes the
 *  deleted_at IS NULL filter that every dashboard read applies. */
export const notDeleted = isNull(transactions.deletedAt);

/** The description to SHOW for a transaction: the one-time custom override,
 *  else the scraped/manual description. Phase 2 will insert the alias layer
 *  between them. Use this only for DISPLAY, never for matching (category rules
 *  and alias matching key on the raw scraped `description`). */
export const displayDescription = sql<string>`COALESCE(${transactions.customDescription}, ${transactions.description})`;
