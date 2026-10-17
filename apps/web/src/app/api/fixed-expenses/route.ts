import { NextResponse } from "next/server";
import { db } from "@/lib/db";
import { fixedExpenses } from "@/lib/db/schema";
import { DEFAULT_SHARED_RATIO } from "@/lib/fixed-expenses";
import { eq, isNull, or, gte } from "drizzle-orm";
import { withJsonBody } from "@/lib/api/validation";
import {
  createFixedExpenseSchema,
  idSchema,
  updateFixedExpenseSchema,
} from "@/lib/api/schemas";

/** GET /api/fixed-expenses — list active fixed expenses */
export async function GET() {
  const today = new Date().toISOString().split("T")[0];

  const rows = await db
    .select()
    .from(fixedExpenses)
    .where(
      or(isNull(fixedExpenses.activeTo), gte(fixedExpenses.activeTo, today))
    );

  return NextResponse.json(rows);
}

/** POST /api/fixed-expenses — create a fixed expense */
export const POST = withJsonBody(createFixedExpenseSchema, async (body) => {
  const { name, amount, isShared, sharedRatio, activeFrom, activeTo } = body;

  const [created] = await db
    .insert(fixedExpenses)
    .values({
      name,
      amount,
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

/** PUT /api/fixed-expenses — update a fixed expense */
export const PUT = withJsonBody(updateFixedExpenseSchema, async (body) => {
  const { id, sharedRatio, ...fields } = body;

  const [updated] = await db
    .update(fixedExpenses)
    // Drizzle skips undefined keys, so absent fields stay untouched.
    .set({
      ...fields,
      sharedRatio:
        sharedRatio === undefined ? undefined : (sharedRatio?.toString() ?? null),
      updatedAt: new Date(),
    })
    .where(eq(fixedExpenses.id, id))
    .returning();

  if (!updated) {
    return NextResponse.json({ error: "Not found" }, { status: 404 });
  }

  return NextResponse.json(updated);
});

/** DELETE /api/fixed-expenses — delete a fixed expense */
export const DELETE = withJsonBody(idSchema, async ({ id }) => {
  await db.delete(fixedExpenses).where(eq(fixedExpenses.id, id));

  return NextResponse.json({ ok: true });
});
