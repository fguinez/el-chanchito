// Monitor adjustments ("variaciones"): request validation, the API shape, and
// the per-day grouping the monitor page renders. Pure (no db, no next
// imports) like validate.ts; how an adjustment moves the thresholds lives in
// evaluate.ts (adjustmentOnDate).

import type { monitorAdjustments } from "@/lib/db/schema";
import { isValidDay } from "./dates";
import type { MonitorAdjustment } from "./types";
import type { ValidationFailure, ValidationResult } from "./validate";

/** An adjustment write, validated and normalized. Create mode returns every
 *  field; partial mode only the fields present in the body. */
export type NormalizedAdjustmentInput = {
  adjustmentDate: string;
  amount: number;
  description: string | null;
};

/** NUMERIC(20, 8) keeps 12 integer digits and 8 decimals. */
const MAX_ABS_AMOUNT = 1e12;
const AMOUNT_DECIMALS = 8;
export const MAX_DESCRIPTION_LENGTH = 200;
const MONTH_RE = /^\d{4}-(0[1-9]|1[0-2])$/;

function fail(error: string, field?: string): ValidationFailure {
  return { ok: false, status: 400, error, field };
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function normalizeAmount(raw: unknown): ValidationResult<number> {
  if (typeof raw !== "number" || !Number.isFinite(raw)) {
    return fail("Field 'amount' must be a number", "amount");
  }
  if (Math.abs(raw) >= MAX_ABS_AMOUNT) {
    return fail("Field 'amount' is too large", "amount");
  }
  // Round to the stored scale first, so a value the column would store as 0
  // is rejected here instead of failing the CHECK constraint.
  const amount = Number(raw.toFixed(AMOUNT_DECIMALS));
  if (amount === 0) {
    return fail("Field 'amount' must not be zero", "amount");
  }
  return { ok: true, value: amount };
}

function normalizeDescription(raw: unknown): ValidationResult<string | null> {
  if (raw === null) return { ok: true, value: null };
  if (typeof raw !== "string") {
    return fail("Field 'description' must be a string or null", "description");
  }
  const description = raw.trim();
  if (description.length > MAX_DESCRIPTION_LENGTH) {
    return fail(
      `Field 'description' must be at most ${MAX_DESCRIPTION_LENGTH} characters`,
      "description"
    );
  }
  return { ok: true, value: description === "" ? null : description };
}

/**
 * Validate and normalize an adjustment write. Create mode (default) requires
 * `adjustmentDate` (YYYY-MM-DD) and a non-zero `amount`; `description` is
 * optional (blank means none). `{ partial: true }` validates only the fields
 * present, for PATCH, and rejects a body with nothing to update.
 */
export function validateAdjustmentInput(
  body: unknown
): ValidationResult<NormalizedAdjustmentInput>;
export function validateAdjustmentInput(
  body: unknown,
  opts: { partial: true }
): ValidationResult<Partial<NormalizedAdjustmentInput>>;
export function validateAdjustmentInput(
  body: unknown,
  opts?: { partial?: boolean }
): ValidationResult<Partial<NormalizedAdjustmentInput>> {
  const partial = opts?.partial === true;
  if (!isPlainObject(body)) {
    return fail("Request body must be a JSON object");
  }
  const out: Partial<NormalizedAdjustmentInput> = {};

  if (body.adjustmentDate !== undefined) {
    if (
      typeof body.adjustmentDate !== "string" ||
      !isValidDay(body.adjustmentDate)
    ) {
      return fail(
        "Field 'adjustmentDate' must be a date, expected YYYY-MM-DD",
        "adjustmentDate"
      );
    }
    out.adjustmentDate = body.adjustmentDate;
  } else if (!partial) {
    return fail("Field 'adjustmentDate' is required", "adjustmentDate");
  }

  if (body.amount !== undefined) {
    const amount = normalizeAmount(body.amount);
    if (!amount.ok) return amount;
    out.amount = amount.value;
  } else if (!partial) {
    return fail("Field 'amount' is required", "amount");
  }

  if (body.description !== undefined) {
    const description = normalizeDescription(body.description);
    if (!description.ok) return description;
    out.description = description.value;
  } else if (!partial) {
    out.description = null;
  }

  if (partial && Object.keys(out).length === 0) {
    return fail(
      "Nothing to update: send adjustmentDate, amount or description"
    );
  }
  return { ok: true, value: out };
}

/** The optional `?month=YYYY-MM` list filter; null when absent. */
export function parseMonthParam(
  raw: string | null
): ValidationResult<string | null> {
  if (raw == null) return { ok: true, value: null };
  if (!MONTH_RE.test(raw)) {
    return fail("Invalid 'month', expected YYYY-MM", "month");
  }
  return { ok: true, value: raw };
}

export type MonitorAdjustmentRow = typeof monitorAdjustments.$inferSelect;

/** The API shape of an adjustment: the row with `amount` as a number (drizzle
 *  reads NUMERIC as a string). Also a MonitorAdjustment for the engine. */
export type ApiMonitorAdjustment = Omit<MonitorAdjustmentRow, "amount"> & {
  amount: number;
};

export function toApiAdjustment(
  row: MonitorAdjustmentRow
): ApiMonitorAdjustment {
  return { ...row, amount: Number(row.amount) };
}

export type AdjustmentDay<A extends MonitorAdjustment> = {
  adjustmentDate: string;
  /** That day's adjustments, in the order given. */
  entries: A[];
  dayTotal: number;
  /** Month-to-date sum through this day: what every threshold carries from
   *  this day until the next adjustment day (see adjustmentOnDate). */
  runningTotal: number;
};

/** One month's (YYYY-MM) adjustments grouped per day, oldest day first. */
export function groupAdjustmentsByDay<A extends MonitorAdjustment>(
  adjustments: readonly A[],
  month: string
): AdjustmentDay<A>[] {
  const byDay = new Map<string, A[]>();
  for (const adjustment of adjustments) {
    if (!adjustment.adjustmentDate.startsWith(`${month}-`)) continue;
    const entries = byDay.get(adjustment.adjustmentDate) ?? [];
    entries.push(adjustment);
    byDay.set(adjustment.adjustmentDate, entries);
  }

  let runningTotal = 0;
  return [...byDay.keys()].sort().map((adjustmentDate) => {
    const entries = byDay.get(adjustmentDate)!;
    const dayTotal = entries.reduce((sum, a) => sum + a.amount, 0);
    runningTotal += dayTotal;
    return { adjustmentDate, entries, dayTotal, runningTotal };
  });
}
