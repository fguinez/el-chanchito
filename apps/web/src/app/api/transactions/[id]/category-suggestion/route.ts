import { NextResponse, type NextRequest } from "next/server";
import { rowId } from "@/lib/api/schemas";
import { failureBody } from "@/lib/api/validation";
import { suggestionForTransaction } from "@/lib/category-suggestions-db";

// Next 16: dynamic segment params arrive as a Promise on the context arg.
type Context = { params: Promise<{ id: string }> };

/**
 * GET /api/transactions/[id]/category-suggestion: the category suggested for
 * this transaction, as `{ suggestion }` (`categoryId`, `categoryName`,
 * `confidence`, `matchCount`, `exact`, `reason`), or `{ suggestion: null }`
 * when it has a category, its category was cleared by hand, the suggestion
 * was dismissed on the Categorías page, or nothing is confident enough. 400
 * for a malformed id, 404 for an unknown or deleted transaction.
 */
export async function GET(_request: NextRequest, { params }: Context) {
  const { id: rawId } = await params;
  if (!rowId.safeParse(rawId).success) {
    return NextResponse.json(
      failureBody({ error: "Invalid transaction id", field: "id" }),
      { status: 400 }
    );
  }
  try {
    const outcome = await suggestionForTransaction(rawId.toLowerCase());
    if (!outcome.ok) {
      return NextResponse.json(
        { error: "Transaction not found" },
        { status: 404 }
      );
    }
    return NextResponse.json({ suggestion: outcome.suggestion });
  } catch (error) {
    console.error("GET /api/transactions/[id]/category-suggestion failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}
