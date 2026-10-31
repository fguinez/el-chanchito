import { NextResponse } from "next/server";
import { db } from "@/lib/db";
import { fixedExpenses } from "@/lib/db/schema";
import { asc, eq } from "drizzle-orm";
import { failureBody, withJsonBody } from "@/lib/api/validation";
import {
  createFixedExpenseSchema,
  idSchema,
  updateFixedExpenseSchema,
} from "@/lib/api/schemas";
import {
  DEFAULT_SHARED_RATIO,
  mergeFixedExpenseUpdate,
  type FixedExpenseFields,
} from "@/lib/fixed-expenses";

/** GET /api/fixed-expenses: every fixed expense by name, including ended and
 *  scheduled ones; the page decides which are active on the local day. */
export async function GET() {
  const rows = await db
    .select()
    .from(fixedExpenses)
    .orderBy(asc(fixedExpenses.name));

  return NextResponse.json(rows);
}

/** POST /api/fixed-expenses: create a fixed expense */
export const POST = withJsonBody(createFixedExpenseSchema, async (body) => {
  const { name, amount, kind, isShared, sharedRatio, activeFrom, activeTo } =
    body;

  const [created] = await db
    .insert(fixedExpenses)
    .values({
      name,
      amount,
      kind: kind ?? "fixed",
      isShared: isShared ?? false,
      sharedRatio: isShared
        ? (sharedRatio?.toString() ?? DEFAULT_SHARED_RATIO)
        : null,
      activeFrom: activeFrom ?? null,
      activeTo: activeTo ?? null,
    })
    .returning();

  return NextResponse.json(created, { status: 201 });
});

/** PUT /api/fixed-expenses: update a fixed expense, checked against the
 *  stored row (see mergeFixedExpenseUpdate) */
export const PUT = withJsonBody(updateFixedExpenseSchema, async (body) => {
  const { id, sharedRatio, ...rest } = body;
  const fields: FixedExpenseFields = { ...rest };
  if (sharedRatio !== undefined) {
    fields.sharedRatio = sharedRatio?.toString() ?? null;
  }

  const [existing] = await db
    .select()
    .from(fixedExpenses)
    .where(eq(fixedExpenses.id, id));
  if (!existing) {
    return NextResponse.json({ error: "Not found" }, { status: 404 });
  }

  const merged = mergeFixedExpenseUpdate(existing, fields);
  if (!merged.ok) {
    return NextResponse.json(failureBody(merged), { status: 400 });
  }

  const [updated] = await db
    .update(fixedExpenses)
    .set({ ...merged.value, updatedAt: new Date() })
    .where(eq(fixedExpenses.id, id))
    .returning();

  if (!updated) {
    return NextResponse.json({ error: "Not found" }, { status: 404 });
  }

  return NextResponse.json(updated);
});

/** DELETE /api/fixed-expenses: delete a fixed expense */
export const DELETE = withJsonBody(idSchema, async ({ id }) => {
  await db.delete(fixedExpenses).where(eq(fixedExpenses.id, id));

  return NextResponse.json({ ok: true });
});
