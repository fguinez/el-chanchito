import { NextRequest, NextResponse } from "next/server";
import { eq } from "drizzle-orm";
import { db } from "@/lib/db";
import { accounts } from "@/lib/db/schema";
import {
  isUniqueViolation,
  isUuid,
  validateAccountPatch,
} from "@/lib/management";

// Next 16: dynamic segment params arrive as a Promise on the context arg.
type Context = { params: Promise<{ id: string }> };

/**
 * PATCH /api/accounts/[id]: rename an account (the user's enrollment at an
 * institution). Resolvers pick an institution's account by display order,
 * never by name, so a rename is safe for scrapers. Returns 400 for a bad id
 * or body, 404 for an unknown account, and 409 when the institution already
 * has an account with that name.
 */
export async function PATCH(request: NextRequest, { params }: Context) {
  try {
    const { id } = await params;
    if (!isUuid(id)) {
      return NextResponse.json({ error: "Invalid id" }, { status: 400 });
    }

    let body: unknown;
    try {
      body = await request.json();
    } catch {
      return NextResponse.json({ error: "Invalid JSON body" }, { status: 400 });
    }
    const patch = validateAccountPatch(body);
    if (!patch.ok) {
      return NextResponse.json(
        { error: patch.error, field: patch.field },
        { status: patch.status }
      );
    }

    const [updated] = await db
      .update(accounts)
      .set({ ...patch.value, updatedAt: new Date() })
      .where(eq(accounts.id, id))
      .returning({ id: accounts.id, name: accounts.name });
    if (!updated) {
      return NextResponse.json({ error: "Not found" }, { status: 404 });
    }
    return NextResponse.json({ account: updated });
  } catch (error) {
    // (user_id, institution_id, name) is the table's only unique key.
    if (isUniqueViolation(error)) {
      return NextResponse.json(
        {
          error: "This institution already has an account with that name",
          field: "name",
        },
        { status: 409 }
      );
    }
    console.error("PATCH /api/accounts/[id] failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}
