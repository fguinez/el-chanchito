import { NextResponse } from "next/server";
import { db } from "@/lib/db";
import { budgetConfigs } from "@/lib/db/schema";
import { desc, eq } from "drizzle-orm";

/**
 * POST /api/month-reset — create next month's budget config from current.
 *
 * Replaces the manual "Pasos inicio mes" Excel checklist:
 * 1. Copy current month's budget params to a new month
 * 2. Reset checking initial balance to 0
 * 3. New month starts with fresh adjustments (none carried over)
 */
export async function POST() {
  // Get the latest config
  const [latest] = await db
    .select()
    .from(budgetConfigs)
    .orderBy(desc(budgetConfigs.month))
    .limit(1);

  if (!latest) {
    return NextResponse.json(
      { error: "No existing config to copy from" },
      { status: 404 }
    );
  }

  // Calculate next month (parse YYYY-MM-DD directly to avoid timezone issues)
  const parts = latest.month.split("-");
  const year = parseInt(parts[0]);
  const month = parseInt(parts[1]);
  const nextYear = month === 12 ? year + 1 : year;
  const nextMonthNum = month === 12 ? 1 : month + 1;
  const nextMonthStr = `${nextYear}-${String(nextMonthNum).padStart(2, "0")}-01`;

  // Check if next month already exists
  const existing = await db
    .select()
    .from(budgetConfigs)
    .where(eq(budgetConfigs.month, nextMonthStr))
    .limit(1);

  if (existing.length > 0) {
    return NextResponse.json(
      { error: "Next month config already exists", month: nextMonthStr },
      { status: 409 }
    );
  }

  // Create new month config (copy params, reset checking balance)
  const [created] = await db
    .insert(budgetConfigs)
    .values({
      month: nextMonthStr,
      variableBudget: latest.variableBudget,
      fixedBudget: latest.fixedBudget,
      creditCardLimit: latest.creditCardLimit,
      checkingInitialBalance: 0,
      salary: latest.salary,
      sharedExpensesRatio: latest.sharedExpensesRatio,
      dayStart: latest.dayStart,
    })
    .returning();

  return NextResponse.json({
    message: `Mes ${nextMonthStr} creado`,
    config: created,
    previousMonth: latest.month,
  });
}
