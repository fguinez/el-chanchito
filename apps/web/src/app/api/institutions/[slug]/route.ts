import { NextResponse } from "next/server";
import { queryInstitutionBySlug } from "@/lib/institutions-data";

// Next 16: dynamic segment params arrive as a Promise on the context arg.
type Context = { params: Promise<{ slug: string }> };

/**
 * GET /api/institutions/[slug] — one institution by its slug (products +
 * subtotals, same shape as a list item). Returns 404 for an unknown slug.
 */
export async function GET(_request: Request, { params }: Context) {
  const { slug } = await params;
  const institution = await queryInstitutionBySlug(slug);
  if (!institution) {
    return NextResponse.json({ error: "Not found" }, { status: 404 });
  }
  return NextResponse.json({ institution });
}
