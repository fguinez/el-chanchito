import { NextResponse } from "next/server";
import { dismissSuggestionSchema } from "@/lib/api/schemas";
import { failureBody, withJsonBody } from "@/lib/api/validation";
import { dismissTransferSuggestion } from "@/lib/transfer-suggestions-db";

/**
 * POST /api/transfers/suggestions/dismiss: stop suggesting a pair
 * (`{ sourceTransactionId, targetTransactionId }`) or the one-sided
 * suggestions for a row (`{ sourceTransactionId }`). Idempotent: 200
 * `{ ok: true }` again for a suggestion already dismissed. 400
 * `{ error, field, issues }` for a malformed body or an unknown row.
 */
export const POST = withJsonBody(dismissSuggestionSchema, async (input) => {
  try {
    const outcome = await dismissTransferSuggestion(input);
    if (!outcome.ok) {
      return NextResponse.json(failureBody(outcome), { status: outcome.status });
    }
    return NextResponse.json({ ok: true });
  } catch (error) {
    console.error("POST /api/transfers/suggestions/dismiss failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
});
