import { NextRequest, NextResponse } from "next/server";
import { db } from "@/lib/db";
import { transactions, accounts } from "@/lib/db/schema";
import { eq, desc, and } from "drizzle-orm";

/** GET /api/transactions — list transactions with optional filters */
export async function GET(request: NextRequest) {
  const searchParams = request.nextUrl.searchParams;
  const month = searchParams.get("month");
  const accountId = searchParams.get("accountId");
  const limit = parseInt(searchParams.get("limit") ?? "50");

  const conditions = [];
  if (month) {
    conditions.push(eq(transactions.scheduledMonth, month));
  }
  if (accountId) {
    conditions.push(eq(transactions.accountId, accountId));
  }

  const rows = await db
    .select({
      id: transactions.id,
      description: transactions.description,
      amount: transactions.amount,
      transactionDate: transactions.transactionDate,
      scheduledMonth: transactions.scheduledMonth,
      source: transactions.source,
      accountId: transactions.accountId,
      categoryId: transactions.categoryId,
      isInternalTransfer: transactions.isInternalTransfer,
      notes: transactions.notes,
      createdAt: transactions.createdAt,
    })
    .from(transactions)
    .where(conditions.length > 0 ? and(...conditions) : undefined)
    .orderBy(desc(transactions.transactionDate))
    .limit(limit);

  return NextResponse.json(rows);
}

/** POST /api/transactions — create a manual transaction */
export async function POST(request: NextRequest) {
  const body = await request.json();

  const { description, amount, transactionDate, scheduledMonth, accountId, notes } = body;

  if (!description || amount === undefined || !transactionDate) {
    return NextResponse.json(
      { error: "Missing required fields: description, amount, transactionDate" },
      { status: 400 }
    );
  }

  // If no account specified, resolve or create a "manual" account
  let resolvedAccountId = accountId;
  if (!resolvedAccountId) {
    const [manualAccount] = await db
      .select()
      .from(accounts)
      .where(
        and(
          eq(accounts.institution, "manual"),
          eq(accounts.accountType, "checking")
        )
      )
      .limit(1);

    if (manualAccount) {
      resolvedAccountId = manualAccount.id;
    } else {
      const [created] = await db
        .insert(accounts)
        .values({
          name: "Entrada manual",
          institution: "manual",
          accountType: "checking",
        })
        .returning();
      resolvedAccountId = created.id;
    }
  }

  // Derive scheduledMonth from transactionDate if not provided
  const txDate = new Date(transactionDate);
  const derivedMonth =
    scheduledMonth ??
    `${txDate.getFullYear()}-${String(txDate.getMonth() + 1).padStart(2, "0")}-01`;

  const [created] = await db
    .insert(transactions)
    .values({
      accountId: resolvedAccountId,
      description,
      amount: Math.round(amount),
      transactionDate,
      scheduledMonth: derivedMonth,
      source: "manual",
      notes: notes ?? null,
    })
    .returning();

  return NextResponse.json(created, { status: 201 });
}
