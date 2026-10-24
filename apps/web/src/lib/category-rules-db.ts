// Turning one transaction's category into a rule: the detail panel's opt-in
// "apply to every transaction with this description". Matching keys on the raw
// scraped `transactions.description`, never on the displayed override or alias.

import { and, asc, eq, isNull, ne, not, or, sql } from "drizzle-orm";
import { db } from "@/lib/db";
import { categories, categoryRules, transactions } from "@/lib/db/schema";
import { notDeleted } from "@/lib/db/transactions";
import { RULE_PRIORITY_MAX, normalizeKeyword } from "@/lib/categories";

type Tx = Parameters<Parameters<typeof db.transaction>[0]>[0];

/** Rules made from a transaction's description take the top priority, so the
 *  exact description beats any broader keyword rule it also contains. */
export const DESCRIPTION_RULE_PRIORITY = RULE_PRIORITY_MAX;

export type CategoryRuleRow = typeof categoryRules.$inferSelect;

export type ApplyCategoryRuleOutcome =
  | {
      ok: true;
      rule: CategoryRuleRow;
      /** False when a rule with the same keyword already existed. */
      ruleCreated: boolean;
      /** Rows besides this one that took the category. */
      updatedCount: number;
    }
  | { ok: false; status: 404 }
  | { ok: false; status: 400; error: string; field: string };

/**
 * Give `transactionId` the category (marked manual, as PATCH does), keep a
 * rule whose keyword is its trimmed raw description, and give the category to
 * every other visible, not manually categorized row whose raw description is
 * the same (trimmed, case-insensitive). An existing rule with that keyword
 * (case-insensitive; the oldest if several) is reused and pointed at the
 * category; otherwise one is created. Run it inside `db.transaction`.
 */
export async function applyCategoryRuleFromTransaction(
  tx: Tx,
  transactionId: string,
  categoryId: string
): Promise<ApplyCategoryRuleOutcome> {
  const [txn] = await tx
    .select({ description: transactions.description })
    .from(transactions)
    .where(and(eq(transactions.id, transactionId), notDeleted))
    .limit(1);
  if (!txn) return { ok: false, status: 404 };

  const keyword = normalizeKeyword(txn.description);
  if (!keyword.ok) {
    return {
      ok: false,
      status: 400,
      error: "La descripción es demasiado larga para una regla",
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
      error: "Category not found",
      field: "categoryId",
    };
  }

  const [existing] = await tx
    .select({ id: categoryRules.id })
    .from(categoryRules)
    .where(sql`lower(btrim(${categoryRules.keyword})) = lower(${keyword.value})`)
    .orderBy(asc(categoryRules.createdAt), asc(categoryRules.id))
    .limit(1);
  const ruleValues = { categoryId, priority: DESCRIPTION_RULE_PRIORITY };
  const [rule] = existing
    ? await tx
        .update(categoryRules)
        .set(ruleValues)
        .where(eq(categoryRules.id, existing.id))
        .returning()
    : await tx
        .insert(categoryRules)
        .values({ keyword: keyword.value, ...ruleValues })
        .returning();

  const now = new Date();
  await tx
    .update(transactions)
    .set({ categoryId, isManuallyCategorized: true, updatedAt: now })
    .where(eq(transactions.id, transactionId));

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
  };
}
