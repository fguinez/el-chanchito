import { NextRequest, NextResponse } from "next/server";
import { eq } from "drizzle-orm";
import { db } from "@/lib/db";
import { descriptionAliases } from "@/lib/db/schema";
import { UUID_RE, failureBody, validateAliasInput } from "@/lib/aliases";
import { readJsonBody } from "@/lib/api/validation";

// Next 16: dynamic segment params arrive as a Promise on the context arg.
type Context = { params: Promise<{ id: string }> };

/** PATCH /api/aliases/[id]: partial update `{ matchDescription?, matchType?,
 *  alias?, priority? }`. Resolved live, so the change shows up everywhere at
 *  once with no backfill. */
export async function PATCH(request: NextRequest, { params }: Context) {
  try {
    const { id } = await params;
    if (!UUID_RE.test(id)) {
      return NextResponse.json({ error: "Invalid id" }, { status: 400 });
    }

    const read = await readJsonBody(request);
    if (!read.ok) return read.response;
    const body = read.body;

    const result = validateAliasInput(body, { partial: true });
    if (!result.ok) {
      return NextResponse.json(failureBody(result), { status: result.status });
    }

    const [existing] = await db
      .select({ id: descriptionAliases.id })
      .from(descriptionAliases)
      .where(eq(descriptionAliases.id, id));
    if (!existing) {
      return NextResponse.json({ error: "Not found" }, { status: 404 });
    }

    const [updated] = await db
      .update(descriptionAliases)
      .set({ ...result.value, updatedAt: new Date() })
      .where(eq(descriptionAliases.id, id))
      .returning();
    if (!updated) {
      return NextResponse.json({ error: "Not found" }, { status: 404 });
    }
    return NextResponse.json(updated);
  } catch (error) {
    console.error("PATCH /api/aliases/[id] failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}

/** DELETE /api/aliases/[id]: hard delete. Returns `{ id }`. */
export async function DELETE(_request: NextRequest, { params }: Context) {
  try {
    const { id } = await params;
    if (!UUID_RE.test(id)) {
      return NextResponse.json({ error: "Invalid id" }, { status: 400 });
    }

    const [deleted] = await db
      .delete(descriptionAliases)
      .where(eq(descriptionAliases.id, id))
      .returning({ id: descriptionAliases.id });
    if (!deleted) {
      return NextResponse.json({ error: "Not found" }, { status: 404 });
    }
    return NextResponse.json(deleted);
  } catch (error) {
    console.error("DELETE /api/aliases/[id] failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}
