import { and, asc, eq, isNull } from "drizzle-orm";
import { db } from "@/lib/db";
import {
  users,
  institutions,
  accounts,
  products,
  type ProductKind,
} from "./schema";

/**
 * Resolve (or create) the product for an institution slug + kind + currency
 * (+ optional external_ref), walking the chain institution -> account ->
 * product. Single-user deployment: everything attaches to the oldest user.
 *
 * Note: the products identity is the `uq_products_identity` expression index
 * (COALESCE(external_ref, '')), which Drizzle can't target in an onConflict —
 * so this keeps the select-then-insert flow. The scraper writer is the only
 * concurrent products writer in practice, and it does upsert atomically.
 */
export async function resolveProductId(
  institutionSlug: string,
  kind: ProductKind,
  currency = "CLP",
  externalRef: string | null = null
): Promise<string> {
  let [institution] = await db
    .select()
    .from(institutions)
    .where(eq(institutions.slug, institutionSlug))
    .limit(1);
  if (!institution) {
    [institution] = await db
      .insert(institutions)
      .values({ slug: institutionSlug, name: institutionSlug, kind: "other" })
      .returning();
  }

  const [user] = await db
    .select()
    .from(users)
    .orderBy(asc(users.createdAt))
    .limit(1);
  if (!user) {
    throw new Error("No users found — run the V009 migration first");
  }

  let [account] = await db
    .select()
    .from(accounts)
    .where(
      and(
        eq(accounts.userId, user.id),
        eq(accounts.institutionId, institution.id)
      )
    )
    .orderBy(asc(accounts.displayOrder))
    .limit(1);
  if (!account) {
    [account] = await db
      .insert(accounts)
      .values({ userId: user.id, institutionId: institution.id })
      .returning();
  }

  let [product] = await db
    .select()
    .from(products)
    .where(
      and(
        eq(products.accountId, account.id),
        eq(products.kind, kind),
        eq(products.currency, currency),
        externalRef == null
          ? isNull(products.externalRef)
          : eq(products.externalRef, externalRef)
      )
    )
    .limit(1);
  if (!product) {
    [product] = await db
      .insert(products)
      .values({
        accountId: account.id,
        kind,
        currency,
        externalRef,
        name: `${institution.name} - ${kind}`,
      })
      .returning();
  }

  return product.id;
}
