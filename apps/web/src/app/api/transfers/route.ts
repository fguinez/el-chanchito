import { NextRequest, NextResponse } from "next/server";
import { db } from "@/lib/db";
import {
  accounts,
  institutions,
  internalTransfers,
  products,
} from "@/lib/db/schema";
import { alias } from "drizzle-orm/pg-core";
import { eq, desc, inArray } from "drizzle-orm";
import {
  checkTransferEndpoints,
  parseNewTransfer,
  type TransferProductRef,
} from "@/lib/transfers";

const fromProduct = alias(products, "from_product");
const fromAccount = alias(accounts, "from_account");
const fromInstitution = alias(institutions, "from_institution");
const toProduct = alias(products, "to_product");
const toAccount = alias(accounts, "to_account");
const toInstitution = alias(institutions, "to_institution");

type JoinedEndpoint = {
  [K in keyof TransferProductRef]: TransferProductRef[K] | null;
};

/** The left joins leave every column null on a legacy null endpoint; the
 *  chain's FKs are NOT NULL, so a product id means the rest is there too. */
function productRef(row: JoinedEndpoint): TransferProductRef | null {
  return row.id === null ? null : (row as TransferProductRef);
}

/**
 * GET /api/transfers: every internal transfer, newest first, with its source
 * and target product (`fromProduct`/`toProduct`, null on legacy rows).
 */
export async function GET() {
  const rows = await db
    .select({
      transfer: internalTransfers,
      from: {
        id: fromProduct.id,
        name: fromProduct.name,
        slug: fromProduct.slug,
        institutionSlug: fromInstitution.slug,
        institutionName: fromInstitution.name,
      },
      to: {
        id: toProduct.id,
        name: toProduct.name,
        slug: toProduct.slug,
        institutionSlug: toInstitution.slug,
        institutionName: toInstitution.name,
      },
    })
    .from(internalTransfers)
    .leftJoin(fromProduct, eq(internalTransfers.fromProductId, fromProduct.id))
    .leftJoin(fromAccount, eq(fromProduct.accountId, fromAccount.id))
    .leftJoin(
      fromInstitution,
      eq(fromAccount.institutionId, fromInstitution.id)
    )
    .leftJoin(toProduct, eq(internalTransfers.toProductId, toProduct.id))
    .leftJoin(toAccount, eq(toProduct.accountId, toAccount.id))
    .leftJoin(toInstitution, eq(toAccount.institutionId, toInstitution.id))
    .orderBy(
      desc(internalTransfers.transferDate),
      desc(internalTransfers.createdAt)
    );

  return NextResponse.json(
    rows.map((r) => ({
      ...r.transfer,
      fromProduct: productRef(r.from),
      toProduct: productRef(r.to),
    }))
  );
}

/**
 * POST /api/transfers: create an internal transfer between two distinct CLP
 * products. Both `fromProductId` and `toProductId` are required; only legacy
 * rows lack them.
 */
export async function POST(request: NextRequest) {
  const parsed = parseNewTransfer(await request.json().catch(() => null));
  if (!parsed.ok) {
    return NextResponse.json(
      { error: parsed.error, field: parsed.field },
      { status: parsed.status }
    );
  }
  const input = parsed.value;

  const found = await db
    .select({ id: products.id, currency: products.currency })
    .from(products)
    .where(inArray(products.id, [input.fromProductId, input.toProductId]));
  const endpoints = checkTransferEndpoints(input, found);
  if (!endpoints.ok) {
    return NextResponse.json(
      { error: endpoints.error, field: endpoints.field },
      { status: endpoints.status }
    );
  }

  const [created] = await db.insert(internalTransfers).values(input).returning();

  return NextResponse.json(created, { status: 201 });
}

/** PUT /api/transfers — update transfer status */
export async function PUT(request: NextRequest) {
  const body = await request.json();
  const { id, status, notes } = body;

  if (!id) {
    return NextResponse.json({ error: "Missing id" }, { status: 400 });
  }

  const updates: Record<string, unknown> = { updatedAt: new Date() };
  if (status) updates.status = status;
  if (notes !== undefined) updates.notes = notes;

  const [updated] = await db
    .update(internalTransfers)
    .set(updates)
    .where(eq(internalTransfers.id, id))
    .returning();

  if (!updated) {
    return NextResponse.json({ error: "Not found" }, { status: 404 });
  }

  return NextResponse.json(updated);
}

/** DELETE /api/transfers — delete a transfer */
export async function DELETE(request: NextRequest) {
  const { id } = await request.json();

  if (!id) {
    return NextResponse.json({ error: "Missing id" }, { status: 400 });
  }

  await db.delete(internalTransfers).where(eq(internalTransfers.id, id));
  return NextResponse.json({ ok: true });
}
