import { NextRequest, NextResponse } from "next/server";
import { db } from "@/lib/db";
import {
  wealthSnapshots,
  products,
  productSnapshots,
  accounts,
  institutions,
} from "@/lib/db/schema";
import { eq, asc } from "drizzle-orm";
import { getClpRates } from "@/lib/rates";
import { buildWealthSeries } from "@/lib/wealth";

/** GET /api/wealth: the wealth series with derived metrics (see lib/wealth). */
export async function GET() {
  const rates = await getClpRates();

  const legacy = await db
    .select()
    .from(wealthSnapshots)
    .orderBy(asc(wealthSnapshots.snapshotDate));

  const snapshots = await db
    .select({
      productId: productSnapshots.productId,
      balance: productSnapshots.balance,
      metrics: productSnapshots.metrics,
      asOf: productSnapshots.asOf,
      source: productSnapshots.source,
      kind: products.kind,
      currency: products.currency,
      slug: institutions.slug,
    })
    .from(productSnapshots)
    .innerJoin(products, eq(productSnapshots.productId, products.id))
    .innerJoin(accounts, eq(products.accountId, accounts.id))
    .innerJoin(institutions, eq(accounts.institutionId, institutions.id))
    .orderBy(asc(productSnapshots.asOf));

  return NextResponse.json(buildWealthSeries(legacy, snapshots, rates));
}

/** POST /api/wealth — create a manual wealth snapshot */
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

/** DELETE /api/wealth — delete a manual snapshot (computed points are derived) */
export async function DELETE(request: NextRequest) {
  const { id } = await request.json();

  if (!id) {
    return NextResponse.json({ error: "Missing id" }, { status: 400 });
  }
  if (typeof id === "string" && id.startsWith("computed-")) {
    return NextResponse.json(
      { error: "Computed points are derived from product balances and cannot be deleted" },
      { status: 400 }
    );
  }

  await db.delete(wealthSnapshots).where(eq(wealthSnapshots.id, id));
  return NextResponse.json({ ok: true });
}
