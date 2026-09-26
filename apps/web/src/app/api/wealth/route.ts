import { NextRequest, NextResponse } from "next/server";
import { db } from "@/lib/db";
import {
  wealthSnapshots,
  products,
  productSnapshots,
  accounts,
  institutions,
} from "@/lib/db/schema";
import { eq, asc, max } from "drizzle-orm";
import { getClpRates } from "@/lib/rates";
import {
  buildWealthSeries,
  derivedSeriesStart,
  legacyEntryCutoff,
  validateLegacySnapshot,
} from "@/lib/wealth";

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

/**
 * POST /api/wealth: backdate a legacy snapshot (API only, no UI). The date
 * must fall before today and before the first real product observation after
 * the latest legacy date, so it never hides a computed point; dates inside
 * legacy history are always accepted. 409 otherwise, and for a date that
 * already has one.
 */
export async function POST(request: NextRequest) {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "Invalid JSON body" }, { status: 400 });
  }

  // No SQL filter: the rule lives in derivedSeriesStart alone.
  const [[{ lastLegacyDate }], snapshots] = await Promise.all([
    db
      .select({ lastLegacyDate: max(wealthSnapshots.snapshotDate) })
      .from(wealthSnapshots),
    db
      .select({ asOf: productSnapshots.asOf, source: productSnapshots.source })
      .from(productSnapshots),
  ]);
  const derivedStart = derivedSeriesStart(snapshots, lastLegacyDate);

  const result = validateLegacySnapshot(
    body,
    legacyEntryCutoff(derivedStart, new Date())
  );
  if (!result.ok) {
    return NextResponse.json(
      { error: result.error },
      { status: result.status }
    );
  }

  const [created] = await db
    .insert(wealthSnapshots)
    .values(result.value)
    .onConflictDoNothing({ target: wealthSnapshots.snapshotDate })
    .returning();
  if (!created) {
    return NextResponse.json(
      {
        error: `A legacy snapshot already exists for ${result.value.snapshotDate}`,
      },
      { status: 409 }
    );
  }

  return NextResponse.json(created, { status: 201 });
}

/** DELETE /api/wealth: delete a legacy snapshot (computed points are derived) */
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
