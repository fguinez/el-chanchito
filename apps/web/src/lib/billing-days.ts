import type { ProductAttributes } from "@chanchito/product-model";
import {
  BILLING_DAY_FIELDS,
  isBillingDay,
  type BillingDayField,
} from "@/lib/management";

/** A credit card's billing days, as the product PATCH names them. */
export type BillingDays = Record<BillingDayField, number | null>;

/** The billing days stored in a product's attributes (null when unknown). */
export function storedBillingDays(
  attributes: ProductAttributes | Record<string, never>
): BillingDays {
  return {
    statementDay:
      ("statement_day" in attributes ? attributes.statement_day : null) ?? null,
    dueDay: ("due_day" in attributes ? attributes.due_day : null) ?? null,
  };
}

/**
 * What the edit dialog's billing-day inputs send: only the days that changed,
 * an emptied input as null. A day left as stored is neither sent nor checked,
 * so an odd value a scraper stored never blocks an unrelated save; a changed
 * day must be plain digits naming a day of the month, else `field` names the
 * first that is not.
 */
export function billingDaysPatch(
  inputs: Record<BillingDayField, string>,
  stored: BillingDays
):
  | { ok: true; patch: Partial<BillingDays> }
  | { ok: false; field: BillingDayField } {
  const patch: Partial<BillingDays> = {};
  for (const field of BILLING_DAY_FIELDS) {
    const text = inputs[field].trim();
    const day = text === "" ? null : /^\d+$/.test(text) ? Number(text) : NaN;
    if (day === stored[field]) continue;
    if (day !== null && !isBillingDay(day)) return { ok: false, field };
    patch[field] = day;
  }
  return { ok: true, patch };
}
