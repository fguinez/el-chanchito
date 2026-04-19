import { NextRequest, NextResponse } from "next/server";
import { db } from "@/lib/db";
import { incomeSources } from "@/lib/db/schema";
import { eq } from "drizzle-orm";

/** GET /api/income-sources — list all income sources */
export async function GET() {
  const rows = await db.select().from(incomeSources);

  const total = rows.reduce((sum, r) => sum + r.monthlyAmount, 0);
  const enriched = rows.map((r) => ({
    ...r,
    ratio: total > 0 ? r.monthlyAmount / total : 0,
  }));

  return NextResponse.json({ sources: enriched, total });
}

/** POST /api/income-sources — create an income source */
export async function POST(request: NextRequest) {
  const body = await request.json();
  const { name, monthlyAmount } = body;

  if (!name || !monthlyAmount) {
    return NextResponse.json(
      { error: "Missing required fields: name, monthlyAmount" },
      { status: 400 }
    );
  }

  const [created] = await db
    .insert(incomeSources)
    .values({ name, monthlyAmount: Math.round(monthlyAmount) })
    .returning();

  return NextResponse.json(created, { status: 201 });
}

/** DELETE /api/income-sources — delete an income source */
export async function DELETE(request: NextRequest) {
  const { id } = await request.json();

  if (!id) {
    return NextResponse.json({ error: "Missing id" }, { status: 400 });
  }

  await db.delete(incomeSources).where(eq(incomeSources.id, id));
  return NextResponse.json({ ok: true });
}
