// Body and query schemas of the API routes (see lib/api/validation for how
// routes apply them). Enums come from the Drizzle schema so validation cannot
// drift from what the database stores. Unknown keys are stripped, so a body
// can never write a column its route does not name.

import { z } from "zod";
import { PRODUCT_KINDS, TRANSFER_STATUSES } from "@/lib/db/schema";
import { TRANSACTION_NOTES_MAX_LENGTH } from "@/lib/transactions";

// ---------------------------------------------------------------------------
// Shared fields
// ---------------------------------------------------------------------------

/** A whole CLP amount: any finite number, rounded as the routes always did.
 *  Used where amounts are always pesos (manual CLP entry, transfers); the
 *  transactions column itself is NUMERIC(18, 2) so foreign-currency rows can
 *  keep cents (see `moneyAmount`). */
export const clpAmount = z
  .number()
  .transform((n) => Math.round(n))
  .pipe(z.int32());

/** A signed amount in a product's own currency, kept to the two decimals the
 *  NUMERIC(18, 2) column stores: CLP rows round to whole pesos, USD rows (V023)
 *  keep their cents. The bound keeps `n * 100` below Number.MAX_SAFE_INTEGER so
 *  the cents rounding stays exact, and still sits far above any real balance. */
export const moneyAmount = z
  .number()
  .transform((n) => Math.round(n * 100) / 100)
  .pipe(z.number().min(-1e13).max(1e13));

/** A calendar day, YYYY-MM-DD; impossible days like 2023-02-29 fail. */
export const isoDate = z.iso.date({ error: "Expected a YYYY-MM-DD date" });

/** A row id: any 8-4-4-4-12 hex id, as Postgres accepts (seeded rows are not
 *  RFC 4122 uuids). */
export const rowId = z.guid({ error: "Expected a uuid" });

const requiredText = z.string().trim().min(1);
const notes = z.string().nullish();

/** A transaction's note: trimmed and capped; a blank string becomes null. */
const transactionNotes = z
  .string()
  .trim()
  .max(TRANSACTION_NOTES_MAX_LENGTH, {
    error: `Notes must be at most ${TRANSACTION_NOTES_MAX_LENGTH} characters`,
  })
  .transform((text) => text || null);

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

const fixedExpenseSchema = z.object({
  name: requiredText,
  amount: clpAmount,
  isShared: z.boolean(),
  sharedRatio: z.number().min(0).max(1).nullable(),
  activeFrom: isoDate.nullable(),
  activeTo: isoDate.nullable(),
});

/** Both bounds given and the window ends before it starts: fails on activeTo.
 *  PUT also checks the merged window against the stored row. */
function windowIsOrdered(value: {
  activeFrom?: string | null;
  activeTo?: string | null;
}): boolean {
  const { activeFrom, activeTo } = value;
  return activeFrom == null || activeTo == null || activeFrom <= activeTo;
}

const orderedWindow = {
  error: "The active window must not end before it starts",
  path: ["activeTo"],
};

/** POST /api/fixed-expenses: name and amount are required. */
export const createFixedExpenseSchema = fixedExpenseSchema
  .partial({ sharedRatio: true, activeFrom: true, activeTo: true })
  .extend({ isShared: z.boolean().nullish() })
  .refine(windowIsOrdered, orderedWindow);

/** PUT /api/fixed-expenses: a non-empty subset of the fields, by id. */
export const updateFixedExpenseSchema = fixedExpenseSchema
  .partial()
  .extend({ id: rowId })
  .refine(windowIsOrdered, orderedWindow)
  .refine((body) => Object.keys(body).some((key) => key !== "id"), {
    error: "No updatable fields provided",
  });

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
  notes: transactionNotes.nullish(),
  productId: rowId.nullish(),
  /** Legacy alias for `productId`. */
  accountId: rowId.nullish(),
  /** Checked by validateLocationInput, as PATCH /api/transactions/[id] does. */
  location: z.unknown().optional(),
});

