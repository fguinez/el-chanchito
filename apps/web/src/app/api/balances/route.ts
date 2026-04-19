import { NextResponse } from "next/server";
import { db } from "@/lib/db";
import { accounts, accountBalances } from "@/lib/db/schema";
import { eq } from "drizzle-orm";

/** GET /api/balances — get latest balance per account */
export async function GET() {
  const rows = await db
    .select({
      accountId: accounts.id,
      name: accounts.name,
      institution: accounts.institution,
      accountType: accounts.accountType,
      balance: accountBalances.balance,
      asOf: accountBalances.asOf,
      source: accountBalances.source,
    })
    .from(accounts)
    .innerJoin(accountBalances, eq(accounts.id, accountBalances.accountId))
    .orderBy(accounts.displayOrder);

  return NextResponse.json(rows);
}
