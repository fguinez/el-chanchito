import { NextRequest, NextResponse } from "next/server";
import { and, asc, count, desc, eq, isNotNull, isNull, sql } from "drizzle-orm";
import { db } from "@/lib/db";
import { categories, categoryRules, transactions } from "@/lib/db/schema";
import { validateCategoryInput } from "@/lib/categories";

/**
 * GET /api/categories: every category ordered by name, each with its `rules`
 * (priority desc, oldest first on ties, the order they apply in) and its
 * `transactionCount`.
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
        .where(isNotNull(transactions.categoryId))
        .groupBy(transactions.categoryId),
    ]);

    const countByCategory = new Map(counts.map((c) => [c.categoryId, c.n]));
    const result = cats.map((cat) => ({
      ...cat,
      rules: rules.filter((r) => r.categoryId === cat.id),
      transactionCount: countByCategory.get(cat.id) ?? 0,
    }));

    return NextResponse.json(result);
  } catch (error) {
    console.error("GET /api/categories failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}

/** POST /api/categories: create a category `{ name, color?, icon? }`. Names
 *  are unique case-insensitively (409). */
export async function POST(request: NextRequest) {
  try {
    let body: unknown;
    try {
      body = await request.json();
    } catch {
      return NextResponse.json({ error: "Invalid JSON body" }, { status: 400 });
    }

    const result = validateCategoryInput(body);
    if (!result.ok) {
      return NextResponse.json(
        { error: result.error, field: result.field },
        { status: result.status }
      );
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

    const [created] = await db
      .insert(categories)
      .values(result.value)
      .returning();
    return NextResponse.json(created, { status: 201 });
  } catch (error) {
    console.error("POST /api/categories failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}

/**
 * PUT /api/categories: the "Categorizar ahora" backfill. Applies the rules to
 * every uncategorized, not manually categorized transaction in one UPDATE,
 * through the same `category_for_description` function the insert trigger
 * uses (V020). Returns `{ assigned }`.
 */
export async function PUT() {
  try {
    const matched = sql<string | null>`category_for_description(${transactions.description})`;
    const updated = await db
      .update(transactions)
      .set({ categoryId: matched, updatedAt: new Date() })
      .where(
        and(
          isNull(transactions.categoryId),
          eq(transactions.isManuallyCategorized, false),
          isNotNull(matched)
        )
      )
      .returning({ id: transactions.id });

    return NextResponse.json({ assigned: updated.length });
  } catch (error) {
    console.error("PUT /api/categories failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}
