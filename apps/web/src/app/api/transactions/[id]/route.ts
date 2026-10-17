import { NextResponse, type NextRequest } from "next/server";
import { and, eq, isNull, or } from "drizzle-orm";
import { db } from "@/lib/db";
import {
  accounts,
  categories,
  institutions,
  internalTransfers,
  products,
  transactions,
  type TransferStatus,
} from "@/lib/db/schema";
import { UUID_RE } from "@/lib/monitors/validate";
import type { ProductKind } from "@chanchito/product-model";
import {
  locationFromColumns,
  locationToColumns,
  validateLocationInput,
  type TransactionLocation,
} from "@/lib/transaction-location";
import { failureBody, readJsonBody } from "@/lib/api/validation";
import { updateTransactionSchema } from "@/lib/api/schemas";

// Next 16: dynamic segment params arrive as a Promise on the context arg.
type Context = { params: Promise<{ id: string }> };

/** One transaction with the product, institution and category it belongs to. */
export interface TransactionDetail {
  id: string;
  productId: string;
  categoryId: string | null;
  description: string;
  /** Signed amount in the product's currency: negative = expense. */
  amount: number;
  /** Plain YYYY-MM-DD dates. */
  transactionDate: string;
  accountingDate: string | null;
  scheduledMonth: string | null;
  source: string;
  externalId: string | null;
  isInternalTransfer: boolean;
  isManuallyCategorized: boolean;
  notes: string | null;
  /** Where it happened, when known (V025); null when no field is set. */
  location: TransactionLocation | null;
  /** The user set or cleared the location; scrapers no longer touch it. */
  locationIsManual: boolean;
  /** ISO timestamps. */
  createdAt: string;
  updatedAt: string;
  product: {
    id: string;
    name: string;
    kind: ProductKind;
    slug: string;
    currency: string;
  };
  institution: { id: string; name: string; slug: string };
  category: { id: string; name: string } | null;
  /** The internal transfer that links this row (V028), and on which side:
   *  "from" = its outgoing leg, "to" = its incoming leg. `autoLinked`: the
   *  suggestions linked it on their own (V032). */
  transfer: {
    id: string;
    description: string;
    amount: number;
    transferDate: string;
    status: TransferStatus;
    side: "from" | "to";
    autoLinked: boolean;
  } | null;
}

const detailColumns = {
  id: transactions.id,
  productId: transactions.productId,
  categoryId: transactions.categoryId,
  description: transactions.description,
  amount: transactions.amount,
  transactionDate: transactions.transactionDate,
  accountingDate: transactions.accountingDate,
  scheduledMonth: transactions.scheduledMonth,
  source: transactions.source,
  externalId: transactions.externalId,
  isInternalTransfer: transactions.isInternalTransfer,
  isManuallyCategorized: transactions.isManuallyCategorized,
  notes: transactions.notes,
  locationName: transactions.locationName,
  locationCity: transactions.locationCity,
  locationCountry: transactions.locationCountry,
  locationLatitude: transactions.locationLatitude,
  locationLongitude: transactions.locationLongitude,
  locationIsManual: transactions.locationIsManual,
  createdAt: transactions.createdAt,
  updatedAt: transactions.updatedAt,
  productName: products.name,
  productKind: products.kind,
  productSlug: products.slug,
  productCurrency: products.currency,
  institutionId: institutions.id,
  institutionName: institutions.name,
  institutionSlug: institutions.slug,
  categoryName: categories.name,
};

/**
 * GET /api/transactions/[id]: the full transaction row joined with its
 * product, institution, (optional) category and the internal transfer that
 * links it, if any. 400 `{ error, field }` on a
 * malformed id, 404 `{ error }` when no transaction has that id.
 */
