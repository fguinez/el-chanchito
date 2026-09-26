import { NextRequest, NextResponse } from "next/server";
import { eq } from "drizzle-orm";
import { db } from "@/lib/db";
import { monitorAdjustments, monitors } from "@/lib/db/schema";
import { loadMonitorAdjustments } from "@/lib/monitors/catalog";
import { UUID_RE } from "@/lib/monitors/validate";
import {
  parseMonthParam,
  toApiAdjustment,
  validateAdjustmentInput,
} from "@/lib/monitors/adjustments";

// Next 16: dynamic segment params arrive as a Promise on the context arg.
type Context = { params: Promise<{ id: string }> };

async function monitorExists(id: string): Promise<boolean> {
  const [row] = await db
    .select({ id: monitors.id })
    .from(monitors)
    .where(eq(monitors.id, id));
  return row != null;
}

/**
 * GET /api/monitors/[id]/adjustments: the monitor's adjustments
 * ("variaciones"), oldest day first; `?month=YYYY-MM` keeps one month.
 */
export async function GET(request: NextRequest, { params }: Context) {
  try {
    const { id } = await params;
    if (!UUID_RE.test(id)) {
      return NextResponse.json({ error: "Invalid id" }, { status: 400 });
    }
    const month = parseMonthParam(request.nextUrl.searchParams.get("month"));
    if (!month.ok) {
      return NextResponse.json(
        { error: month.error, field: month.field },
        { status: month.status }
      );
    }
    if (!(await monitorExists(id))) {
      return NextResponse.json({ error: "Not found" }, { status: 404 });
    }

    const adjustments = await loadMonitorAdjustments(id);
    const selected = month.value;
    return NextResponse.json({
      adjustments:
        selected == null
          ? adjustments
          : adjustments.filter((a) => a.adjustmentDate.startsWith(`${selected}-`)),
    });
  } catch (error) {
    console.error("GET /api/monitors/[id]/adjustments failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}

/** POST /api/monitors/[id]/adjustments: create an adjustment from
 *  `{ adjustmentDate, amount, description? }`; 404 if the monitor is gone. */
export async function POST(request: NextRequest, { params }: Context) {
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
    const result = validateAdjustmentInput(body);
    if (!result.ok) {
      return NextResponse.json(
        { error: result.error, field: result.field },
        { status: result.status }
      );
    }
    if (!(await monitorExists(id))) {
      return NextResponse.json({ error: "Not found" }, { status: 404 });
    }

    const [created] = await db
      .insert(monitorAdjustments)
      .values({
        monitorId: id,
        adjustmentDate: result.value.adjustmentDate,
        amount: String(result.value.amount),
        description: result.value.description,
      })
      .returning();
    return NextResponse.json(toApiAdjustment(created), { status: 201 });
  } catch (error) {
    console.error("POST /api/monitors/[id]/adjustments failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}
