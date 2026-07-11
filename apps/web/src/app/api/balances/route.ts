import { NextResponse } from "next/server";
import { db } from "@/lib/db";
import { products, accounts, institutions } from "@/lib/db/schema";
import { eq, isNotNull } from "drizzle-orm";

/** GET /api/balances — current balance per product (denormalized on products) */
export async function GET() {
  const rows = await db
    .select({
      productId: products.id,
      name: products.name,
      institution: institutions.slug,
      institutionName: institutions.name,
      kind: products.kind,
      currency: products.currency,
      balance: products.currentBalance,
      asOf: products.balanceAsOf,
    })
    .from(products)
    .innerJoin(accounts, eq(products.accountId, accounts.id))
    .innerJoin(institutions, eq(accounts.institutionId, institutions.id))
    .where(isNotNull(products.currentBalance))
    .orderBy(products.displayOrder);

  return NextResponse.json(
    rows.map((r) => ({ ...r, balance: Number(r.balance) }))
  );
}
