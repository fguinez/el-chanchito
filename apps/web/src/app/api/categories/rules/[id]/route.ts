import { NextRequest, NextResponse } from "next/server";
import { eq } from "drizzle-orm";
import { db } from "@/lib/db";
import { categories, categoryRules } from "@/lib/db/schema";
import { UUID_RE, validateRuleInput } from "@/lib/categories";

// Next 16: dynamic segment params arrive as a Promise on the context arg.
type Context = { params: Promise<{ id: string }> };

/** PATCH /api/categories/rules/[id]: partial update `{ keyword?, categoryId?,
 *  priority? }`. Categories the rule already assigned are left as they are. */
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

    const result = validateRuleInput(body, { partial: true });
    if (!result.ok) {
      return NextResponse.json(
        { error: result.error, field: result.field },
        { status: result.status }
      );
    }

    const [existing] = await db
      .select({ id: categoryRules.id })
      .from(categoryRules)
      .where(eq(categoryRules.id, id));
    if (!existing) {
      return NextResponse.json({ error: "Not found" }, { status: 404 });
    }

    if (result.value.categoryId !== undefined) {
      const [category] = await db
        .select({ id: categories.id })
        .from(categories)
        .where(eq(categories.id, result.value.categoryId));
      if (!category) {
        return NextResponse.json(
          { error: "Category not found", field: "categoryId" },
          { status: 400 }
        );
      }
    }

    const [updated] = await db
      .update(categoryRules)
      .set(result.value)
      .where(eq(categoryRules.id, id))
      .returning();
    if (!updated) {
      return NextResponse.json({ error: "Not found" }, { status: 404 });
    }
    return NextResponse.json(updated);
  } catch (error) {
    console.error("PATCH /api/categories/rules/[id] failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}

/** DELETE /api/categories/rules/[id]: hard delete. Categories the rule
 *  already assigned are left as they are. Returns `{ id }`. */
export async function DELETE(_request: NextRequest, { params }: Context) {
  try {
    const { id } = await params;
    if (!UUID_RE.test(id)) {
      return NextResponse.json({ error: "Invalid id" }, { status: 400 });
    }

    const [deleted] = await db
      .delete(categoryRules)
      .where(eq(categoryRules.id, id))
      .returning({ id: categoryRules.id });
    if (!deleted) {
      return NextResponse.json({ error: "Not found" }, { status: 404 });
    }
    return NextResponse.json(deleted);
  } catch (error) {
    console.error("DELETE /api/categories/rules/[id] failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}
