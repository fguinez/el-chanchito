import { NextResponse } from "next/server";
import { db } from "@/lib/db";
import { transactions } from "@/lib/db/schema";
import { resolveProductId } from "@/lib/db/resolve";
import { withJsonBody } from "@/lib/api/validation";
import { csvRowSchema, importSchema, monthStart } from "@/lib/api/schemas";

/**
 * POST /api/import: import transactions from parsed CSV data.
 *
 * A malformed body or an unknown `kind` is a 400; a row without a
 * description, a finite amount or a YYYY-MM-DD date is skipped.
 */
export const POST = withJsonBody(importSchema, async (body) => {
  const { rows } = body;
  const kind = body.kind ?? body.accountType ?? "checking";

  const productId = await resolveProductId(body.institution || "csv_import", kind);

  let imported = 0;
  let skipped = 0;

  for (const raw of rows) {
    const parsed = csvRowSchema.safeParse(raw);
    if (!parsed.success) {
      skipped++;
      continue;
    }
    const row = parsed.data;

    // Generate external_id for dedup. The amount is the rounded one; the
    // dashboard's importer already sends whole pesos, so its keys are unchanged.
    const rawKey = `${row.date}|${row.description}|${row.amount}`;
    const externalId = `csv_${Buffer.from(rawKey).toString("base64url").slice(0, 24)}`;

    try {
      await db
        .insert(transactions)
        .values({
          productId,
          description: row.description,
          amount: row.amount,
          transactionDate: row.date,
          scheduledMonth: monthStart(row.date),
          source: "csv_import",
          externalId: externalId,
        })
        .onConflictDoNothing();

      imported++;
    } catch {
      skipped++;
    }
  }

  return NextResponse.json({ imported, skipped, total: rows.length });
});
