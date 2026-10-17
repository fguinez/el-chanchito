import { NextResponse } from "next/server";
import { queryInstitutions } from "@/lib/institutions-data";

/**
 * GET /api/institutions — every institution the user is enrolled at, with its
 * products nested underneath and per-institution subtotals (holdings by
 * currency + a CLP-converted net-worth total). Institutions with no products
 * are omitted (the inner joins drop them). Foreign/crypto balances are
 * converted to CLP per product: the institution's own rate when its scraper
 * stored one, else the provider rates (see lib/rates). Each non-CLP product
 * carries the rate used as `exchangeRate`.
 */
export async function GET() {
  return NextResponse.json(await queryInstitutions());
}
