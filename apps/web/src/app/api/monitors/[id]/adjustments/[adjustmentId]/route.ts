import { NextRequest, NextResponse } from "next/server";
import { and, eq } from "drizzle-orm";
import { db } from "@/lib/db";
import { monitorAdjustments } from "@/lib/db/schema";
import { UUID_RE } from "@/lib/monitors/validate";

// Next 16: dynamic segment params arrive as a Promise on the context arg.
type Context = { params: Promise<{ id: string; adjustmentId: string }> };

/** The adjustment, scoped to its monitor: another monitor's id is a 404. */
function matchAdjustment(id: string, adjustmentId: string) {
  return and(
    eq(monitorAdjustments.id, adjustmentId),
    eq(monitorAdjustments.monitorId, id)
  );
}

/** DELETE /api/monitors/[id]/adjustments/[adjustmentId]: unlink the
 *  transaction (the transaction itself stays). */
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
