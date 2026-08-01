import { NextResponse } from "next/server";
import { queryInstitutions } from "@/lib/institutions-data";

/**
 * GET /api/institutions — every institution the user is enrolled at, with its
 * products nested underneath and per-institution subtotals (holdings by
 * currency + a CLP-converted net-worth total). Institutions with no products
 * are omitted (the inner joins drop them). Foreign/crypto balances are
 * converted to CLP with Buda's public tickers (see lib/rates).
 */
export async function GET() {
  return NextResponse.json(await queryInstitutions());
}
