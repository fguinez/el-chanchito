import { NextRequest, NextResponse } from "next/server";
import { asc } from "drizzle-orm";
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
  buildSparkline,
  enrichMonitor,
  referencedProductIds,
  type SparklinePoint,
} from "@/lib/monitors/serialize";

/**
 * GET /api/monitors — every monitor with its display-form expressions and
 * current evaluation; monitors displayed as line charts also get a 30-day
 * `sparkline` replayed from product_snapshots (one combined query across all
 * referenced products). Past days are valued at current rates (see
 * lib/monitors/history).
 */
export async function GET() {
  try {
    const [catalog, rates, rows] = await Promise.all([
      loadProductCatalog(),
      getClpRates(),
      db.select().from(monitors).orderBy(asc(monitors.createdAt)),
    ]);
    const now = new Date();

    // One snapshot query feeds every sparkline; replayHistory ignores
    // products a given monitor doesn't reference.
    const sparklineProductIds = new Set<string>();
    for (const row of rows) {
      if (row.display.chart !== "line") continue;
      for (const id of referencedProductIds(row)) sparklineProductIds.add(id);
    }
    const snapshots = await loadSnapshotsForProducts([...sparklineProductIds]);

    const enriched = rows.map((row) => {
      const evaluation = evaluateMonitor(row, {
        date: now,
        products: catalog.byId,
        rates,
        currency: row.currency,
      });
      let sparkline: SparklinePoint[] | undefined;
      if (row.display.chart === "line") {
        sparkline = buildSparkline(row, {
          snapshots,
          products: catalog.byId,
          rates,
          now,
        });
      }
      return enrichMonitor(row, catalog, evaluation, sparkline);
    });

    return NextResponse.json({ monitors: enriched });
  } catch (error) {
    console.error("GET /api/monitors failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}

/** POST /api/monitors — create a monitor. Expressions arrive in display or
 *  uuid-ref form and persist normalized to uuid-ref form. */
export async function POST(request: NextRequest) {
  try {
    let body: unknown;
    try {
      body = await request.json();
    } catch {
      return NextResponse.json({ error: "Invalid JSON body" }, { status: 400 });
    }

    const catalog = await loadProductCatalog();
    const result = validateMonitorInput(body, catalog);
    if (!result.ok) {
      return NextResponse.json(
        { error: result.error, field: result.field, position: result.position },
        { status: result.status }
      );
    }

    const [created] = await db.insert(monitors).values(result.value).returning();

    const rates = await getClpRates();
    const evaluation = evaluateMonitor(created, {
      date: new Date(),
      products: catalog.byId,
      rates,
      currency: created.currency,
    });
    return NextResponse.json(enrichMonitor(created, catalog, evaluation), {
      status: 201,
    });
  } catch (error) {
    console.error("POST /api/monitors failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}
