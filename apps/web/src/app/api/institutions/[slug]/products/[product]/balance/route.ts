import { NextRequest, NextResponse } from "next/server";
import { db } from "@/lib/db";
import {
  institutions,
  accounts,
  products,
  productSnapshots,
} from "@/lib/db/schema";
import { and, eq } from "drizzle-orm";
import { isRetiredGhost } from "@/lib/retired-products";
import {
  acceptsManualBalance,
  manualBalanceMetrics,
  parseManualBalance,
} from "@/lib/manual-balance";

// Next 16: dynamic segment params arrive as a Promise on the context arg.
type Context = { params: Promise<{ slug: string; product: string }> };

/**
 * POST /api/institutions/[slug]/products/[product]/balance: record a balance
 * typed by the user, for kinds with no automatic balance source (see
 * lib/manual-balance). Body `{ balance }` in whole CLP. Always appends a
 * `product_snapshots` row with source 'manual' (an explicit confirmation is a
 * history point even when unchanged) and refreshes the product's latest
 * balance/metrics in the same transaction. A later scraper observation that
 * differs replaces it as the latest value, since the writer compares against
 * products.metrics. Returns 201 `{ asOf, balance, source }`; 400 on a bad
 * body or a kind that doesn't accept manual balances, 404 when the product is
 * unknown, 409 when it is inactive.
 */
export async function POST(request: NextRequest, { params }: Context) {
  try {
    const { slug, product } = await params;

    let body: unknown;
    try {
      body = await request.json();
    } catch {
      return NextResponse.json({ error: "Invalid JSON body" }, { status: 400 });
    }
    const parsed = parseManualBalance(body);
    if (!parsed.ok) {
      return NextResponse.json({ error: parsed.error }, { status: 400 });
    }

    const [row] = await db
      .select({
        productId: products.id,
        kind: products.kind,
        currentBalance: products.currentBalance,
        isActive: products.isActive,
      })
      .from(products)
      .innerJoin(accounts, eq(products.accountId, accounts.id))
      .innerJoin(institutions, eq(accounts.institutionId, institutions.id))
      .where(and(eq(institutions.slug, slug), eq(products.slug, product)))
      .limit(1);

    // Same visibility rule as the sibling GET: a retired ghost doesn't exist.
    if (!row || isRetiredGhost(row)) {
      return NextResponse.json({ error: "Not found" }, { status: 404 });
    }
    if (!row.isActive) {
      return NextResponse.json(
        { error: "El producto está inactivo" },
        { status: 409 }
      );
    }
    if (!acceptsManualBalance(row.kind)) {
      return NextResponse.json(
        { error: "Este tipo de producto no admite saldo manual" },
        { status: 400 }
      );
    }

    const { balance } = parsed;
    const metrics = manualBalanceMetrics(row.kind, balance);
    // One timestamp so the snapshot and the product's balance_as_of agree.
    const asOf = new Date();

    await db.transaction(async (tx) => {
      await tx.insert(productSnapshots).values({
        productId: row.productId,
        balance: String(balance),
        metrics,
        asOf,
        source: "manual",
      });
      await tx
        .update(products)
        .set({
          currentBalance: String(balance),
          metrics,
          balanceAsOf: asOf,
          updatedAt: asOf,
        })
        .where(eq(products.id, row.productId));
    });

    return NextResponse.json(
      { asOf: asOf.toISOString(), balance, source: "manual" },
      { status: 201 }
    );
  } catch (error) {
    console.error(
      "POST /api/institutions/[slug]/products/[product]/balance failed:",
      error
    );
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}
