import { NextResponse, type NextRequest } from "next/server";
import { db } from "@/lib/db";
import { rowId, transactionCategoryRuleSchema } from "@/lib/api/schemas";
import { failureBody, parseJsonBody } from "@/lib/api/validation";
import { applyCategoryRuleFromTransaction } from "@/lib/category-rules-db";
import { EXACT_KEYWORD_CLASH, EXACT_KEYWORD_INDEX } from "@/lib/categories";
import { isUniqueViolation } from "@/lib/management";

// Next 16: dynamic segment params arrive as a Promise on the context arg.
type Context = { params: Promise<{ id: string }> };

/**
 * POST /api/transactions/[id]/category-rule: categorize this transaction and
 * every one with the same description, now and from now on, in one database
 * transaction (see applyCategoryRuleFromTransaction). Body `{ categoryId }`.
 *
 * 200 `{ rule, ruleCreated, updatedCount }` (`updatedCount`: rows besides this
 * one); 400 `{ error, field, issues }` on a malformed id or body, an unknown
 * category or an empty or too long description; 404 for an unknown or deleted
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
    // Only a rules-API write racing this one (it does not take the keyword
    // lock) can get here first; panel saves serialize on the lock.
    if (isUniqueViolation(error, EXACT_KEYWORD_INDEX)) {
      return NextResponse.json(failureBody(EXACT_KEYWORD_CLASH), {
        status: 400,
      });
    }
    console.error("POST /api/transactions/[id]/category-rule failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}
