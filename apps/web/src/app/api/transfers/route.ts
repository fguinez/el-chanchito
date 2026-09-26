import { NextResponse } from "next/server";
import { db } from "@/lib/db";
import { internalTransfers } from "@/lib/db/schema";
import { eq, desc } from "drizzle-orm";
import { withJsonBody } from "@/lib/api/validation";
import {
  createTransferSchema,
  idSchema,
  updateTransferSchema,
} from "@/lib/api/schemas";

/** GET /api/transfers — list all internal transfers */
export async function GET() {
  const rows = await db
    .select()
    .from(internalTransfers)
    .orderBy(desc(internalTransfers.transferDate));

  return NextResponse.json(rows);
}

/** POST /api/transfers — create an internal transfer */
export const POST = withJsonBody(createTransferSchema, async (body) => {
  const { description, amount, transferDate, notes } = body;
  const fromProductId = body.fromProductId ?? body.fromAccountId ?? null;
  const toProductId = body.toProductId ?? body.toAccountId ?? null;

  const [created] = await db
    .insert(internalTransfers)
    .values({
      description,
      amount,
      fromProductId,
      toProductId,
      transferDate,
      notes: notes ?? null,
    })
    .returning();

  return NextResponse.json(created, { status: 201 });
});

/** PUT /api/transfers — update transfer status */
export const PUT = withJsonBody(updateTransferSchema, async (body) => {
  const { id, status, notes } = body;

  const [updated] = await db
    .update(internalTransfers)
    // Drizzle skips undefined keys, so an absent status or notes stays as is.
    .set({ status, notes, updatedAt: new Date() })
    .where(eq(internalTransfers.id, id))
    .returning();

  if (!updated) {
    return NextResponse.json({ error: "Not found" }, { status: 404 });
  }

  return NextResponse.json(updated);
});

/** DELETE /api/transfers — delete a transfer */
export const DELETE = withJsonBody(idSchema, async ({ id }) => {
  await db.delete(internalTransfers).where(eq(internalTransfers.id, id));
  return NextResponse.json({ ok: true });
});
