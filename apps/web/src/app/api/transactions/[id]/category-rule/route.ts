import { NextResponse, type NextRequest } from "next/server";
import { db } from "@/lib/db";
import { rowId, transactionCategoryRuleSchema } from "@/lib/api/schemas";
import { failureBody, parseJsonBody } from "@/lib/api/validation";
import { applyCategoryRuleFromTransaction } from "@/lib/category-rules-db";

// Next 16: dynamic segment params arrive as a Promise on the context arg.
type Context = { params: Promise<{ id: string }> };

/**
 * POST /api/transactions/[id]/category-rule: categorize this transaction and
 * every one with the same description, now and from now on. Body
 * `{ categoryId }`. In one database transaction: the row takes the category
 * (marked manual, as PATCH does); a category rule keyed on its trimmed raw
 * description is created at priority 1000 (or the existing rule with that
 * keyword is pointed at the category); and the other visible rows with the same
 * raw description (trimmed, case-insensitive) that were not categorized by hand
 * take the category too. Future rows get it from the insert trigger (V021).
 *
 * 200 `{ rule, ruleCreated, updatedCount }` (`updatedCount`: rows besides this
 * one); 400 `{ error, field, issues }` on a malformed id or body, an unknown
 * category or a description too long for a rule; 404 for an unknown or deleted
 * transaction.
 */
export async function POST(request: NextRequest, { params }: Context) {
  const { id: rawId } = await params;
  if (!rowId.safeParse(rawId).success) {
    return NextResponse.json(
      failureBody({ error: "Invalid transaction id", field: "id" }),
      { status: 400 }
    );
  }
  const id = rawId.toLowerCase();

  const parsed = await parseJsonBody(request, transactionCategoryRuleSchema);
  if (!parsed.ok) return parsed.response;

  try {
    const outcome = await db.transaction((tx) =>
      applyCategoryRuleFromTransaction(tx, id, parsed.data.categoryId)
    );
    if (!outcome.ok) {
      if (outcome.status === 404) {
        return NextResponse.json(
          { error: "Transaction not found" },
          { status: 404 }
        );
      }
      return NextResponse.json(failureBody(outcome), { status: 400 });
    }
    return NextResponse.json({
      rule: outcome.rule,
      ruleCreated: outcome.ruleCreated,
      updatedCount: outcome.updatedCount,
    });
  } catch (error) {
    console.error("POST /api/transactions/[id]/category-rule failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}
