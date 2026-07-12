import { NextRequest, NextResponse } from "next/server";
import { db } from "@/lib/db";
import { budgetConfigs, budgetAdjustments, transactions, products, accounts, institutions } from "@/lib/db/schema";
import { eq, desc, sql, and, gt, inArray, isNotNull } from "drizzle-orm";
import {
  generatePlanningTable,
  getTodayStatus,
  getDaysInMonth,
  calcCupoTcMes,
  type BudgetConfig,
} from "@/lib/budget-engine";
import { getClpRates, toClp } from "@/lib/rates";

/** GET /api/planning — get the planning table + today status for current month */
export async function GET(request: NextRequest) {
  const searchParams = request.nextUrl.searchParams;
  const monthParam = searchParams.get("month");

  const now = new Date();
  const todayDay = now.getDate();
  const currentYear = now.getFullYear();
  const currentMonth = now.getMonth() + 1;
  const daysInMonth = getDaysInMonth(currentYear, currentMonth);

  // Get budget config
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
    return NextResponse.json(
      { error: "No budget configuration found. Create one in Settings." },
      { status: 404 }
    );
  }

  // Get adjustments for this config
  const adjustments = await db
    .select()
    .from(budgetAdjustments)
    .where(eq(budgetAdjustments.budgetConfigId, config.id));

  // Calculate future debts (transactions scheduled for months after current)
  const currentMonthStr = `${currentYear}-${String(currentMonth).padStart(2, "0")}-01`;
  const futureDebtsResult = await db
    .select({ total: sql<number>`COALESCE(SUM(amount), 0)` })
    .from(transactions)
    .where(gt(transactions.scheduledMonth, currentMonthStr));
  const futureDebts = Math.abs(futureDebtsResult[0]?.total ?? 0);

  // Calculate variable spending this month
  const spentResult = await db
    .select({ total: sql<number>`COALESCE(SUM(amount), 0)` })
    .from(transactions)
    .where(eq(transactions.scheduledMonth, currentMonthStr));
  const spentVariable = Math.abs(spentResult[0]?.total ?? 0);

  const budgetConfig: BudgetConfig = {
    variableBudget: config.variableBudget,
    fixedBudget: config.fixedBudget,
    creditCardLimit: config.creditCardLimit,
    checkingInitialBalance: config.checkingInitialBalance,
    salary: config.salary,
    dayStart: config.dayStart,
  };

  const adjustmentsByDay = adjustments.map((a) => ({
    day: new Date(a.adjustmentDate).getDate(),
    amount: a.amount,
  }));

  const plans = generatePlanningTable(
    budgetConfig,
    daysInMonth,
    adjustmentsByDay,
    futureDebts,
    todayDay
  );

  // Get real balance from scrapers (sum of BanChile checking + credit card
  // balances). BdC holds USD products too, so each balance is converted to CLP
  // from its own currency before summing — the planning drift is a CLP figure.
  const balanceRows = await db
    .select({ balance: products.currentBalance, currency: products.currency })
    .from(products)
    .innerJoin(accounts, eq(products.accountId, accounts.id))
    .innerJoin(institutions, eq(accounts.institutionId, institutions.id))
    .where(
      and(
        eq(institutions.slug, "banchile"),
        inArray(products.kind, ["checking", "credit_card"]),
        isNotNull(products.currentBalance)
      )
    );

  let realBalance: number | null = null;
  if (balanceRows.length > 0) {
    const rates = await getClpRates();
    // Sum only the convertible balances; a balance with no known rate is left
    // out rather than added raw (an omission beats a wrong figure). realBalance
    // stays null if nothing converts — CLP always does, so this is a safety net.
    let sum = 0;
    let converted = false;
    for (const r of balanceRows) {
      const clp = toClp(r.currency, Number(r.balance), rates);
      if (clp != null) {
        sum += clp;
        converted = true;
      }
    }
    if (converted) realBalance = sum;
  }

  const todayStatus = getTodayStatus(
    budgetConfig,
    daysInMonth,
    adjustmentsByDay,
    futureDebts,
    todayDay,
    realBalance,
    spentVariable
  );

  return NextResponse.json({
    config: budgetConfig,
    plans,
    todayStatus,
    month: currentMonthStr,
    daysInMonth,
  });
}
