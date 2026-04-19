import { NextRequest, NextResponse } from "next/server";
import { db } from "@/lib/db";
import { categories, categoryRules, transactions } from "@/lib/db/schema";
import { eq, desc, isNull, ilike, and } from "drizzle-orm";

/** GET /api/categories — list categories with their rules */
export async function GET() {
  const cats = await db.select().from(categories);

  const rules = await db
    .select()
    .from(categoryRules)
    .orderBy(desc(categoryRules.priority));

  const result = cats.map((cat) => ({
    ...cat,
    rules: rules.filter((r) => r.categoryId === cat.id),
  }));

  return NextResponse.json(result);
}

/** POST /api/categories/rules — add a category rule */
export async function POST(request: NextRequest) {
  const body = await request.json();
  const { keyword, categoryId, priority } = body;

  if (!keyword || !categoryId) {
    return NextResponse.json(
      { error: "Missing keyword or categoryId" },
      { status: 400 }
    );
  }

  const [created] = await db
    .insert(categoryRules)
    .values({
      keyword: keyword.toLowerCase(),
      categoryId,
      priority: priority ?? 0,
    })
    .returning();

  return NextResponse.json(created, { status: 201 });
}

/** PUT /api/categories — auto-assign categories to uncategorized transactions */
export async function PUT() {
  const rules = await db
    .select()
    .from(categoryRules)
    .orderBy(desc(categoryRules.priority));

  if (rules.length === 0) {
    return NextResponse.json({ assigned: 0, message: "No rules defined" });
  }

  let assigned = 0;

  for (const rule of rules) {
    const updated = await db
      .update(transactions)
      .set({ categoryId: rule.categoryId })
      .where(
        and(
          isNull(transactions.categoryId),
          eq(transactions.isManuallyCategorized, false),
          ilike(transactions.description, `%${rule.keyword}%`)
        )
      )
      .returning({ id: transactions.id });

    assigned += updated.length;
  }

  return NextResponse.json({ assigned });
}
