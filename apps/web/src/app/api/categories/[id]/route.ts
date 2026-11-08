import { NextRequest, NextResponse } from "next/server";
import { and, eq, ne, sql } from "drizzle-orm";
import { db } from "@/lib/db";
import { categories, categoryRules, transactions } from "@/lib/db/schema";
import {
  CATEGORY_ONE_LEVEL_CLASH,
  CATEGORY_ONE_LEVEL_CONSTRAINT,
  CATEGORY_PARENT_FK,
  CATEGORY_PARENT_MISSING,
  UUID_RE,
  checkParentPlacement,
  failureBody,
  validateCategoryInput,
} from "@/lib/categories";
import { isCheckViolation, isForeignKeyViolation } from "@/lib/management";
import { readJsonBody } from "@/lib/api/validation";

// Next 16: dynamic segment params arrive as a Promise on the context arg.
type Context = { params: Promise<{ id: string }> };

/** PATCH /api/categories/[id]: partial update `{ name?, color?, icon?,
 *  parentId? }`. A new name must not clash, case-insensitively, with another
 *  category (409). `parentId` moves the category under a top-level one, or
 *  back to top-level with null; a category with subcategories cannot move
 *  under another (400 on `parentId`, as for a missing or nested parent). */
export async function PATCH(request: NextRequest, { params }: Context) {
  try {
    const { id } = await params;
    if (!UUID_RE.test(id)) {
      return NextResponse.json({ error: "Invalid id" }, { status: 400 });
    }

    const read = await readJsonBody(request);
    if (!read.ok) return read.response;
    const body = read.body;

    const result = validateCategoryInput(body, { partial: true });
    if (!result.ok) {
      return NextResponse.json(failureBody(result), { status: result.status });
    }

    const [existing] = await db
      .select({ id: categories.id })
      .from(categories)
      .where(eq(categories.id, id));
    if (!existing) {
      return NextResponse.json({ error: "Not found" }, { status: 404 });
    }

    const parentId = result.value.parentId;
    if (parentId != null) {
      const [[parent], [child]] = await Promise.all([
        db
          .select({ id: categories.id, parentId: categories.parentId })
          .from(categories)
          .where(eq(categories.id, parentId)),
        db
          .select({ id: categories.id })
          .from(categories)
          .where(eq(categories.parentId, id))
          .limit(1),
      ]);
      const placement = checkParentPlacement({
        id,
        parentId,
        parent,
        hasChildren: child !== undefined,
      });
      if (!placement.ok) {
        return NextResponse.json(failureBody(placement), { status: 400 });
      }
    }

    if (result.value.name !== undefined) {
      const [clash] = await db
        .select({ id: categories.id })
        .from(categories)
        .where(
          and(
            sql`lower(${categories.name}) = lower(${result.value.name})`,
            ne(categories.id, id)
          )
        );
      if (clash) {
        return NextResponse.json(
          { error: "A category with that name already exists", field: "name" },
          { status: 409 }
        );
      }
    }

    const [updated] = await db
      .update(categories)
      .set(result.value)
      .where(eq(categories.id, id))
      .returning();
    if (!updated) {
      return NextResponse.json({ error: "Not found" }, { status: 404 });
    }
    return NextResponse.json(updated);
  } catch (error) {
    // The tree changed between the checks and the update (V046's trigger).
    if (isCheckViolation(error, CATEGORY_ONE_LEVEL_CONSTRAINT)) {
      return NextResponse.json(failureBody(CATEGORY_ONE_LEVEL_CLASH), {
        status: 400,
      });
    }
    // The new parent was deleted between the check and the update.
    if (isForeignKeyViolation(error, CATEGORY_PARENT_FK)) {
      return NextResponse.json(failureBody(CATEGORY_PARENT_MISSING), {
        status: 400,
      });
    }
    console.error("PATCH /api/categories/[id] failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}

/**
 * DELETE /api/categories/[id]: delete a category even when in use, in one
 * transaction. Its transactions become uncategorized and lose the manual flag
 * (the user's choice no longer exists, so rules may categorize them again),
 * its subcategories become top-level (keeping their own rows, which stop
 * counting toward it), and its rules are deleted. Returns
 * `{ id, uncategorized, rulesDeleted }`.
 */
export async function DELETE(_request: NextRequest, { params }: Context) {
  try {
    const { id } = await params;
    if (!UUID_RE.test(id)) {
      return NextResponse.json({ error: "Invalid id" }, { status: 400 });
    }

    const outcome = await db.transaction(async (tx) => {
      const [existing] = await tx
        .select({ id: categories.id, color: categories.color })
        .from(categories)
        .where(eq(categories.id, id))
        .for("update");
      if (!existing) return null;

      const uncategorized = await tx
        .update(transactions)
        .set({
          categoryId: null,
          isManuallyCategorized: false,
          updatedAt: new Date(),
        })
        .where(eq(transactions.categoryId, id))
        .returning({ id: transactions.id });
      await tx
        .update(categories)
        .set({
          parentId: null,
          // Keep the color they showed, inherited from the deleted parent.
          color: sql`coalesce(${categories.color}, ${existing.color})`,
        })
        .where(eq(categories.parentId, id));
      const rules = await tx
        .delete(categoryRules)
        .where(eq(categoryRules.categoryId, id))
        .returning({ id: categoryRules.id });
      await tx.delete(categories).where(eq(categories.id, id));

      return {
        id,
        uncategorized: uncategorized.length,
        rulesDeleted: rules.length,
      };
    });

    if (!outcome) {
      return NextResponse.json({ error: "Not found" }, { status: 404 });
    }
    return NextResponse.json(outcome);
  } catch (error) {
    console.error("DELETE /api/categories/[id] failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}
