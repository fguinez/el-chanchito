import { and, asc, eq } from "drizzle-orm";
import { db } from "@/lib/db";
import {
  users,
  institutions,
  accounts,
  products,
  type ProductKind,
} from "./schema";

/**
 * Resolve (or create) the product for an institution slug + kind + currency,
 * walking the chain institution -> account -> product. Single-user deployment:
 * everything attaches to the oldest user.
 */
export async function resolveProductId(
  institutionSlug: string,
  kind: ProductKind,
  currency = "CLP"
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
        eq(products.currency, currency)
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
        name: `${institution.name} - ${kind}`,
      })
      .returning();
  }

  return product.id;
}
