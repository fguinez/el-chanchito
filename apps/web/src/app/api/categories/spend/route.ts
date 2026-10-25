import { NextRequest, NextResponse } from "next/server";
import { and, eq, gte, isNull, lte, sql, type SQL } from "drizzle-orm";
import { db } from "@/lib/db";
import {
  accounts,
  categories,
  institutions,
  products,
  transactions,
} from "@/lib/db/schema";
import { notDeleted } from "@/lib/db/transactions";
import { parseSearchParams } from "@/lib/api/validation";
import { categorySpendQuerySchema } from "@/lib/api/schemas";
import {
  groupSpend,
  summarizeSpend,
  UNCATEGORIZED_PARAM,
  type CategorySpendResponse,
  type SpendRow,
} from "@/lib/category-spend";
import { getRateBook } from "@/lib/rate-book";
import { ratesFor, toClp } from "@/lib/rates";
import { displayProductName } from "@/lib/product-names";

const month = sql<string>`to_char(${transactions.transactionDate}, 'YYYY-MM')`;

/**
 * GET /api/categories/spend?from=&to=: spending per category over an
 * inclusive day window (see lib/category-spend for what counts), with each
 * category's id, name, color, amount and percent, ranked. Optional filters:
 * `productId` (amounts then stay in the product's currency), `institution`
 * (slug), `categoryId` (a uuid, or `none` for uncategorized rows); outside a
 * product, amounts are CLP converted with each product's rates. `groupBy`
 * month|product|institution adds `groups`, each summarized on its own.
 * 400 for a bad query, 404 for an unknown product or institution.
 */
export async function GET(request: NextRequest) {
  const query = parseSearchParams(
    request.nextUrl.searchParams,
    categorySpendQuerySchema
  );
  if (!query.ok) return query.response;
  const { from, to, productId, institution, categoryId, groupBy } = query.data;

  try {
    let currency = "CLP";
    if (productId) {
      const [product] = await db
        .select({ currency: products.currency })
        .from(products)
        .where(eq(products.id, productId))
        .limit(1);
      if (!product) {
        return NextResponse.json({ error: "Product not found" }, { status: 404 });
      }
      currency = product.currency;
    }
    if (institution) {
      const [known] = await db
        .select({ id: institutions.id })
        .from(institutions)
        .where(eq(institutions.slug, institution))
        .limit(1);
      if (!known) {
        return NextResponse.json({ error: "Institution not found" }, { status: 404 });
      }
    }

    // Soft-deleted rows (V033) and internal transfers never count as spend.
    const conditions: SQL[] = [
      notDeleted,
      eq(transactions.isInternalTransfer, false),
      gte(transactions.transactionDate, from),
      lte(transactions.transactionDate, to),
    ];
    if (productId) conditions.push(eq(transactions.productId, productId));
    if (institution) conditions.push(eq(institutions.slug, institution));
    if (categoryId === UNCATEGORIZED_PARAM) {
      conditions.push(isNull(transactions.categoryId));
    } else if (categoryId) {
      conditions.push(eq(transactions.categoryId, categoryId));
    }

    // Partial sums per category, product and month, split by sign so the
    // uncategorized expenses stay apart from uncategorized income.
    const [sums, cats, book] = await Promise.all([
      db
        .select({
          categoryId: transactions.categoryId,
          productId: products.id,
          productName: products.name,
          productKind: products.kind,
          currency: products.currency,
          institutionSlug: institutions.slug,
          institutionName: institutions.name,
          month,
          // Raw SUM/COUNT bypass the column's number mapping.
          amount: sql<string>`sum(${transactions.amount})`,
          rows: sql<string>`count(*)`,
        })
        .from(transactions)
        .innerJoin(products, eq(transactions.productId, products.id))
        .innerJoin(accounts, eq(products.accountId, accounts.id))
        .innerJoin(institutions, eq(accounts.institutionId, institutions.id))
        .where(and(...conditions))
        .groupBy(
          transactions.categoryId,
          products.id,
          institutions.id,
          month,
          sql`${transactions.amount} < 0`
        ),
      db
        .select({ id: categories.id, name: categories.name, color: categories.color })
        .from(categories),
      productId ? null : getRateBook(),
    ]);

    const rows: SpendRow[] = [];
    let unconvertedCount = 0;
    for (const sum of sums) {
      const native = Number(sum.amount);
      const amount = book
        ? toClp(
            sum.currency,
            native,
            ratesFor(book, {
              productId: sum.productId,
              institutionSlug: sum.institutionSlug,
            })
          )
        : native;
      if (amount == null) {
        unconvertedCount += Number(sum.rows);
        continue;
      }
      rows.push({
        categoryId: sum.categoryId,
        amount,
        month: sum.month,
        productId: sum.productId,
        institutionSlug: sum.institutionSlug,
      });
    }

    const categoryInfo = new Map(cats.map((c) => [c.id, c]));
    const body: CategorySpendResponse = {
      from,
      to,
      currency,
      groupBy,
      ...summarizeSpend(rows, categoryInfo),
      unconvertedCount,
    };
    if (groupBy !== "none") {
      const labels = new Map<string, string>();
      for (const sum of sums) {
        if (groupBy === "product") {
          labels.set(
            sum.productId,
            displayProductName(
              {
                name: sum.productName,
                kind: sum.productKind,
                currency: sum.currency,
              },
              sum.institutionName
            )
          );
        } else if (groupBy === "institution") {
          labels.set(sum.institutionSlug, sum.institutionName);
        }
      }
      body.groups = groupSpend(rows, categoryInfo, groupBy, { from, to }).map(
        (group) => ({ ...group, label: labels.get(group.key) ?? group.key })
      );
    }
    return NextResponse.json(body);
  } catch (error) {
    console.error("GET /api/categories/spend failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}
