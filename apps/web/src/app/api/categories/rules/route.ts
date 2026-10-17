import { NextRequest, NextResponse } from "next/server";
import { eq } from "drizzle-orm";
import { db } from "@/lib/db";
import { categories, categoryRules } from "@/lib/db/schema";
import { failureBody, validateRuleInput } from "@/lib/categories";
import { readJsonBody } from "@/lib/api/validation";

/** POST /api/categories/rules: create a rule `{ keyword, categoryId,
 *  priority? }`. The keyword is stored trimmed and lowercased; it applies to
 *  transactions inserted from now on and to the next PUT /api/categories. */
export async function POST(request: NextRequest) {
  try {
    const read = await readJsonBody(request);
    if (!read.ok) return read.response;
    const body = read.body;

    const result = validateRuleInput(body);
    if (!result.ok) {
      return NextResponse.json(failureBody(result), { status: result.status });
    }

    const [category] = await db
      .select({ id: categories.id })
      .from(categories)
      .where(eq(categories.id, result.value.categoryId));
    if (!category) {
      return NextResponse.json(
        failureBody({ error: "Category not found", field: "categoryId" }),
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
