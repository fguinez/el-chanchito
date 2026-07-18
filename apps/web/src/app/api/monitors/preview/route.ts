import { NextRequest, NextResponse } from "next/server";
import { getClpRates } from "@/lib/rates";
import { evaluateMonitor } from "@/lib/monitors/evaluate";
import { loadProductCatalog } from "@/lib/monitors/catalog";
import { validateMonitorInput } from "@/lib/monitors/validate";
import { enrichMonitor } from "@/lib/monitors/serialize";

/**
 * POST /api/monitors/preview — validate a create body and evaluate it now
 * WITHOUT persisting anything; the builder's live preview hits this. Returns
 * the same 400 `{ error, field?, position? }` shape as POST /api/monitors,
 * or `{ valid: true, monitor, evaluation }` with an id-less monitor shape.
 */
export async function POST(request: NextRequest) {
  try {
    let body: unknown;
    try {
      body = await request.json();
    } catch {
      return NextResponse.json({ error: "Invalid JSON body" }, { status: 400 });
    }

    const [catalog, rates] = await Promise.all([
      loadProductCatalog(),
      getClpRates(),
    ]);
    const result = validateMonitorInput(body, catalog);
    if (!result.ok) {
      return NextResponse.json(
        { error: result.error, field: result.field, position: result.position },
        { status: result.status }
      );
    }

    const evaluation = evaluateMonitor(result.value, {
      date: new Date(),
      products: catalog.byId,
      rates,
      currency: result.value.currency,
    });
    return NextResponse.json({
      valid: true,
      monitor: enrichMonitor(result.value, catalog),
      evaluation,
    });
  } catch (error) {
    console.error("POST /api/monitors/preview failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}
