import { NextRequest, NextResponse } from "next/server";
import { and, eq } from "drizzle-orm";
import { db } from "@/lib/db";
import { monitorAdjustments } from "@/lib/db/schema";
import { UUID_RE } from "@/lib/monitors/validate";
import {
  toApiAdjustment,
  validateAdjustmentInput,
} from "@/lib/monitors/adjustments";

// Next 16: dynamic segment params arrive as a Promise on the context arg.
type Context = { params: Promise<{ id: string; adjustmentId: string }> };

/** The adjustment, scoped to its monitor: another monitor's id is a 404. */
function matchAdjustment(id: string, adjustmentId: string) {
  return and(
    eq(monitorAdjustments.id, adjustmentId),
    eq(monitorAdjustments.monitorId, id)
  );
}

/** PATCH /api/monitors/[id]/adjustments/[adjustmentId]: partial update of
 *  adjustmentDate, amount and/or description. Bumps updatedAt. */
export async function PATCH(request: NextRequest, { params }: Context) {
  try {
    const { id, adjustmentId } = await params;
    if (!UUID_RE.test(id) || !UUID_RE.test(adjustmentId)) {
      return NextResponse.json({ error: "Invalid id" }, { status: 400 });
    }

    let body: unknown;
    try {
      body = await request.json();
    } catch {
      return NextResponse.json({ error: "Invalid JSON body" }, { status: 400 });
    }
    const result = validateAdjustmentInput(body, { partial: true });
    if (!result.ok) {
      return NextResponse.json(
        { error: result.error, field: result.field },
        { status: result.status }
      );
    }

    const { amount, ...rest } = result.value;
    const [updated] = await db
      .update(monitorAdjustments)
      .set({
        ...rest,
        ...(amount !== undefined && { amount: String(amount) }),
        updatedAt: new Date(),
      })
      .where(matchAdjustment(id, adjustmentId))
      .returning();
    if (!updated) {
      return NextResponse.json({ error: "Not found" }, { status: 404 });
    }
    return NextResponse.json(toApiAdjustment(updated));
  } catch (error) {
    console.error(
      "PATCH /api/monitors/[id]/adjustments/[adjustmentId] failed:",
      error
    );
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}

/** DELETE /api/monitors/[id]/adjustments/[adjustmentId]: hard delete. */
export async function DELETE(request: NextRequest, { params }: Context) {
  try {
    const { id, adjustmentId } = await params;
    if (!UUID_RE.test(id) || !UUID_RE.test(adjustmentId)) {
      return NextResponse.json({ error: "Invalid id" }, { status: 400 });
    }

    const deleted = await db
      .delete(monitorAdjustments)
      .where(matchAdjustment(id, adjustmentId))
      .returning({ id: monitorAdjustments.id });
    if (deleted.length === 0) {
      return NextResponse.json({ error: "Not found" }, { status: 404 });
    }
    return NextResponse.json({ ok: true });
  } catch (error) {
    console.error(
      "DELETE /api/monitors/[id]/adjustments/[adjustmentId] failed:",
      error
    );
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}
