import { NextRequest, NextResponse } from "next/server";
import { db } from "@/lib/db";
import { wealthSnapshots } from "@/lib/db/schema";
import { eq, asc } from "drizzle-orm";
import { calcWealthMetrics } from "@/lib/budget-engine";

/** GET /api/wealth — list all wealth snapshots with derived metrics */
export async function GET() {
  const rows = await db
    .select()
    .from(wealthSnapshots)
    .orderBy(asc(wealthSnapshots.snapshotDate));

  // Compute derived fields for each snapshot
  const enriched = rows.map((row, i) => {
    const prev = i > 0 ? rows[i - 1] : null;
    const metrics = calcWealthMetrics(
      {
        patrimonio: row.patrimonio,
        deuda: row.deuda,
        date: new Date(row.snapshotDate),
      },
      prev
        ? {
            patrimonio: prev.patrimonio,
            deuda: prev.deuda,
            date: new Date(prev.snapshotDate),
          }
        : null
    );

    return {
      ...row,
      ahorro: metrics.ahorro,
      periodSavings: metrics.periodSavings,
      monthsBetween: metrics.monthsBetween,
      monthlyRate: metrics.monthlyRate,
    };
  });

  return NextResponse.json(enriched);
}

/** POST /api/wealth — create a new wealth snapshot */
export async function POST(request: NextRequest) {
  const body = await request.json();

  const {
    snapshotDate,
    patrimonio,
    deuda,
    fintualBalance,
    mercadopagoBalance,
    banchileSavings,
    notes,
  } = body;

  if (!snapshotDate || patrimonio === undefined) {
    return NextResponse.json(
      { error: "Missing required fields: snapshotDate, patrimonio" },
      { status: 400 }
    );
  }

  const [created] = await db
    .insert(wealthSnapshots)
    .values({
      snapshotDate,
      patrimonio: Math.round(patrimonio),
      deuda: Math.round(deuda ?? 0),
      fintualBalance: fintualBalance != null ? Math.round(fintualBalance) : null,
      mercadopagoBalance:
        mercadopagoBalance != null ? Math.round(mercadopagoBalance) : null,
      banchileSavings:
        banchileSavings != null ? Math.round(banchileSavings) : null,
      notes: notes ?? null,
    })
    .returning();

  return NextResponse.json(created, { status: 201 });
}

/** DELETE /api/wealth — delete a snapshot */
export async function DELETE(request: NextRequest) {
  const { id } = await request.json();

  if (!id) {
    return NextResponse.json({ error: "Missing id" }, { status: 400 });
  }

  await db.delete(wealthSnapshots).where(eq(wealthSnapshots.id, id));
  return NextResponse.json({ ok: true });
}
