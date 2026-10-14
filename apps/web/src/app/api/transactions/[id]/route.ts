import { NextResponse } from "next/server";
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
  /** ISO timestamps. */
  createdAt: string;
  updatedAt: string;
  product: {
    id: string;
    name: string;
    kind: string;
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
