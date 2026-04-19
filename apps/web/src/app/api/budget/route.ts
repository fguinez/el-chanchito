import { NextRequest, NextResponse } from "next/server";
import { db } from "@/lib/db";
import { budgetConfigs } from "@/lib/db/schema";
import { eq, desc } from "drizzle-orm";

/** GET /api/budget — get the current month's config (or latest) */
export async function GET(request: NextRequest) {
  const searchParams = request.nextUrl.searchParams;
  const monthParam = searchParams.get("month");

  let config;
  if (monthParam) {
    [config] = await db
      .select()
      .from(budgetConfigs)
      .where(eq(budgetConfigs.month, monthParam))
      .limit(1);
  } else {
    [config] = await db
      .select()
      .from(budgetConfigs)
      .orderBy(desc(budgetConfigs.month))
      .limit(1);
  }

  if (!config) {
    return NextResponse.json(null, { status: 404 });
  }

  return NextResponse.json(config);
}

/** POST /api/budget — create or update a month's config */
export async function POST(request: NextRequest) {
  const body = await request.json();

  const {
    month,
    variableBudget,
    fixedBudget,
    creditCardLimit,
    checkingInitialBalance,
    salary,
    sharedExpensesRatio,
    dayStart,
  } = body;

  if (!month || !variableBudget || !fixedBudget || !creditCardLimit || !salary) {
    return NextResponse.json(
      { error: "Missing required fields" },
      { status: 400 }
    );
  }

  const monthStr = typeof month === "string" ? month : new Date(month).toISOString().split("T")[0];

  // Upsert: try update first, then insert
  const existing = await db
    .select()
    .from(budgetConfigs)
    .where(eq(budgetConfigs.month, monthStr))
    .limit(1);

  if (existing.length > 0) {
    const [updated] = await db
      .update(budgetConfigs)
      .set({
        variableBudget,
        fixedBudget,
        creditCardLimit,
        checkingInitialBalance: checkingInitialBalance ?? 0,
        salary,
        sharedExpensesRatio: sharedExpensesRatio?.toString() ?? "0.6900",
        dayStart: dayStart ?? 1,
        updatedAt: new Date(),
      })
      .where(eq(budgetConfigs.month, monthStr))
      .returning();

    return NextResponse.json(updated);
  }

  const [created] = await db
    .insert(budgetConfigs)
    .values({
      month: monthStr,
      variableBudget,
      fixedBudget,
      creditCardLimit,
      checkingInitialBalance: checkingInitialBalance ?? 0,
      salary,
      sharedExpensesRatio: sharedExpensesRatio?.toString() ?? "0.6900",
      dayStart: dayStart ?? 1,
    })
    .returning();

  return NextResponse.json(created, { status: 201 });
}
