import { NextRequest, NextResponse } from "next/server";
import { eq } from "drizzle-orm";
import { db } from "@/lib/db";
import { institutions } from "@/lib/db/schema";
import { queryInstitutionBySlug } from "@/lib/institutions-data";
import { validateInstitutionPatch } from "@/lib/management";

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

/**
 * PATCH /api/institutions/[slug]: edit an institution's `name`, `kind`,
 * `country` or `url` (see lib/management). The slug itself is what scrapers
 * resolve the institution by, so it cannot change. Returns 400 for a bad body
 * and 404 for an unknown slug.
 */
export async function PATCH(request: NextRequest, { params }: Context) {
  try {
    const { slug } = await params;

    let body: unknown;
    try {
      body = await request.json();
    } catch {
      return NextResponse.json({ error: "Invalid JSON body" }, { status: 400 });
    }
    const patch = validateInstitutionPatch(body);
    if (!patch.ok) {
      return NextResponse.json(
        { error: patch.error, field: patch.field },
        { status: patch.status }
      );
    }

    const [updated] = await db
      .update(institutions)
      .set({ ...patch.value, updatedAt: new Date() })
      .where(eq(institutions.slug, slug))
      .returning({
        id: institutions.id,
        slug: institutions.slug,
        name: institutions.name,
        kind: institutions.kind,
        country: institutions.country,
        url: institutions.url,
      });
    if (!updated) {
      return NextResponse.json({ error: "Not found" }, { status: 404 });
    }
    return NextResponse.json({ institution: updated });
  } catch (error) {
    console.error("PATCH /api/institutions/[slug] failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}
