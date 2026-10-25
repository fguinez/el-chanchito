// Database side of the category suggestions: load the categorized history and
// the uncategorized rows, apply a suggestion once to every row with one
// description, and remember a dismissed suggestion (V042). Scoring and
// wording live in lib/category-suggestions. "Apply always" is the exact rule
// of lib/category-rules-db.
//
// A target is a visible row with no category that the user did not clear by
// hand (is_manually_categorized stays set when the category is cleared).

import { and, desc, eq, isNotNull, isNull, not, sql } from "drizzle-orm";
import { db } from "@/lib/db";
import {
  categories,
  dismissedCategorySuggestions,
  transactions,
} from "@/lib/db/schema";
import { notDeleted } from "@/lib/db/transactions";
import { isForeignKeyViolation } from "@/lib/monitors/adjustments";
import {
  buildHistoryIndex,
  dismissalKey,
  normalizeDescription,
  suggestCategory,
  type CategorySuggestion,
  type HistoryIndex,
} from "@/lib/category-suggestions";

/** Most recent (description, category) groups the history keeps. */
export const HISTORY_GROUP_LIMIT = 5000;
/** Uncategorized descriptions considered on the Categorías page, most rows
 *  first. */
export const TARGET_GROUP_LIMIT = 500;

/** Spaces-only `btrim`, the key the exact-rule trigger (V041) and the rule
 *  route's backfill match on, so every path groups the same rows. */
const descriptionKey = sql<string>`lower(btrim(${transactions.description}))`;

const isTarget = and(
  notDeleted,
  isNull(transactions.categoryId),
  not(transactions.isManuallyCategorized)
);

export interface SuggestionContext {
  index: HistoryIndex;
  categoryNames: Map<string, string>;
  dismissed: Set<string>;
}

/** Everything a suggestion is computed from: the categorized history, the
 *  category names and the dismissals. */
export async function loadSuggestionContext(): Promise<SuggestionContext> {
  const [history, names, dismissals] = await Promise.all([
    db
      .select({
        description: descriptionKey,
        categoryId: transactions.categoryId,
        count: sql<number>`count(*)::int`,
      })
      .from(transactions)
      .where(and(notDeleted, isNotNull(transactions.categoryId)))
      .groupBy(descriptionKey, transactions.categoryId)
      .orderBy(desc(sql`max(${transactions.transactionDate})`))
      .limit(HISTORY_GROUP_LIMIT),
    db.select({ id: categories.id, name: categories.name }).from(categories),
    db
      .select({
        normalizedDescription: dismissedCategorySuggestions.normalizedDescription,
        categoryId: dismissedCategorySuggestions.categoryId,
      })
      .from(dismissedCategorySuggestions),
  ]);
  return {
    index: buildHistoryIndex(
      history.map((row) => ({
        description: row.description,
        categoryId: row.categoryId as string,
        count: Number(row.count),
      }))
    ),
    categoryNames: new Map(names.map((c) => [c.id, c.name])),
    dismissed: new Set(
      dismissals.map((d) => dismissalKey(d.normalizedDescription, d.categoryId))
    ),
  };
}

/** The suggestion for `description`, unless the user dismissed it. */
export function suggestionFor(
  description: string,
  context: SuggestionContext
): CategorySuggestion | null {
  const suggestion = suggestCategory(
    description,
    context.index,
    context.categoryNames
  );
  if (!suggestion) return null;
  const key = dismissalKey(normalizeDescription(description), suggestion.categoryId);
  return context.dismissed.has(key) ? null : suggestion;
}

/** One uncategorized description and what to suggest for it. */
export interface CategorySuggestionGroup extends CategorySuggestion {
  /** The description as stored (one of them, when only case or surrounding
   *  spaces differ). */
  description: string;
  /** Uncategorized rows with it. */
  count: number;
  latestDate: string;
  /** Its most recent row, the one "Aplicar siempre" sends to
   *  POST /api/transactions/[id]/category-rule. */
  sampleTransactionId: string;
}

/** Uncategorized descriptions with an undismissed suggestion, most rows
 *  first. */