export async function GET(_request: Request, { params }: Context) {
  const { id } = await params;
  if (!UUID_RE.test(id)) {
    return NextResponse.json(
      { error: "Invalid transaction id", field: "id" },
      { status: 400 }
    );
  }

  try {
    return await loadTransactionDetail(id);
  } catch (error) {
    console.error("GET /api/transactions/[id] failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}

async function loadTransactionDetail(id: string) {
  const [row] = await db
    .select(detailColumns)
    .from(transactions)
    .innerJoin(products, eq(transactions.productId, products.id))
    .innerJoin(accounts, eq(products.accountId, accounts.id))
    .innerJoin(institutions, eq(accounts.institutionId, institutions.id))
    .leftJoin(categories, eq(transactions.categoryId, categories.id))
    // A soft-deleted row (V033) is gone as far as the dashboard is concerned.
    .where(and(eq(transactions.id, id), isNull(transactions.deletedAt)))
    .limit(1);

  if (!row) {
    return NextResponse.json(
      { error: "Transaction not found" },
      { status: 404 }
    );
  }

  const [link] = await db
    .select({
      id: internalTransfers.id,
      description: internalTransfers.description,
      amount: internalTransfers.amount,
      transferDate: internalTransfers.transferDate,
      status: internalTransfers.status,
      autoLinked: internalTransfers.autoLinked,
      fromTransactionId: internalTransfers.fromTransactionId,
    })
    .from(internalTransfers)
    .where(
      or(
        eq(internalTransfers.fromTransactionId, id),
        eq(internalTransfers.toTransactionId, id)
      )
    )
    .limit(1);

  const detail: TransactionDetail = {
    id: row.id,
    productId: row.productId,
    categoryId: row.categoryId,
    description: row.description,
    amount: row.amount,
    transactionDate: row.transactionDate,
    accountingDate: row.accountingDate,
    scheduledMonth: row.scheduledMonth,
    source: row.source,
    externalId: row.externalId,
    isInternalTransfer: row.isInternalTransfer,
    isManuallyCategorized: row.isManuallyCategorized,
    notes: row.notes,
    location: locationFromColumns(row),
    locationIsManual: row.locationIsManual,
    createdAt: row.createdAt.toISOString(),
    updatedAt: row.updatedAt.toISOString(),
    product: {
      id: row.productId,
      name: row.productName,
      kind: row.productKind,
      slug: row.productSlug,
      currency: row.productCurrency,
    },
    institution: {
      id: row.institutionId,
      name: row.institutionName,
      slug: row.institutionSlug,
    },
    // Left join: a missing category comes back as a null name.
    category:
      row.categoryId && row.categoryName !== null
        ? { id: row.categoryId, name: row.categoryName }
        : null,
    transfer: link
      ? {
          id: link.id,
          description: link.description,
          amount: link.amount,
          transferDate: link.transferDate,
          status: link.status,
          side: link.fromTransactionId === row.id ? "from" : "to",
          autoLinked: link.autoLinked,
        }
      : null,
  };
  return NextResponse.json(detail);
}

// Columns of the transactions table that PATCH may write, by name, so an
// update can be built from only the fields the body carries.
type TransactionUpdate = Partial<{
  description: string;
  amount: number;
  transactionDate: string;
  categoryId: string | null;
  isManuallyCategorized: boolean;
  scheduledMonth: string | null;
  isInternalTransfer: boolean;
  productId: string;
  locationName: string | null;
  locationCity: string | null;
  locationCountry: string | null;
  locationLatitude: number | null;
  locationLongitude: number | null;
  locationIsManual: boolean;
  updatedAt: Date;
}>;

/**
 * PATCH /api/transactions/[id]: edit a transaction. The body is a non-empty
 * subset of `description`, `amount` (signed CLP; negative = expense),
 * `transactionDate`, `scheduledMonth` (null unassigns it), `categoryId` (null
 * clears the category), `isInternalTransfer`, `productId` and `location`.
 *
 * `location` keeps the contract the detail panel's location field relies on:
 * `{ name?, city?, country?, latitude?, longitude? } | null` replaces the
 * stored location as a whole (an omitted field is cleared, `null` clears it)
 * and marks it as the user's, so later scrapes leave it alone. A non-null
 * `categoryId` also sets `is_manually_categorized`, so the choice survives the
 * auto-categorization backfill (which only touches rows that are not manually
 * categorized); clearing the category (null) leaves that flag untouched.
 *
 * Response contract: a location-only body returns `{ location, locationIsManual }`
 * (what `TransactionLocationField` reads); any body with another field returns
 * the full refreshed `TransactionDetail` (which also carries `location` and
 * `locationIsManual`). A soft-deleted row (V033) is not editable.
 *
 * 400 `{ error, field?, issues? }` on a malformed id or invalid field;
 * 404 `{ error }` for an unknown or deleted id; 200 on success.
 */
export async function PATCH(request: NextRequest, { params }: Context) {
  const { id } = await params;
  if (!UUID_RE.test(id)) {
    return NextResponse.json(
      { error: "Invalid transaction id", field: "id" },
      { status: 400 }
    );
  }

  const read = await readJsonBody(request);
  if (!read.ok) return read.response;
  const parsed = updateTransactionSchema.safeParse(read.body);
  if (!parsed.success) {
    const issue = parsed.error.issues[0];
    const field = issue?.path.map(String).join(".") || undefined;
    return NextResponse.json(
      failureBody({ error: issue?.message ?? "Invalid request", field }),
      { status: 400 }
    );
  }
  const body = parsed.data;

  const update: TransactionUpdate = { updatedAt: new Date() };
  if (body.description !== undefined) update.description = body.description;
  if (body.amount !== undefined) update.amount = body.amount;
  if (body.transactionDate !== undefined)
    update.transactionDate = body.transactionDate;
  if (body.scheduledMonth !== undefined)
    update.scheduledMonth = body.scheduledMonth;
  if (body.isInternalTransfer !== undefined)
    update.isInternalTransfer = body.isInternalTransfer;
  if (body.productId !== undefined) update.productId = body.productId;
  if ("categoryId" in body) {
    update.categoryId = body.categoryId ?? null;
    // A deliberate category choice is manual, so the backfill leaves it alone.
    // Clearing it (null) does not flip the flag back: the user may be about to
    // recategorize, and the backfill only fills rows that have no category.
    if (body.categoryId) update.isManuallyCategorized = true;
  }

  const locationInBody = "location" in body;
  let location: TransactionLocation | null = null;
  if (locationInBody) {
    const result = validateLocationInput(body.location);
    if (!result.ok) {
      return NextResponse.json(
        { error: result.error, field: result.field },
        { status: result.status }
      );
    }
    location = result.value;
    Object.assign(update, locationToColumns(location));
    update.locationIsManual = true;
  }

  // A location-only body keeps the lightweight response the panel's location
  // field consumes; any other field means the caller wants the full row back.
  const locationOnly =
    locationInBody && Object.keys(body).length === 1;

  try {
    const [updated] = await db
      .update(transactions)
      .set(update)
      // Never edit a soft-deleted row.
      .where(and(eq(transactions.id, id), isNull(transactions.deletedAt)))
      .returning({ id: transactions.id });
    if (!updated) {
      return NextResponse.json(
        { error: "Transaction not found" },
        { status: 404 }
      );
    }
    if (locationOnly) {
      return NextResponse.json({ location, locationIsManual: true });
    }
    return await loadTransactionDetail(id);
  } catch (error) {
    console.error("PATCH /api/transactions/[id] failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}

/**
 * DELETE /api/transactions/[id]: remove a transaction. An imported row (one
 * with an `external_id`: scraper or CSV) is soft-deleted (`deleted_at = now()`)
 * so it keeps its (product_id, external_id) key and a re-scrape or re-import
 * skips it instead of bringing it back (V033). A manual row (no `external_id`)
 * is hard-deleted, since nothing can re-import it.
 *
 * 400 `{ error, field }` on a malformed id; 404 `{ error }` for an unknown or
 * already-deleted id; 200 `{ ok: true, softDeleted }` on success.
 */
export async function DELETE(_request: Request, { params }: Context) {
  const { id } = await params;
  if (!UUID_RE.test(id)) {
    return NextResponse.json(
      { error: "Invalid transaction id", field: "id" },
      { status: 400 }
    );
  }

  try {
    const [row] = await db
      .select({ externalId: transactions.externalId })
      .from(transactions)
      .where(and(eq(transactions.id, id), isNull(transactions.deletedAt)))
      .limit(1);
    if (!row) {
      return NextResponse.json(
        { error: "Transaction not found" },
        { status: 404 }
      );
    }

    const softDelete = row.externalId !== null && row.externalId !== "";
    if (softDelete) {
      await db
        .update(transactions)
        .set({ deletedAt: new Date(), updatedAt: new Date() })
        .where(eq(transactions.id, id));
    } else {
      await db.delete(transactions).where(eq(transactions.id, id));
    }
    return NextResponse.json({ ok: true, softDeleted: softDelete });
  } catch (error) {
    console.error("DELETE /api/transactions/[id] failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}
