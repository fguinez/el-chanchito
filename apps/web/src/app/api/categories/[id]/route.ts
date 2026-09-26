import { NextRequest, NextResponse } from "next/server";
import { and, eq, ne, sql } from "drizzle-orm";
import { db } from "@/lib/db";
import { categories, categoryRules, transactions } from "@/lib/db/schema";
import { UUID_RE, validateCategoryInput } from "@/lib/categories";

// Next 16: dynamic segment params arrive as a Promise on the context arg.
type Context = { params: Promise<{ id: string }> };

/** PATCH /api/categories/[id]: partial update `{ name?, color?, icon? }`.
 *  A new name must not clash, case-insensitively, with another category
 *  (409). */
export async function PATCH(request: NextRequest, { params }: Context) {
  try {
    const { id } = await params;
    if (!UUID_RE.test(id)) {
      return NextResponse.json({ error: "Invalid id" }, { status: 400 });
    }

    let body: unknown;
    try {
      body = await request.json();
    } catch {
      return NextResponse.json({ error: "Invalid JSON body" }, { status: 400 });
    }

    const result = validateCategoryInput(body, { partial: true });
    if (!result.ok) {
      return NextResponse.json(
        { error: result.error, field: result.field },
        { status: result.status }
      );
    }

    const [existing] = await db
      .select({ id: categories.id })
      .from(categories)
      .where(eq(categories.id, id));
    if (!existing) {
      return NextResponse.json({ error: "Not found" }, { status: 404 });
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
    console.error("PATCH /api/categories/[id] failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}

/**
 * DELETE /api/categories/[id]: delete a category even when in use, in one
 * transaction. Its transactions become uncategorized and lose the manual flag
 * (the user's choice no longer exists, so rules may categorize them again),
 * its subcategories become top-level, and its rules are deleted. Returns
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
        .select({ id: categories.id })
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
        .set({ parentId: null })
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
