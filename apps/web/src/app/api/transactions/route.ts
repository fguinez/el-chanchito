import { NextRequest, NextResponse } from "next/server";
import { db } from "@/lib/db";
import { products, transactions } from "@/lib/db/schema";
import { resolveProductId } from "@/lib/db/resolve";
import { eq, desc, and, isNull } from "drizzle-orm";
import { parseSearchParams, withJsonBody } from "@/lib/api/validation";
import {
  createTransactionSchema,
  listTransactionsQuerySchema,
  monthStart,
} from "@/lib/api/schemas";
import {
  locationToColumns,
  validateLocationInput,
} from "@/lib/transaction-location";

/** GET /api/transactions — list transactions with optional filters */
export async function GET(request: NextRequest) {
  const query = parseSearchParams(
    request.nextUrl.searchParams,
    listTransactionsQuerySchema
  );
  if (!query.ok) return query.response;
  const { month, limit } = query.data;
  const productId = query.data.productId ?? query.data.accountId;

  // Soft-deleted rows (V033) never appear in any dashboard list.
  const conditions = [isNull(transactions.deletedAt)];
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
      // `amount` is in this currency; only CLP rows may be summed as pesos.
      currency: products.currency,
      categoryId: transactions.categoryId,
      isInternalTransfer: transactions.isInternalTransfer,
      notes: transactions.notes,
      createdAt: transactions.createdAt,
    })
    .from(transactions)
    .innerJoin(products, eq(transactions.productId, products.id))
    .where(conditions.length > 0 ? and(...conditions) : undefined)
    .orderBy(desc(transactions.transactionDate))
    .limit(limit);

  return NextResponse.json(rows);
}

/** POST /api/transactions: create a manual transaction. An optional
 *  `location` ({ name?, city?, country?, latitude?, longitude? }) is validated
 *  like PATCH /api/transactions/[id] does; 400 `{ error, field }` when not. */
export const POST = withJsonBody(createTransactionSchema, async (body) => {
  const { description, amount, transactionDate, scheduledMonth, notes } = body;
  const productId = body.productId ?? body.accountId;

  const location = validateLocationInput(body.location ?? null);
  if (!location.ok) {
    return NextResponse.json(
      { error: location.error, field: location.field },
      { status: location.status }
    );
  }

  // If no product specified, resolve or create the manual-entry product
  const resolvedProductId =
    productId ?? (await resolveProductId("manual", "checking"));

  const [created] = await db
    .insert(transactions)
    .values({
      productId: resolvedProductId,
      description,
      amount,
      transactionDate,
      scheduledMonth: scheduledMonth ?? monthStart(transactionDate),
      source: "manual",
      notes: notes ?? null,
      ...locationToColumns(location.value),
      // A location typed in by the user is theirs, like one set via PATCH.
      locationIsManual: location.value !== null,
    })
    .returning();

  return NextResponse.json(created, { status: 201 });
});
