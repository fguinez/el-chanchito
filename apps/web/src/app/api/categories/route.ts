import { NextRequest, NextResponse } from "next/server";
import { and, asc, count, desc, eq, isNotNull, sql } from "drizzle-orm";
import { db } from "@/lib/db";
import { categories, categoryRules, transactions } from "@/lib/db/schema";
import { notDeleted } from "@/lib/db/transactions";
import {
  CATEGORY_ONE_LEVEL_CLASH,
  CATEGORY_ONE_LEVEL_CONSTRAINT,
  CATEGORY_PARENT_FK,
  CATEGORY_PARENT_MISSING,
  checkParentPlacement,
  failureBody,
  validateCategoryInput,
} from "@/lib/categories";
import { isCheckViolation, isForeignKeyViolation } from "@/lib/management";
import { readJsonBody } from "@/lib/api/validation";

/**
 * GET /api/categories: every category ordered by name, each with its `rules`
 * (priority desc, oldest first on ties, the order they apply in), its
 * `parentId` (V046), its `directTransactionCount` (rows assigned to it) and
 * its `transactionCount`: the direct count plus, for a top-level category,
 * its subcategories' rows, since those count toward it too.
 */
export async function GET() {
  try {
    const [cats, rules, counts] = await Promise.all([
      db.select().from(categories).orderBy(asc(categories.name)),
      db
        .select()
        .from(categoryRules)
        .orderBy(
          desc(categoryRules.priority),
          asc(categoryRules.createdAt),
          asc(categoryRules.id)
        ),
      db
        .select({ categoryId: transactions.categoryId, n: count() })
        .from(transactions)
        // Soft-deleted rows (V033) are not counted against their category.
        .where(and(isNotNull(transactions.categoryId), notDeleted))
        .groupBy(transactions.categoryId),
    ]);

    const countByCategory = new Map(counts.map((c) => [c.categoryId, c.n]));
    const rolledUp = new Map<string, number>();
    for (const cat of cats) {
      const n = countByCategory.get(cat.id) ?? 0;
      const top = cat.parentId ?? cat.id;
      rolledUp.set(top, (rolledUp.get(top) ?? 0) + n);
      if (cat.parentId) rolledUp.set(cat.id, (rolledUp.get(cat.id) ?? 0) + n);
    }
    const result = cats.map((cat) => ({
      ...cat,
      rules: rules.filter((r) => r.categoryId === cat.id),
      directTransactionCount: countByCategory.get(cat.id) ?? 0,
      transactionCount: rolledUp.get(cat.id) ?? 0,
    }));

    return NextResponse.json(result);
  } catch (error) {
    console.error("GET /api/categories failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}

/** POST /api/categories: create a category `{ name, color?, icon?,
 *  parentId? }`. Names are unique case-insensitively (409); a `parentId` must
 *  name an existing top-level category (400 on `parentId`). */
export async function POST(request: NextRequest) {
  try {
    const read = await readJsonBody(request);
    if (!read.ok) return read.response;
    const body = read.body;

    const result = validateCategoryInput(body);
    if (!result.ok) {
      return NextResponse.json(failureBody(result), { status: result.status });
    }

    const [clash] = await db
      .select({ id: categories.id })
      .from(categories)
      .where(sql`lower(${categories.name}) = lower(${result.value.name})`);
    if (clash) {
      return NextResponse.json(
        { error: "A category with that name already exists", field: "name" },
        { status: 409 }
      );
    }

    const { parentId } = result.value;
    if (parentId !== null) {
      const [parent] = await db
        .select({ id: categories.id, parentId: categories.parentId })
        .from(categories)
        .where(eq(categories.id, parentId));
      const placement = checkParentPlacement({
        parentId,
        parent,
        hasChildren: false,
      });
      if (!placement.ok) {
        return NextResponse.json(failureBody(placement), { status: 400 });
      }
    }

    const [created] = await db
      .insert(categories)
      .values(result.value)
      .returning();
    return NextResponse.json(created, { status: 201 });
  } catch (error) {
    // The parent became a subcategory between the check and the insert.
    if (isCheckViolation(error, CATEGORY_ONE_LEVEL_CONSTRAINT)) {
      return NextResponse.json(failureBody(CATEGORY_ONE_LEVEL_CLASH), {
        status: 400,
      });
    }
    // The parent was deleted between the check and the insert.
    if (isForeignKeyViolation(error, CATEGORY_PARENT_FK)) {
      return NextResponse.json(failureBody(CATEGORY_PARENT_MISSING), {
        status: 400,
      });
    }
    console.error("POST /api/categories failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}

/**
 * PUT /api/categories: the "Categorizar ahora" backfill. Applies the rules to
 * every uncategorized, not manually categorized transaction in one UPDATE,
 * through the same `category_for_description` function the insert trigger
 * uses (V021). Returns `{ assigned }`.
 */
export async function PUT() {
  try {
    // MATERIALIZED keeps Postgres from inlining the CTE, so the function runs
    // once per row; the outer conditions are re-checked on rows another
    // writer changed in the meantime.
    const updated = await db.execute(sql`
      WITH m AS MATERIALIZED (
        SELECT id, category_for_description(description) AS category_id
        FROM transactions
        WHERE category_id IS NULL
          AND NOT is_manually_categorized
          AND deleted_at IS NULL
      )
      UPDATE transactions t
      SET category_id = m.category_id, updated_at = now()
      FROM m
      WHERE t.id = m.id
        AND m.category_id IS NOT NULL
        AND t.category_id IS NULL
        AND NOT t.is_manually_categorized
      RETURNING t.id
    `);

    return NextResponse.json({ assigned: updated.length });
  } catch (error) {
    console.error("PUT /api/categories failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}
