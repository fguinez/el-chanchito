// Body and query schemas of the API routes (see lib/api/validation for how
// routes apply them). Enums come from the Drizzle schema so validation cannot
// drift from what the database stores. Unknown keys are stripped, so a body
// can never write a column its route does not name.

import { z } from "zod";
import { PRODUCT_KINDS, TRANSFER_STATUSES } from "@/lib/db/schema";

// ---------------------------------------------------------------------------
// Shared fields
// ---------------------------------------------------------------------------

/** A whole CLP amount: any finite number, rounded as the routes always did,
 *  that fits the Postgres `integer` columns amounts live in. */
export const clpAmount = z
  .number()
  .transform((n) => Math.round(n))
  .pipe(z.int32());

/** A calendar day, YYYY-MM-DD; impossible days like 2023-02-29 fail. */
export const isoDate = z.iso.date({ error: "Expected a YYYY-MM-DD date" });

/** A row id: any 8-4-4-4-12 hex id, as Postgres accepts (seeded rows are not
 *  RFC 4122 uuids). */
export const rowId = z.guid({ error: "Expected a uuid" });

const requiredText = z.string().trim().min(1);
const notes = z.string().nullish();

/** First day of the month of a YYYY-MM-DD day, read off the string so the
 *  server's time zone cannot move it (`new Date("2026-03-01")` is UTC midnight,
 *  still February in Chile). */
export function monthStart(day: string): string {
  return `${day.slice(0, 7)}-01`;
}

/** A budget month (`scheduled_month`): any day of it, stored as its first. */
const month = isoDate.transform(monthStart);

/** `{ id }`, the body of the DELETE handlers. */
export const idSchema = z.object({ id: rowId });

// ---------------------------------------------------------------------------
// Per route
// ---------------------------------------------------------------------------

/** POST /api/categories */
export const createCategoryRuleSchema = z.object({
  keyword: requiredText.toLowerCase(),
  categoryId: rowId,
  priority: z.int32().nullish(),
});

const fixedExpenseSchema = z.object({
  name: requiredText,
  amount: clpAmount,
  isShared: z.boolean(),
  sharedRatio: z.number().min(0).max(1).nullable(),
  activeFrom: isoDate.nullable(),
  activeTo: isoDate.nullable(),
});

/** POST /api/fixed-expenses: name and amount are required. */
export const createFixedExpenseSchema = fixedExpenseSchema
  .partial({ sharedRatio: true, activeFrom: true, activeTo: true })
  .extend({ isShared: z.boolean().nullish() });

/** PUT /api/fixed-expenses: any subset of the fields, by id. */
export const updateFixedExpenseSchema = fixedExpenseSchema
  .partial()
  .extend({ id: rowId });

/** POST /api/import. Rows are checked one by one (csvRowSchema) so a bad
 *  line is skipped instead of failing the whole file. */
export const importSchema = z.object({
  rows: z
    .array(z.unknown(), { error: "No rows provided" })
    .min(1, { error: "No rows provided" }),
  institution: z.string().trim().nullish(),
  kind: z.enum(PRODUCT_KINDS).nullish(),
  /** Legacy alias for `kind`. */
  accountType: z.enum(PRODUCT_KINDS).nullish(),
});

export const csvRowSchema = z.object({
  description: z.string().min(1),
  amount: clpAmount,
  date: isoDate,
});

/** Largest page GET /api/transactions returns. */
export const MAX_TRANSACTIONS_LIMIT = 500;

/** GET /api/transactions query string. */
export const listTransactionsQuerySchema = z.object({
  month: month.optional(),
  productId: rowId.optional(),
  /** Legacy alias for `productId`. */
  accountId: rowId.optional(),
  limit: z.coerce
    .number()
    .int()
    .min(1)
    .max(MAX_TRANSACTIONS_LIMIT)
    .default(50),
});

/** POST /api/transactions */
export const createTransactionSchema = z.object({
  description: requiredText,
  amount: clpAmount,
  transactionDate: isoDate,
  scheduledMonth: month.nullish(),
  notes,
  productId: rowId.nullish(),
  /** Legacy alias for `productId`. */
  accountId: rowId.nullish(),
});

/** POST /api/transfers. `from*`/`to*AccountId` are legacy aliases. */
export const createTransferSchema = z.object({
  description: requiredText,
  amount: clpAmount,
  transferDate: isoDate,
  notes,
  fromProductId: rowId.nullish(),
  fromAccountId: rowId.nullish(),
  toProductId: rowId.nullish(),
  toAccountId: rowId.nullish(),
});

/** PUT /api/transfers: status and/or notes (null clears them), by id. */
export const updateTransferSchema = z.object({
  id: rowId,
  status: z.enum(TRANSFER_STATUSES).optional(),
  notes,
});

/** POST /api/wealth */
export const createWealthSnapshotSchema = z.object({
  snapshotDate: isoDate,
  patrimonio: clpAmount,
  deuda: clpAmount.nullish(),
  fintualBalance: clpAmount.nullish(),
  mercadopagoBalance: clpAmount.nullish(),
  banchileSavings: clpAmount.nullish(),
  notes,
});

/** DELETE /api/wealth. Computed points carry a synthetic `computed-<day>` id. */
export const deleteWealthSnapshotSchema = z.object({
  id: z
    .string()
    .refine((id) => !id.startsWith("computed-"), {
      error:
        "Computed points are derived from product balances and cannot be deleted",
    })
    .pipe(rowId),
});