/** PATCH /api/transactions/[id]: a non-empty subset of a transaction's
 *  editable fields (the id comes from the URL). `categoryId` null clears the
 *  category; `scheduledMonth` null unassigns the budget month.
 *  `customDescription` is the one-time override (V034): a non-empty string sets
 *  it, null clears it (restoring the original); an empty string is rejected.
 *  `location` is passed through untouched and checked by validateLocationInput
 *  in the route, as POST /api/transactions does. `notes` is trimmed; a blank
 *  string or null clears the note (so `{ notes: "" }` is a clear, not an empty
 *  body). */
export const updateTransactionSchema = z
  .object({
    description: requiredText,
    // Non-empty to set the one-time override, null to clear it (V034).
    customDescription: z.string().trim().min(1).nullable(),
    // Kept to 2 decimals so a USD row keeps its cents; CLP rounds to pesos.
    amount: moneyAmount,
    transactionDate: isoDate,
    categoryId: rowId.nullable(),
    scheduledMonth: month.nullable(),
    isInternalTransfer: z.boolean(),
    productId: rowId,
    notes: transactionNotes.nullable(),
    location: z.unknown(),
  })
  .partial()
  .refine((body) => Object.keys(body).length > 0, {
    error: "No updatable fields provided",
  });

/** POST /api/transactions/[id]/category-rule: the category to give the row,
 *  the rows with the same description and a rule for future ones. */
export const transactionCategoryRuleSchema = z.object({
  categoryId: rowId,
});

/** Longest description a category suggestion body accepts. */
export const SUGGESTION_DESCRIPTION_MAX_LENGTH = 1000;

/** POST /api/categories/suggestions/apply and .../dismiss: the raw
 *  description of the suggested rows, untrimmed so it matches them as stored,
 *  and the suggested category (lowercased, as Postgres returns ids). */
export const categorySuggestionSchema = z.object({
  description: z
    .string()
    .max(SUGGESTION_DESCRIPTION_MAX_LENGTH)
    .refine((description) => description.trim() !== "", { error: "Description is blank" }),
  categoryId: rowId.transform((id) => id.toLowerCase()),
});

/** POST /api/transfers: money between two distinct products, both required.
 *  `from*`/`to*AccountId` are legacy aliases; ids come out lowercased, as
 *  Postgres returns them. */
export const createTransferSchema = z
  .object({
    description: requiredText,
    amount: clpAmount.pipe(z.number().positive()),
    transferDate: isoDate,
    notes,
    fromProductId: rowId.nullish(),
    fromAccountId: rowId.nullish(),
    toProductId: rowId.nullish(),
    toAccountId: rowId.nullish(),
  })
  .transform((body, ctx) => {
    const fromProductId = (body.fromProductId ?? body.fromAccountId)?.toLowerCase();
    const toProductId = (body.toProductId ?? body.toAccountId)?.toLowerCase();
    if (!fromProductId || !toProductId) {
      ctx.addIssue({
        code: "custom",
        message: "Expected a product id",
        path: [fromProductId ? "toProductId" : "fromProductId"],
      });
      return z.NEVER;
    }
    if (fromProductId === toProductId) {
      ctx.addIssue({
        code: "custom",
        message: "Source and target products must differ",
        path: ["toProductId"],
      });
      return z.NEVER;
    }
    return {
      description: body.description,
      amount: body.amount,
      fromProductId,
      toProductId,
      transferDate: body.transferDate,
      notes: body.notes?.trim() || null,
    };
  });

/** PUT /api/transfers: status and/or notes (null clears them), by id. */
export const updateTransferSchema = z.object({
  id: rowId,
  status: z.enum(TRANSFER_STATUSES).optional(),
  notes,
});

/** A transfer side's transaction: a row id (lowercased, as Postgres returns
 *  it), null to clear the link, or omitted to leave it as it is. */
const transferLink = rowId
  .transform((id) => id.toLowerCase())
  .nullable()
  .optional();

