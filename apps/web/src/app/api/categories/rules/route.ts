import { NextRequest, NextResponse } from "next/server";
import { eq } from "drizzle-orm";
import { db } from "@/lib/db";
import { categories, categoryRules } from "@/lib/db/schema";
import { validateRuleInput } from "@/lib/categories";

/** POST /api/categories/rules: create a rule `{ keyword, categoryId,
 *  priority? }`. The keyword is stored trimmed and lowercased; it applies to
 *  transactions inserted from now on and to the next PUT /api/categories. */
export async function POST(request: NextRequest) {
  try {
    let body: unknown;
    try {
      body = await request.json();
    } catch {
      return NextResponse.json({ error: "Invalid JSON body" }, { status: 400 });
    }

    const result = validateRuleInput(body);
    if (!result.ok) {
      return NextResponse.json(
        { error: result.error, field: result.field },
        { status: result.status }
      );
    }

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

    const [created] = await db
      .insert(categoryRules)
      .values(result.value)
      .returning();
    return NextResponse.json(created, { status: 201 });
  } catch (error) {
    console.error("POST /api/categories/rules failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}
