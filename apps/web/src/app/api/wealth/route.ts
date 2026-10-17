import { NextResponse } from "next/server";
import { db } from "@/lib/db";
import {
  wealthSnapshots,
  products,
  productSnapshots,
  accounts,
  institutions,
} from "@/lib/db/schema";
import { eq, asc, max } from "drizzle-orm";
import { getRateBook } from "@/lib/rate-book";
import {
  buildWealthSeries,
  derivedSeriesStart,
  legacyDateError,
  legacyEntryCutoff,
} from "@/lib/wealth";
import { withJsonBody } from "@/lib/api/validation";
import {
  createWealthSnapshotSchema,
  deleteWealthSnapshotSchema,
} from "@/lib/api/schemas";

/** GET /api/wealth: the wealth series with derived metrics (see lib/wealth). */
export async function GET() {
  const book = await getRateBook();

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

  return NextResponse.json(buildWealthSeries(legacy, snapshots, book));
}

/**
 * POST /api/wealth: backdate a legacy snapshot (API only, no UI). The date
 * must fall before today and before the first real product observation after
 * the latest legacy date, so it never hides a computed point; dates inside
 * legacy history are always accepted. 409 otherwise, and for a date that
 * already has one.
 */
export const POST = withJsonBody(createWealthSnapshotSchema, async (body) => {
  // No SQL filter: the rule lives in derivedSeriesStart alone.
  const [[latest], snapshots] = await Promise.all([
    db
      .select({ lastLegacyDate: max(wealthSnapshots.snapshotDate) })
      .from(wealthSnapshots),
    db
      .select({ asOf: productSnapshots.asOf, source: productSnapshots.source })
      .from(productSnapshots),
  ]);
  const derivedStart = derivedSeriesStart(
    snapshots,
    latest?.lastLegacyDate ?? null
  );

  const dateError = legacyDateError(
    body.snapshotDate,
    legacyEntryCutoff(derivedStart, new Date())
  );
  if (dateError) {
    return NextResponse.json({ error: dateError }, { status: 409 });
  }

  const [created] = await db
    .insert(wealthSnapshots)
    .values({
      snapshotDate: body.snapshotDate,
      patrimonio: body.patrimonio,
      deuda: body.deuda ?? 0,
      fintualBalance: body.fintualBalance ?? null,
      mercadopagoBalance: body.mercadopagoBalance ?? null,
      banchileSavings: body.banchileSavings ?? null,
      notes: body.notes ?? null,
    })
    .onConflictDoNothing({ target: wealthSnapshots.snapshotDate })
    .returning();
  if (!created) {
    return NextResponse.json(
      { error: `A legacy snapshot already exists for ${body.snapshotDate}` },
      { status: 409 }
    );
  }

  return NextResponse.json(created, { status: 201 });
});

/** DELETE /api/wealth: delete a legacy snapshot by id (computed points are
 *  derived; 400 for them or a malformed id, 404 for an unknown one). */
export const DELETE = withJsonBody(deleteWealthSnapshotSchema, async ({ id }) => {
  const deleted = await db
    .delete(wealthSnapshots)
    .where(eq(wealthSnapshots.id, id))
    .returning({ id: wealthSnapshots.id });
  if (deleted.length === 0) {
    return NextResponse.json({ error: "Not found" }, { status: 404 });
  }
  return NextResponse.json({ ok: true });
});