/** PUT /api/transfers/[id]/links: set or clear either side, at least one. */
export const transferLinksSchema = z
  .object({
    fromTransactionId: transferLink,
    toTransactionId: transferLink,
  })
  .refine(
    (body) =>
      body.fromTransactionId !== undefined || body.toTransactionId !== undefined,
    { error: "Expected fromTransactionId and/or toTransactionId" }
  );

/** A suggestion leg's row or product id, lowercased as Postgres returns it. */
const suggestionId = rowId.transform((id) => id.toLowerCase());

/** GET /api/transfers/suggestions: optionally only those using one row. */
export const suggestionsQuerySchema = z.object({
  transactionId: suggestionId.optional(),
});

/** POST /api/transfers/suggestions/confirm: the outgoing row and either the
 *  incoming row (a pair) or the target product (one-sided), not both. */
export const confirmSuggestionSchema = z
  .object({
    sourceTransactionId: suggestionId,
    targetTransactionId: suggestionId.nullish(),
    targetProductId: suggestionId.nullish(),
  })
  .transform((body, ctx) => {
    const targetTransactionId = body.targetTransactionId ?? null;
    const targetProductId = body.targetProductId ?? null;
    if (targetTransactionId === null && targetProductId === null) {
      ctx.addIssue({
        code: "custom",
        message: "Expected targetTransactionId or targetProductId",
        path: ["targetTransactionId"],
      });
      return z.NEVER;
    }
    if (targetTransactionId !== null && targetProductId !== null) {
      ctx.addIssue({
        code: "custom",
        message: "Expected targetTransactionId or targetProductId, not both",
        path: ["targetProductId"],
      });
      return z.NEVER;
    }
    if (targetTransactionId === body.sourceTransactionId) {
      ctx.addIssue({
        code: "custom",
        message: "Source and target transactions must differ",
        path: ["targetTransactionId"],
      });
      return z.NEVER;
    }
    return {
      sourceTransactionId: body.sourceTransactionId,
      targetTransactionId,
      targetProductId,
    };
  });

/** POST /api/transfers/suggestions/dismiss: the outgoing row and the incoming
 *  one of a pair; no target (or null) dismisses the one-sided suggestion. */
export const dismissSuggestionSchema = z
  .object({
    sourceTransactionId: suggestionId,
    targetTransactionId: suggestionId.nullish(),
  })
  .transform((body, ctx) => {
    const targetTransactionId = body.targetTransactionId ?? null;
    if (targetTransactionId === body.sourceTransactionId) {
      ctx.addIssue({
        code: "custom",
        message: "Source and target transactions must differ",
        path: ["targetTransactionId"],
      });
      return z.NEVER;
    }
    return { sourceTransactionId: body.sourceTransactionId, targetTransactionId };
  });

/** A legacy total: patrimonio and deuda are sums, never negative. */
const wealthTotal = clpAmount.pipe(z.number().nonnegative());

/** POST /api/wealth; the backdating cutoff is checked by the route. */
export const createWealthSnapshotSchema = z.object({
  snapshotDate: isoDate,
  patrimonio: wealthTotal,
  deuda: wealthTotal.nullish(),
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

/** POST /api/monitors/[id]/adjustments: the transaction to ignore,
 *  lowercased as Postgres returns ids. */
export const createMonitorAdjustmentSchema = z.object({
  transactionId: rowId.transform((id) => id.toLowerCase()),
});

/** Largest page GET /api/monitors/[id]/adjustments/candidates returns. */
export const MAX_ADJUSTMENT_CANDIDATES = 100;

/** GET /api/monitors/[id]/adjustments/candidates query string: an optional
 *  search text, a YYYY-MM month (the route defaults to the current one) and
 *  a page size. */
export const adjustmentCandidatesQuerySchema = z.object({
  q: z.string().trim().max(200).optional(),
  month: z
    .string()
    .regex(/^\d{4}-(0[1-9]|1[0-2])$/, { error: "Expected a YYYY-MM month" })
    .optional(),
  limit: z.coerce
    .number()
    .int()
    .min(1)
    .max(MAX_ADJUSTMENT_CANDIDATES)
    .default(50),
});
