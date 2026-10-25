import { NextResponse } from "next/server";
import { categorySuggestionSchema } from "@/lib/api/schemas";
import { failureBody, withJsonBody } from "@/lib/api/validation";
import { dismissCategorySuggestion } from "@/lib/category-suggestions-db";

/**
 * POST /api/categories/suggestions/dismiss: stop suggesting `categoryId` for
 * descriptions that normalise like `description` (V042). Body
 * `{ description, categoryId }`. A different suggested category still shows.
 * Idempotent: 200 `{ ok: true }` again for a suggestion already dismissed.
 * 400 `{ error, field, issues }` for a malformed body, a description with no
 * words left after normalising, or an unknown category.
 */
export const POST = withJsonBody(categorySuggestionSchema, async (input) => {
  try {
    const outcome = await dismissCategorySuggestion(input);
    if (!outcome.ok) {
      return NextResponse.json(failureBody(outcome), { status: outcome.status });
    }
    return NextResponse.json({ ok: true });
  } catch (error) {
    console.error("POST /api/categories/suggestions/dismiss failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
});
