import { NextResponse } from "next/server";
import { rowId } from "@/lib/api/schemas";
import { failureBody } from "@/lib/api/validation";
import { loadTransferCandidates } from "@/lib/transfers-db";

// Next 16: dynamic segment params arrive as a Promise on the context arg.
type Context = { params: Promise<{ id: string }> };

/**
 * GET /api/transfers/[id]/candidates: per side (`from`, `to`), the product,
 * whether it has a transaction feed, and the transactions the side may link
 * (with their raw `description` and resolved `displayDescription`): on that
 * product, with the right sign and exactly the transfer amount, dated within
 * 5 days of the transfer, and linked by no other transfer. The row the side
 * links today comes back with `linked: true`. 404 for an unknown transfer.
 */
export async function GET(_request: Request, { params }: Context) {
  const { id } = await params;
  if (!rowId.safeParse(id).success) {
    return NextResponse.json(
      failureBody({ error: "Invalid transfer id", field: "id" }),
      { status: 400 }
    );
  }
  try {
    const candidates = await loadTransferCandidates(id.toLowerCase());
    if (!candidates) {
      return NextResponse.json({ error: "Not found" }, { status: 404 });
    }
    return NextResponse.json(candidates);
  } catch (error) {
    console.error("GET /api/transfers/[id]/candidates failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}
