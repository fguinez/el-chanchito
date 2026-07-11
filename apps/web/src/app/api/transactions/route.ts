import { NextRequest, NextResponse } from "next/server";
import { db } from "@/lib/db";
import { transactions } from "@/lib/db/schema";
import { resolveProductId } from "@/lib/db/resolve";
import { eq, desc, and } from "drizzle-orm";

/** GET /api/transactions — list transactions with optional filters */
export async function GET(request: NextRequest) {
  const searchParams = request.nextUrl.searchParams;
  const month = searchParams.get("month");
  const productId =
    searchParams.get("productId") ?? searchParams.get("accountId");
  const limit = parseInt(searchParams.get("limit") ?? "50");

  const conditions = [];
  if (month) {
    conditions.push(eq(transactions.scheduledMonth, month));
  }
  if (productId) {
    conditions.push(eq(transactions.productId, productId));
  }

  const rows = await db
    .select({
      id: transactions.id,
      description: transactions.description,
      amount: transactions.amount,
      transactionDate: transactions.transactionDate,
      scheduledMonth: transactions.scheduledMonth,
      source: transactions.source,
      productId: transactions.productId,
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

  const { description, amount, transactionDate, scheduledMonth, notes } = body;
  const productId = body.productId ?? body.accountId;

  if (!description || amount === undefined || !transactionDate) {
    return NextResponse.json(
      { error: "Missing required fields: description, amount, transactionDate" },
      { status: 400 }
    );
  }

  // If no product specified, resolve or create the manual-entry product
  const resolvedProductId =
    productId ?? (await resolveProductId("manual", "checking"));

  // Derive scheduledMonth from transactionDate if not provided
  const txDate = new Date(transactionDate);
  const derivedMonth =
    scheduledMonth ??
    `${txDate.getFullYear()}-${String(txDate.getMonth() + 1).padStart(2, "0")}-01`;

  const [created] = await db
    .insert(transactions)
    .values({
      productId: resolvedProductId,
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
