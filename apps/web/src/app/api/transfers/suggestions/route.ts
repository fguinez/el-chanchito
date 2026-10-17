import { NextResponse, type NextRequest } from "next/server";
import { suggestionsQuerySchema } from "@/lib/api/schemas";
import { parseSearchParams } from "@/lib/api/validation";
import { suggestionsForTransaction } from "@/lib/transfer-suggestions";
import { autoLinkSuggestions } from "@/lib/transfer-suggestions-db";

/**
 * GET /api/transfers/suggestions: rows that look like internal transfers,
 * highest score first, as `{ suggestions, autoLinked }`. Each one has an `id`, a `kind`
 * (`pair`: a debit and a credit of exactly the same amount on two CLP
 * products within 3 days; `one_sided`: a debit naming a CLP product with no
 * transaction feed), its `source` and `target` legs (`transactionId`,
 * `productId`, `productName`, `productSlug`, `institutionName`,
 * `institutionSlug`, `date`, `description`, `amount`, `currency`; a one-sided
 * target has nulls for the row fields), the positive `amount`, a `score` and
 * Spanish `evidence`. Only unlinked, unflagged, undismissed rows of the last
 * 90 days count. Before answering, every pair scoring at least 85 that
 * shares no row with another suggestion is confirmed on its own (an
 * automatic transfer, undone by deleting it); `autoLinked` lists the ids of
 * the transfers this request created, and those suggestions are left out.
 * The rest wait for POST /api/transfers/suggestions/confirm.
 *
 * `?transactionId=<uuid>` keeps only the suggestions that use that row on
 * either side (the transaction detail panel's hint); auto-linking still
 * covers every suggestion.
 */
export async function GET(request: NextRequest) {
  const query = parseSearchParams(
    request.nextUrl.searchParams,
    suggestionsQuerySchema
  );
  if (!query.ok) return query.response;
  try {
    const { suggestions: all, autoLinked } = await autoLinkSuggestions();
    const { transactionId } = query.data;
    const suggestions = transactionId
      ? suggestionsForTransaction(all, transactionId)
      : all;
    return NextResponse.json({ suggestions, autoLinked });
  } catch (error) {
    console.error("GET /api/transfers/suggestions failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}
