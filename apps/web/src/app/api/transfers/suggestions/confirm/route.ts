import { NextResponse } from "next/server";
import { eq } from "drizzle-orm";
import { internalTransfers } from "@/lib/db/schema";
import { confirmSuggestionSchema } from "@/lib/api/schemas";
import { failureBody, withJsonBody } from "@/lib/api/validation";
import { confirmTransferSuggestion } from "@/lib/transfer-suggestions-db";
import { loadTransfers } from "@/lib/transfers-db";

/**
 * POST /api/transfers/suggestions/confirm: turn a suggestion into a transfer.
 * Body `{ sourceTransactionId, targetTransactionId }` for a pair or
 * `{ sourceTransactionId, targetProductId }` for a one-sided suggestion. In
 * one db transaction it re-checks the rows (an outgoing source, an incoming
 * target of exactly the same amount on another product, both CLP; a one-sided
 * target product without a transaction feed), creates the transfer with the
 * source row's date and description, links and flags the rows as
 * PUT /api/transfers/[id]/links does (so it turns `resolved`) and saves the
 * pattern that ranks similar rows first later.
 *
 * 201 with the transfer as GET /api/transfers lists it; 400
 * `{ error, field, issues }` on a failed check; 409 when a row is already
 * linked to a transfer.
 */
export const POST = withJsonBody(confirmSuggestionSchema, async (input) => {
  try {
    const outcome = await confirmTransferSuggestion(input);
    if (!outcome.ok) {
      return NextResponse.json(failureBody(outcome), { status: outcome.status });
    }
    const [transfer] = await loadTransfers(
      eq(internalTransfers.id, outcome.transferId)
    );
    return NextResponse.json(transfer ?? null, { status: 201 });
  } catch (error) {
    console.error("POST /api/transfers/suggestions/confirm failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
});
