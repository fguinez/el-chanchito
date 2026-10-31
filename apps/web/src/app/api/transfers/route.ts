import { NextResponse } from "next/server";
import { db } from "@/lib/db";
import { internalTransfers, products } from "@/lib/db/schema";
import { eq, inArray } from "drizzle-orm";
import { failureBody, withJsonBody } from "@/lib/api/validation";
import {
  createTransferSchema,
  idSchema,
  updateTransferSchema,
} from "@/lib/api/schemas";
import { checkTransferEndpoints } from "@/lib/transfers";
import { deleteTransfer, loadTransfers } from "@/lib/transfers-db";

/**
 * GET /api/transfers: every internal transfer, newest first, with its source
 * and target product (`fromProduct`/`toProduct`, null on legacy rows), the
 * transactions it links (`fromTransaction`/`toTransaction`: id, date, raw
 * `description` and resolved `displayDescription`, amount, currency; null when
 * unlinked) and whether each product has a transaction feed
 * (`fromHasFeed`/`toHasFeed`).
 */
export async function GET() {
  return NextResponse.json(await loadTransfers());
}

/**
 * POST /api/transfers: create an internal transfer between two distinct CLP
 * products. Both `fromProductId` and `toProductId` are required; only legacy
 * rows lack them.
 */
export const POST = withJsonBody(createTransferSchema, async (input) => {
  const found = await db
    .select({ id: products.id, currency: products.currency })
    .from(products)
    .where(inArray(products.id, [input.fromProductId, input.toProductId]));
  const endpoints = checkTransferEndpoints(input, found);
  if (!endpoints.ok) {
    return NextResponse.json(failureBody(endpoints), {
      status: endpoints.status,
    });
  }

  const [created] = await db.insert(internalTransfers).values(input).returning();

  return NextResponse.json(created, { status: 201 });
});

/** PUT /api/transfers: update a transfer's status and/or notes. Links to its
 *  transactions go through PUT /api/transfers/[id]/links. */
export const PUT = withJsonBody(updateTransferSchema, async (body) => {
  const { id, status, notes } = body;

  const [updated] = await db
    .update(internalTransfers)
    // Drizzle skips undefined keys, so an absent status or notes stays as is.
    .set({ status, notes, updatedAt: new Date() })
    .where(eq(internalTransfers.id, id))
    .returning();

  if (!updated) {
    return NextResponse.json({ error: "Not found" }, { status: 404 });
  }

  return NextResponse.json(updated);
});

/** DELETE /api/transfers: delete a transfer. The rows it linked lose
 *  `is_internal_transfer` (unless another transfer links them) and, when it
 *  linked both sides, that pair is dismissed from the suggestions, in the
 *  same db transaction. */
export const DELETE = withJsonBody(idSchema, async ({ id }) => {
  await deleteTransfer(id);
  return NextResponse.json({ ok: true });
});
