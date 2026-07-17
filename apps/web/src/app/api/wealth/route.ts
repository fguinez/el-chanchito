import { NextRequest, NextResponse } from "next/server";
import { db } from "@/lib/db";
import {
  wealthSnapshots,
  products,
  productSnapshots,
  accounts,
  institutions,
  type ProductKind,
  type ProductMetrics,
} from "@/lib/db/schema";
import { eq, asc } from "drizzle-orm";
import { calcWealthMetrics } from "@/lib/budget-engine";
import { getClpRates } from "@/lib/rates";
import { assetClp, debtClp } from "@/lib/networth";

interface WealthPoint {
  id: string;
  snapshotDate: string;
  patrimonio: number;
  deuda: number;
  fintualBalance: number | null;
  mercadopagoBalance: number | null;
  banchileSavings: number | null;
  notes: string | null;
  source: "manual" | "computed";
}

/** A snapshot's typed metrics, or null for rows that predate them (`{}`). */
function snapshotMetrics(
  metrics: ProductMetrics | Record<string, never>
): ProductMetrics | null {
  return "kind" in metrics ? (metrics as ProductMetrics) : null;
}

/**
 * GET /api/wealth — wealth series with derived metrics.
 *
 * Pre-migration dates come from legacy `wealth_snapshots` totals (which may
 * include components that never became products). Later dates are computed
 * from `product_snapshots`, carrying each product's latest observation forward
 * per date: patrimonio = Σ asset value, deuda = Σ owed, both in CLP. Debt
 * derives from each snapshot's *own* metrics (the limit/owed as observed on
 * that date), not from today's product row.
 *
 * Foreign/crypto balances are converted to CLP with current Buda tickers (see
 * lib/networth). Note: only *current* rates are available, so historical
 * points are valued at today's prices — acceptable while the computed series is
 * short; storing per-date rates would be the fix once history accumulates.
 */
export async function GET() {
  const rates = await getClpRates();

  const legacy = await db
    .select()
    .from(wealthSnapshots)
    .orderBy(asc(wealthSnapshots.snapshotDate));

  const balanceRows = await db
    .select({
      productId: productSnapshots.productId,
      balance: productSnapshots.balance,
      metrics: productSnapshots.metrics,
      asOf: productSnapshots.asOf,
      kind: products.kind,
      currency: products.currency,
      slug: institutions.slug,
    })
    .from(productSnapshots)
    .innerJoin(products, eq(productSnapshots.productId, products.id))
    .innerJoin(accounts, eq(products.accountId, accounts.id))
    .innerJoin(institutions, eq(accounts.institutionId, institutions.id))
    .orderBy(asc(productSnapshots.asOf));

  // Legacy totals are authoritative up to their last date; the backfilled
  // history rows on those dates only cover 3 components and would undercount.
  const lastLegacyDate =
    legacy.length > 0 ? legacy[legacy.length - 1].snapshotDate : "";

  // Group history rows per calendar date, then walk chronologically carrying
  // the latest balance per product.
  const byDate = new Map<string, typeof balanceRows>();
  for (const row of balanceRows) {
    const dateStr = row.asOf.toISOString().slice(0, 10);
    if (!byDate.has(dateStr)) byDate.set(dateStr, []);
    byDate.get(dateStr)!.push(row);
  }

  const latestByProduct = new Map<
    string,
    {
      balance: number;
      metrics: ProductMetrics | null;
      kind: ProductKind;
      currency: string;
      slug: string;
    }
  >();
  const computed: WealthPoint[] = [];

  for (const [dateStr, rows] of [...byDate.entries()].sort(([a], [b]) =>
    a.localeCompare(b)
  )) {
    for (const row of rows) {
      latestByProduct.set(row.productId, {
        balance: Number(row.balance),
        metrics: snapshotMetrics(row.metrics),
        kind: row.kind,
        currency: row.currency,
        slug: row.slug,
      });
    }

    if (dateStr <= lastLegacyDate) continue;

    let patrimonio = 0;
    let deuda = 0;
    let fintualBalance: number | null = null;
    let mercadopagoBalance: number | null = null;
    let banchileSavings: number | null = null;

    for (const p of latestByProduct.values()) {
      patrimonio += assetClp(p.kind, p.balance, p.currency, rates) ?? 0;
      deuda += debtClp(p.kind, p.balance, p.metrics, p.currency, rates) ?? 0;
      // These component columns are CLP-denominated products (informational).
      if (p.slug === "fintual" && p.kind === "investment")
        fintualBalance = p.balance;
      if (p.slug === "mercadopago" && p.kind === "wallet")
        mercadopagoBalance = p.balance;
      if (p.slug === "banchile" && p.kind === "savings")
        banchileSavings = p.balance;
    }

    computed.push({
      id: `computed-${dateStr}`,
      snapshotDate: dateStr,
      patrimonio: Math.round(patrimonio),
      deuda: Math.round(deuda),
      fintualBalance,
      mercadopagoBalance,
      banchileSavings,
      notes: null,
      source: "computed",
    });
  }

  const merged: WealthPoint[] = [
    ...legacy.map((row) => ({
      id: row.id,
      snapshotDate: row.snapshotDate,
      patrimonio: row.patrimonio,
      deuda: row.deuda,
      fintualBalance: row.fintualBalance,
      mercadopagoBalance: row.mercadopagoBalance,
      banchileSavings: row.banchileSavings,
      notes: row.notes,
      source: "manual" as const,
    })),
    ...computed,
  ].sort((a, b) => a.snapshotDate.localeCompare(b.snapshotDate));

  const enriched = merged.map((row, i) => {
    const prev = i > 0 ? merged[i - 1] : null;
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
