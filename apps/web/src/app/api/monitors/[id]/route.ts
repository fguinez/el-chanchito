import { NextRequest, NextResponse } from "next/server";
import { eq } from "drizzle-orm";
import { db } from "@/lib/db";
import { monitors } from "@/lib/db/schema";
import { getClpRates } from "@/lib/rates";
import { evaluateMonitor } from "@/lib/monitors/evaluate";
import {
  loadProductCatalog,
  loadSnapshotsForProducts,
} from "@/lib/monitors/catalog";
import { validateMonitorInput } from "@/lib/monitors/validate";
import {
  buildReferences,
  enrichMonitor,
  referencedProductIds,
  replayWindow,
} from "@/lib/monitors/serialize";

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// Next 16: dynamic segment params arrive as a Promise on the context arg.
type Context = { params: Promise<{ id: string }> };

const DEFAULT_HISTORY_DAYS = 90;
const MAX_HISTORY_DAYS = 365;

/**
 * GET /api/monitors/[id]: one monitor with evaluation, snapshot-replayed
 * `history` (default 90 days, `?days=N` clamped to [1, 365]), and a
 * `references` row per distinct product/field the expressions mention.
 */
export async function GET(request: NextRequest, { params }: Context) {
  try {
    const { id } = await params;
    if (!UUID_RE.test(id)) {
      return NextResponse.json({ error: "Invalid id" }, { status: 400 });
    }

    const daysParam = request.nextUrl.searchParams.get("days");
    let days = DEFAULT_HISTORY_DAYS;
    if (daysParam != null) {
      const parsed = Number(daysParam);
      if (!Number.isFinite(parsed)) {
        return NextResponse.json(
          { error: "Invalid days parameter", field: "days" },
          { status: 400 }
        );
      }
      days = Math.min(MAX_HISTORY_DAYS, Math.max(1, Math.trunc(parsed)));
    }

    const [row] = await db.select().from(monitors).where(eq(monitors.id, id));
    if (!row) {
      return NextResponse.json({ error: "Not found" }, { status: 404 });
    }

    const [catalog, rates] = await Promise.all([
      loadProductCatalog(),
      getClpRates(),
    ]);
    const now = new Date();
    const evaluation = evaluateMonitor(row, {
      date: now,
      products: catalog.byId,
      rates,
      currency: row.currency,
    });
    const snapshots = await loadSnapshotsForProducts(referencedProductIds(row));
    const history = replayWindow(
      row,
      { snapshots, products: catalog.byId, rates, now },
      days
    );
    const references = buildReferences(row, catalog.byId);

    return NextResponse.json({
      ...enrichMonitor(row, catalog, evaluation),
      history,
      references,
    });
  } catch (error) {
    console.error("GET /api/monitors/[id] failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}

/** PUT /api/monitors/[id]: partial update; a body that includes thresholds
 *  must still carry an `alert` one. Bumps updatedAt. */
export async function PUT(request: NextRequest, { params }: Context) {
  try {
    const { id } = await params;
    if (!UUID_RE.test(id)) {
      return NextResponse.json({ error: "Invalid id" }, { status: 400 });
    }

    let body: unknown;
    try {
      body = await request.json();
    } catch {
      return NextResponse.json({ error: "Invalid JSON body" }, { status: 400 });
    }

    const catalog = await loadProductCatalog();
    const result = validateMonitorInput(body, catalog, { partial: true });
    if (!result.ok) {
      return NextResponse.json(
        { error: result.error, field: result.field, position: result.position },
        { status: result.status }
      );
    }

    const [updated] = await db
      .update(monitors)
      .set({ ...result.value, updatedAt: new Date() })
      .where(eq(monitors.id, id))
      .returning();
    if (!updated) {
      return NextResponse.json({ error: "Not found" }, { status: 404 });
    }

    const rates = await getClpRates();
    const evaluation = evaluateMonitor(updated, {
      date: new Date(),
      products: catalog.byId,
      rates,
      currency: updated.currency,
    });
    return NextResponse.json(enrichMonitor(updated, catalog, evaluation));
  } catch (error) {
    console.error("PUT /api/monitors/[id] failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}

/** DELETE /api/monitors/[id]: hard delete. */
export async function DELETE(request: NextRequest, { params }: Context) {
  try {
    const { id } = await params;
    if (!UUID_RE.test(id)) {
      return NextResponse.json({ error: "Invalid id" }, { status: 400 });
    }

    const deleted = await db
      .delete(monitors)
      .where(eq(monitors.id, id))
      .returning({ id: monitors.id });
    if (deleted.length === 0) {
      return NextResponse.json({ error: "Not found" }, { status: 404 });
    }
    return NextResponse.json({ ok: true });
  } catch (error) {
    console.error("DELETE /api/monitors/[id] failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}
