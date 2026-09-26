import { NextRequest, NextResponse } from "next/server";
import { getClpRates } from "@/lib/rates";
import { evaluateMonitor } from "@/lib/monitors/evaluate";
import {
  loadMonitorAdjustments,
  loadProductCatalog,
} from "@/lib/monitors/catalog";
import { UUID_RE, validateMonitorInput } from "@/lib/monitors/validate";
import { enrichMonitor } from "@/lib/monitors/serialize";

/**
 * POST /api/monitors/preview: validate a create body and evaluate it now
 * WITHOUT persisting anything; the builder's live preview hits this. An
 * optional `monitorId` (the monitor being edited) applies that monitor's
 * stored adjustments. Returns the same 400 `{ error, field?, position? }`
 * shape as POST /api/monitors, or `{ valid: true, monitor, evaluation }`
 * with an id-less monitor shape.
 */
export async function POST(request: NextRequest) {
  try {
    let body: unknown;
    try {
      body = await request.json();
    } catch {
      return NextResponse.json({ error: "Invalid JSON body" }, { status: 400 });
    }

    // null or absent: a new monitor. A well-formed id with no stored
    // monitor simply has no adjustments.
    const rawMonitorId =
      typeof body === "object" && body !== null && "monitorId" in body
        ? body.monitorId
        : undefined;
    const monitorId = rawMonitorId ?? undefined;
    if (
      monitorId !== undefined &&
      (typeof monitorId !== "string" || !UUID_RE.test(monitorId))
    ) {
      return NextResponse.json(
        { error: "Field 'monitorId' must be a monitor id", field: "monitorId" },
        { status: 400 }
      );
    }

    const [catalog, rates, adjustments] = await Promise.all([
      loadProductCatalog(),
      getClpRates(),
      monitorId !== undefined ? loadMonitorAdjustments(monitorId) : [],
    ]);
    const result = validateMonitorInput(body, catalog);
    if (!result.ok) {
      return NextResponse.json(
        { error: result.error, field: result.field, position: result.position },
        { status: result.status }
      );
    }

    const evaluation = evaluateMonitor({ ...result.value, adjustments }, {
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
