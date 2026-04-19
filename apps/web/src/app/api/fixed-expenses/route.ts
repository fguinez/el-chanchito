import { NextRequest, NextResponse } from "next/server";
import { db } from "@/lib/db";
import { fixedExpenses } from "@/lib/db/schema";
import { eq, isNull, or, gte } from "drizzle-orm";

/** GET /api/fixed-expenses — list active fixed expenses */
export async function GET() {
  const today = new Date().toISOString().split("T")[0];

  const rows = await db
    .select()
    .from(fixedExpenses)
    .where(
      or(isNull(fixedExpenses.activeTo), gte(fixedExpenses.activeTo, today))
    );

  return NextResponse.json(rows);
}

/** POST /api/fixed-expenses — create a fixed expense */
export async function POST(request: NextRequest) {
  const body = await request.json();

  const { name, amount, isShared, sharedRatio, activeFrom, activeTo } = body;

  if (!name || amount === undefined) {
    return NextResponse.json(
      { error: "Missing required fields: name, amount" },
      { status: 400 }
    );
  }

  const [created] = await db
    .insert(fixedExpenses)
    .values({
      name,
      amount: Math.round(amount),
      isShared: isShared ?? false,
      sharedRatio: isShared ? (sharedRatio?.toString() ?? "0.6900") : null,
      activeFrom: activeFrom ?? null,
      activeTo: activeTo ?? null,
    })
    .returning();

  return NextResponse.json(created, { status: 201 });
}

/** PUT /api/fixed-expenses — update a fixed expense */
export async function PUT(request: NextRequest) {
  const body = await request.json();
  const { id, ...updates } = body;

  if (!id) {
    return NextResponse.json({ error: "Missing id" }, { status: 400 });
  }

  if (updates.sharedRatio !== undefined) {
    updates.sharedRatio = updates.sharedRatio?.toString() ?? null;
  }
  updates.updatedAt = new Date();

  const [updated] = await db
    .update(fixedExpenses)
    .set(updates)
    .where(eq(fixedExpenses.id, id))
    .returning();

  if (!updated) {
    return NextResponse.json({ error: "Not found" }, { status: 404 });
  }

  return NextResponse.json(updated);
}

/** DELETE /api/fixed-expenses — delete a fixed expense */
export async function DELETE(request: NextRequest) {
  const { id } = await request.json();

  if (!id) {
    return NextResponse.json({ error: "Missing id" }, { status: 400 });
  }

  await db.delete(fixedExpenses).where(eq(fixedExpenses.id, id));

  return NextResponse.json({ ok: true });
}
