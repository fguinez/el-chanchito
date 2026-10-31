import { NextResponse } from "next/server";
import { db } from "@/lib/db";
import { budgetSettings } from "@/lib/db/schema";
import { withJsonBody } from "@/lib/api/validation";
import { updateBudgetSettingsSchema } from "@/lib/api/schemas";

/** GET /api/budget-settings: `{ monthlyIncome }`, 0 until one is saved. */
export async function GET() {
  const [row] = await db.select().from(budgetSettings).limit(1);

  return NextResponse.json({ monthlyIncome: row?.monthlyIncome ?? 0 });
}

/** PUT /api/budget-settings: save the monthly income (upserts the single
 *  row) and answer `{ monthlyIncome }`. */
export const PUT = withJsonBody(updateBudgetSettingsSchema, async (body) => {
  const values = { monthlyIncome: body.monthlyIncome, updatedAt: new Date() };
  const [saved] = await db
    .insert(budgetSettings)
    .values({ id: true, ...values })
    .onConflictDoUpdate({ target: budgetSettings.id, set: values })
    .returning();

  return NextResponse.json({ monthlyIncome: saved.monthlyIncome });
});
