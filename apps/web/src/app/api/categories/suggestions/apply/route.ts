import { NextResponse } from "next/server";
import { categorySuggestionSchema } from "@/lib/api/schemas";
import { failureBody, withJsonBody } from "@/lib/api/validation";
import { applySuggestionOnce } from "@/lib/category-suggestions-db";

/**
 * POST /api/categories/suggestions/apply: apply a suggestion once. Body
 * `{ description, categoryId }`. Every visible uncategorized row whose raw
 * description equals `description` (trimmed, case-insensitive) takes the
 * category, marked manual; rows with a category, or whose category was
 * cleared by hand, are left alone, and no rule is created. 200
 * `{ updatedCount }`; 400 `{ error, field, issues }` for a malformed body or
 * an unknown category.
 */
export const POST = withJsonBody(categorySuggestionSchema, async (input) => {
  try {
    const outcome = await applySuggestionOnce(input);
    if (!outcome.ok) {
      return NextResponse.json(failureBody(outcome), { status: outcome.status });
    }
    return NextResponse.json({ updatedCount: outcome.updatedCount });
  } catch (error) {
    console.error("POST /api/categories/suggestions/apply failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
});
