// Turning one transaction's category into a rule: the detail panel's opt-in
// "apply to every transaction with this description". Matching keys on the raw
// scraped `transactions.description`, never on the displayed override or alias.

import { and, asc, eq, isNull, ne, not, or, sql } from "drizzle-orm";
import { db } from "@/lib/db";
import { categories, categoryRules, transactions } from "@/lib/db/schema";
import { notDeleted } from "@/lib/db/transactions";
import { RULE_KEYWORD_MAX_LENGTH } from "@/lib/categories";

type Tx = Parameters<Parameters<typeof db.transaction>[0]>[0];

export type CategoryRuleRow = typeof categoryRules.$inferSelect;

export type ApplyCategoryRuleOutcome =
  | {
      ok: true;
      rule: CategoryRuleRow;
      /** False when an exact rule with the same keyword already existed. */
      ruleCreated: boolean;
      /** Rows besides this one that took the category. */
      updatedCount: number;
      /** Whether this row took the category (false only under
       *  `onlyIfUncategorized`, when it had one or was cleared by hand). */
      transactionUpdated: boolean;
    }
  | { ok: false; status: 404 }
  | { ok: false; status: 400; error: string; field: string };

/**
 * Give `transactionId` the category (marked manual, as PATCH does), keep an
 * exact-match rule (V041) whose keyword is its trimmed raw description, and give
 * the category to every other visible, not manually categorized row whose raw
 * description is the same (trimmed, case-insensitive). An existing exact rule
 * with that keyword (case-insensitive; the oldest if several) is reused and
 * pointed at the category, its priority untouched; otherwise one is created at
 * the default priority. Exact rules already win over contains rules, so no
 * priority bump is needed. Run it inside `db.transaction`.
 *
 * `onlyIfUncategorized` (the Categorías suggestions, whose row id may be
 * stale) updates this row only while it has no category and is not marked
 * manual; the rule and the other rows are handled the same either way.
 */
export async function applyCategoryRuleFromTransaction(
  tx: Tx,
  transactionId: string,
  categoryId: string,
  options: { onlyIfUncategorized?: boolean } = {}
): Promise<ApplyCategoryRuleOutcome> {
  // The keyword is trimmed and lowercased by PostgreSQL, not JS: btrim() strips
  // only spaces, and the trigger compares with it (V041), so a description
  // ending in a tab or NBSP still matches its future rows.
  const [txn] = await tx
    .select({
      description: transactions.description,
      keyword: sql<string>`lower(btrim(${transactions.description}))`,
    })
    .from(transactions)
    .where(and(eq(transactions.id, transactionId), notDeleted))
    .limit(1);
  if (!txn) return { ok: false, status: 404 };

  if (txn.keyword === "" || txn.keyword.length > RULE_KEYWORD_MAX_LENGTH) {
    return {
      ok: false,
      status: 400,
      error:
        txn.keyword === ""
          ? "La descripción original está vacía; no se puede crear una regla"
          : "La descripción es demasiado larga para una regla",
      field: "description",
    };
  }

  const [category] = await tx
    .select({ id: categories.id })
    .from(categories)
    .where(eq(categories.id, categoryId))
    .limit(1);
  if (!category) {
    return {
      ok: false,
      status: 400,
      error: "La categoría no existe",
      field: "categoryId",
    };
  }

  // Serializes concurrent saves of the same keyword, so two of them cannot
  // both miss the lookup and insert twice. Released at commit.
  await tx.execute(
    sql`SELECT pg_advisory_xact_lock(hashtext(lower(${txn.keyword})))`
  );
  const [existing] = await tx
    .select({ id: categoryRules.id })
    .from(categoryRules)
    .where(
      and(
        eq(categoryRules.matchType, "exact"),
        sql`lower(btrim(${categoryRules.keyword})) = lower(${txn.keyword})`
      )
    )
    .orderBy(asc(categoryRules.createdAt), asc(categoryRules.id))
    .limit(1);
  const [rule] = existing
    ? await tx
        .update(categoryRules)
        .set({ categoryId })
        .where(eq(categoryRules.id, existing.id))
        .returning()
    : await tx
        .insert(categoryRules)
        .values({ keyword: txn.keyword, categoryId, matchType: "exact" })
        .returning();

  const now = new Date();
  let transactionUpdated = true;
  if (options.onlyIfUncategorized) {
    const own = await tx
      .update(transactions)
      .set({ categoryId, isManuallyCategorized: true, updatedAt: now })
      .where(
        and(
          eq(transactions.id, transactionId),
          isNull(transactions.categoryId),
          not(transactions.isManuallyCategorized)
        )
      )
      .returning({ id: transactions.id });
    transactionUpdated = own.length > 0;
  } else {
    await tx
      .update(transactions)
      .set({ categoryId, isManuallyCategorized: true, updatedAt: now })
      .where(eq(transactions.id, transactionId));
  }

  const updated = await tx
    .update(transactions)
    .set({ categoryId, updatedAt: now })
    .where(
      and(
        ne(transactions.id, transactionId),
        notDeleted,
        not(transactions.isManuallyCategorized),
        sql`lower(btrim(${transactions.description})) = lower(btrim(${txn.description}))`,
        or(
          isNull(transactions.categoryId),
          ne(transactions.categoryId, categoryId)
        )
      )
    )
    .returning({ id: transactions.id });

  return {
    ok: true,
    rule,
    ruleCreated: !existing,
    updatedCount: updated.length,
    transactionUpdated,
  };
}
