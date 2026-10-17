import { NextResponse, type NextRequest } from "next/server";
import { eq } from "drizzle-orm";
import { db } from "@/lib/db";
import { internalTransfers } from "@/lib/db/schema";
import { transferLinksSchema, rowId } from "@/lib/api/schemas";
import { failureBody, parseJsonBody } from "@/lib/api/validation";
import {
  applyTransferLinks,
  linkFieldOfViolation,
  loadTransfers,
} from "@/lib/transfers-db";

// Next 16: dynamic segment params arrive as a Promise on the context arg.
type Context = { params: Promise<{ id: string }> };

/**
 * PUT /api/transfers/[id]/links: set or clear the transactions a transfer
 * links. Body `{ fromTransactionId?, toTransactionId? }`: a row id links it,
 * null clears the side, an omitted key leaves it as it is. The source must be
 * an outgoing row (amount < 0) on the transfer's source product and the target
 * an incoming row (amount > 0) on its target product, each moving exactly the
 * transfer amount, and neither may be linked by another transfer. Linked rows
 * get `is_internal_transfer`; released rows lose it. A pending transfer turns
 * resolved once every side whose product has a transaction feed is linked.
 *
 * 200 with the transfer as GET /api/transfers lists it; 400
 * `{ error, field, issues }` on a failed check; 404 for an unknown transfer.
 */
export async function PUT(request: NextRequest, { params }: Context) {
  const { id: rawId } = await params;
  if (!rowId.safeParse(rawId).success) {
    return NextResponse.json(
      failureBody({ error: "Invalid transfer id", field: "id" }),
      { status: 400 }
    );
  }
  const id = rawId.toLowerCase();

  const parsed = await parseJsonBody(request, transferLinksSchema);
  if (!parsed.ok) return parsed.response;

  try {
    const outcome = await db.transaction((tx) =>
      applyTransferLinks(tx, id, parsed.data)
    );
    if (!outcome.ok) {
      if (outcome.status === 404) {
        return NextResponse.json({ error: "Not found" }, { status: 404 });
      }
      return NextResponse.json(failureBody(outcome), { status: 400 });
    }
    const [transfer] = await loadTransfers(eq(internalTransfers.id, id));
    return NextResponse.json(transfer ?? null);
  } catch (error) {
    // A concurrent link of the same row loses the race on the UNIQUE index.
    const field = linkFieldOfViolation(error);
    if (field) {
      return NextResponse.json(
        failureBody({
          error: "La transacción ya está vinculada a otro movimiento",
          field,
        }),
        { status: 400 }
      );
    }
    console.error("PUT /api/transfers/[id]/links failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}
