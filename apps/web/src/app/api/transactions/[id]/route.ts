import { NextResponse, type NextRequest } from "next/server";
import { eq } from "drizzle-orm";
import { db } from "@/lib/db";
import {
  accounts,
  categories,
  institutions,
  products,
  transactions,
} from "@/lib/db/schema";
import { UUID_RE } from "@/lib/monitors/validate";
import type { ProductKind } from "@chanchito/product-model";
import {
  locationFromColumns,
  locationToColumns,
  validateLocationInput,
  type TransactionLocation,
} from "@/lib/transaction-location";
import { readJsonBody } from "@/lib/api/validation";

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
 * product, institution and (optional) category. 400 `{ error, field }` on a
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
    .where(eq(transactions.id, id))
    .limit(1);

  if (!row) {
    return NextResponse.json(
      { error: "Transaction not found" },
      { status: 404 }
    );
  }

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
  };
  return NextResponse.json(detail);
}

/**
 * PATCH /api/transactions/[id]: set, replace or clear the location. The body
 * is `{ location: { name?, city?, country?, latitude?, longitude? } | null }`
 * and nothing else; the location given replaces the stored one as a whole
 * (an omitted field is cleared), and `null` clears it. Marks the location as
 * the user's, so later scrapes leave it alone. 400 `{ error, field? }`: a
 * malformed id or an invalid field names the field (e.g. "location.latitude"),
 * while invalid JSON or a body that is not an object carries only `error`.
 * 404 `{ error }` for an unknown id; 200 `{ location, locationIsManual }` on
 * success.
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
  const body = read.body;
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    return NextResponse.json(
      { error: "Body must be a JSON object" },
      { status: 400 }
    );
  }
  const unknownKey = Object.keys(body).find((key) => key !== "location");
  if (unknownKey !== undefined) {
    return NextResponse.json(
      { error: `Only 'location' can be updated here`, field: unknownKey },
      { status: 400 }
    );
  }
  if (!("location" in body)) {
    return NextResponse.json(
      { error: "Missing required field: location", field: "location" },
      { status: 400 }
    );
  }
  const result = validateLocationInput(
    (body as { location: unknown }).location
  );
  if (!result.ok) {
    return NextResponse.json(
      { error: result.error, field: result.field },
      { status: result.status }
    );
  }

  try {
    const [updated] = await db
      .update(transactions)
      .set({
        ...locationToColumns(result.value),
        locationIsManual: true,
        updatedAt: new Date(),
      })
      .where(eq(transactions.id, id))
      .returning({ id: transactions.id });
    if (!updated) {
      return NextResponse.json(
        { error: "Transaction not found" },
        { status: 404 }
      );
    }
    return NextResponse.json({
      location: result.value,
      locationIsManual: true,
    });
  } catch (error) {
    console.error("PATCH /api/transactions/[id] failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}
