import { NextRequest, NextResponse } from "next/server";
import { db } from "@/lib/db";
import { transactions, type ProductKind } from "@/lib/db/schema";
import { resolveProductId } from "@/lib/db/resolve";

interface CsvRow {
  description: string;
  amount: number;
  date: string;  // YYYY-MM-DD
}

/** POST /api/import — import transactions from parsed CSV data */
export async function POST(request: NextRequest) {
  const body = await request.json();
  const { rows, institution } = body as {
    rows: CsvRow[];
    institution: string;
    kind?: ProductKind;
    accountType?: string; // legacy alias for kind
  };
  const kind = (body.kind ?? body.accountType ?? "checking") as ProductKind;

  if (!rows || !Array.isArray(rows) || rows.length === 0) {
    return NextResponse.json(
      { error: "No rows provided" },
      { status: 400 }
    );
  }

  const productId = await resolveProductId(institution || "csv_import", kind);

  let imported = 0;
  let skipped = 0;

  for (const row of rows) {
    if (!row.description || row.amount === undefined || !row.date) {
      skipped++;
      continue;
    }

    const txDate = new Date(row.date);
    if (isNaN(txDate.getTime())) {
      skipped++;
      continue;
    }

    const dateStr = row.date;
    const scheduledMonth = `${txDate.getFullYear()}-${String(txDate.getMonth() + 1).padStart(2, "0")}-01`;

    // Generate external_id for dedup
    const rawKey = `${dateStr}|${row.description}|${row.amount}`;
    const externalId = `csv_${Buffer.from(rawKey).toString("base64url").slice(0, 24)}`;

    try {
      await db
        .insert(transactions)
        .values({
          productId,
          description: row.description,
          amount: Math.round(row.amount),
          transactionDate: dateStr,
          scheduledMonth,
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
}
