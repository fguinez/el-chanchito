import { NextResponse } from "next/server";
import { loadTransferSuggestions } from "@/lib/transfer-suggestions-db";

/**
 * GET /api/transfers/suggestions: rows that look like internal transfers,
 * highest score first, as `{ suggestions }`. Each one has an `id`, a `kind`
 * (`pair`: a debit and a credit of exactly the same amount on two CLP
 * products within 3 days; `one_sided`: a debit naming a CLP product with no
 * transaction feed), its `source` and `target` legs (`transactionId`,
 * `productId`, `productName`, `institutionName`, `date`, `description`,
 * `amount`, `currency`; a one-sided target has nulls for the row fields), the
 * positive `amount`, a `score` and Spanish `evidence`. Only unlinked,
 * unflagged, undismissed rows of the last 90 days count. Nothing is linked
 * until POST /api/transfers/suggestions/confirm.
 */
export async function GET() {
  try {
    return NextResponse.json({ suggestions: await loadTransferSuggestions() });
  } catch (error) {
    console.error("GET /api/transfers/suggestions failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}