export async function listCategorySuggestions(): Promise<CategorySuggestionGroup[]> {
  const [context, groups] = await Promise.all([
    loadSuggestionContext(),
    db
      .select({
        description: sql<string>`min(${transactions.description})`,
        count: sql<number>`count(*)::int`,
        latestDate: sql<string>`max(${transactions.transactionDate})::text`,
        sampleTransactionId: sql<string>`(array_agg(${transactions.id} ORDER BY ${transactions.transactionDate} DESC, ${transactions.id}))[1]::text`,
      })
      .from(transactions)
      .where(and(isTarget, sql`${descriptionKey} <> ''`))
      .groupBy(descriptionKey)
      .orderBy(
        desc(sql`count(*)`),
        desc(sql`max(${transactions.transactionDate})`),
        descriptionKey
      )
      .limit(TARGET_GROUP_LIMIT),
  ]);
  const out: CategorySuggestionGroup[] = [];
  for (const group of groups) {
    const suggestion = suggestionFor(group.description, context);
    if (!suggestion) continue;
    out.push({
      ...suggestion,
      description: group.description,
      count: Number(group.count),
      latestDate: group.latestDate,
      sampleTransactionId: group.sampleTransactionId,
    });
  }
  return out;
}

/** The suggestion for one transaction: null when it already has a category,
 *  its category was cleared by hand, or nothing qualifies. 404 for an unknown
 *  or deleted row. */
export async function suggestionForTransaction(
  transactionId: string
): Promise<{ ok: true; suggestion: CategorySuggestion | null } | { ok: false; status: 404 }> {
  const [txn] = await db
    .select({
      description: transactions.description,
      categoryId: transactions.categoryId,
      isManuallyCategorized: transactions.isManuallyCategorized,
    })
    .from(transactions)
    .where(and(eq(transactions.id, transactionId), notDeleted))
    .limit(1);
  if (!txn) return { ok: false, status: 404 };
  if (txn.categoryId !== null || txn.isManuallyCategorized) {
    return { ok: true, suggestion: null };
  }
  const context = await loadSuggestionContext();
  return { ok: true, suggestion: suggestionFor(txn.description, context) };
}

export interface SuggestionInput {
  description: string;
  categoryId: string;
}

type Failure = { ok: false; status: 400; error: string; field: string };

function failure(error: string, field: string): Failure {
  return { ok: false, status: 400, error, field };
}

async function categoryExists(categoryId: string): Promise<boolean> {
  const [row] = await db
    .select({ id: categories.id })
    .from(categories)
    .where(eq(categories.id, categoryId))
    .limit(1);
  return Boolean(row);
}

/** "Aplicar a estas N": give the category to every target row whose raw
 *  description equals `description` (trimmed, case-insensitive), marking
 *  them manual as a recategorization in the panel does. Rows with a category
 *  or cleared by hand are never touched. No rule is created. */
export async function applySuggestionOnce(
  input: SuggestionInput
): Promise<{ ok: true; updatedCount: number } | Failure> {
  if (!(await categoryExists(input.categoryId))) {
    return failure("La categoría no existe", "categoryId");
  }
  try {
    const updated = await db
      .update(transactions)
      .set({
        categoryId: input.categoryId,
        isManuallyCategorized: true,
        updatedAt: new Date(),
      })
      .where(
        and(
          isTarget,
          sql`${descriptionKey} = lower(btrim(${input.description}))`
        )
      )
      .returning({ id: transactions.id });
    return { ok: true, updatedCount: updated.length };
  } catch (error) {
    // The category was deleted between the check and the update.
    if (isForeignKeyViolation(error)) {
      return failure("La categoría no existe", "categoryId");
    }
    throw error;
  }
}

/** "Ignorar": stop suggesting `categoryId` for descriptions that normalise
 *  like `description`. Dismissing it again is a no-op. */
export async function dismissCategorySuggestion(
  input: SuggestionInput
): Promise<{ ok: true } | Failure> {
  const normalized = normalizeDescription(input.description);
  if (normalized === "") {
    return failure("La descripción no tiene palabras que recordar", "description");
  }
  if (!(await categoryExists(input.categoryId))) {
    return failure("La categoría no existe", "categoryId");
  }
  try {
    await db
      .insert(dismissedCategorySuggestions)
      .values({ normalizedDescription: normalized, categoryId: input.categoryId })
      .onConflictDoNothing();
  } catch (error) {
    if (isForeignKeyViolation(error)) {
      return failure("La categoría no existe", "categoryId");
    }
    throw error;
  }
  return { ok: true };
}
