import { NextRequest, NextResponse } from "next/server";
import { db } from "@/lib/db";
import { internalTransfers } from "@/lib/db/schema";
import { eq, desc } from "drizzle-orm";

/** GET /api/transfers — list all internal transfers */
export async function GET() {
  const rows = await db
    .select()
    .from(internalTransfers)
    .orderBy(desc(internalTransfers.transferDate));

  return NextResponse.json(rows);
}

/** POST /api/transfers — create an internal transfer */
export async function POST(request: NextRequest) {
  const body = await request.json();
  const { description, amount, fromAccountId, toAccountId, transferDate, notes } = body;

  if (!description || amount === undefined || !transferDate) {
    return NextResponse.json(
      { error: "Missing required fields: description, amount, transferDate" },
      { status: 400 }
    );
  }

  const [created] = await db
    .insert(internalTransfers)
    .values({
      description,
      amount: Math.round(amount),
      fromAccountId: fromAccountId ?? null,
      toAccountId: toAccountId ?? null,
      transferDate,
      notes: notes ?? null,
    })
    .returning();

  return NextResponse.json(created, { status: 201 });
}

/** PUT /api/transfers — update transfer status */
export async function PUT(request: NextRequest) {
  const body = await request.json();
  const { id, status, notes } = body;

  if (!id) {
    return NextResponse.json({ error: "Missing id" }, { status: 400 });
  }

  const updates: Record<string, unknown> = { updatedAt: new Date() };
  if (status) updates.status = status;
  if (notes !== undefined) updates.notes = notes;

  const [updated] = await db
    .update(internalTransfers)
    .set(updates)
    .where(eq(internalTransfers.id, id))
    .returning();

  if (!updated) {
    return NextResponse.json({ error: "Not found" }, { status: 404 });
  }

  return NextResponse.json(updated);
}

/** DELETE /api/transfers — delete a transfer */
export async function DELETE(request: NextRequest) {
  const { id } = await request.json();

  if (!id) {
    return NextResponse.json({ error: "Missing id" }, { status: 400 });
  }

  await db.delete(internalTransfers).where(eq(internalTransfers.id, id));
  return NextResponse.json({ ok: true });
}
