// Fixed expenses: the active window (vigente desde/hasta), the monthly totals,
// the stored-row checks of PUT /api/fixed-expenses and the form parsing shared
// by the add form and the edit dialog. Pure (no db, no next imports) so it
// unit-tests in isolation.
//
// Dates are YYYY-MM-DD strings (the DATE columns), compared as strings;
// `today` is the caller's local day (formatLocalDate), never the UTC date.

import { calcPersonalAmount } from "./budget-engine";
import { isValidDay } from "./monitors/dates";
import type { ValidationFailure, ValidationResult } from "./monitors/validate";
import { formatPlainDateEs } from "./utils";

export type FixedExpenseStatus = "active" | "scheduled" | "ended";

export type ActiveWindow = {
  activeFrom: string | null;
  activeTo: string | null;
};

/** A GET /api/fixed-expenses row; `sharedRatio` is the numeric column as a
 *  string (e.g. "0.6900"). */
export type FixedExpense = ActiveWindow & {
  id: string;
  name: string;
  amount: number;
  isShared: boolean;
  sharedRatio: string | null;
};

export const DEFAULT_SHARED_RATIO = "0.6900";

const INT32_MIN = -2_147_483_648;
const INT32_MAX = 2_147_483_647;

/** Both bounds are inclusive: an expense ending today is still active. */
export function fixedExpenseStatus(
  { activeFrom, activeTo }: ActiveWindow,
  today: string
): FixedExpenseStatus {
  if (activeFrom != null && activeFrom > today) return "scheduled";
  if (activeTo != null && activeTo < today) return "ended";
  return "active";
}

const STATUS_RANK: Record<FixedExpenseStatus, number> = {
  active: 0,
  scheduled: 1,
  ended: 2,
};

/** Active first, then scheduled, then ended; stable within each group. */
export function sortByStatus<T extends ActiveWindow>(
  expenses: readonly T[],
  today: string
): T[] {
  return [...expenses].sort(
    (a, b) =>
      STATUS_RANK[fixedExpenseStatus(a, today)] -
      STATUS_RANK[fixedExpenseStatus(b, today)]
  );
}

export function sharedRatioValue(sharedRatio: string | null): number {
  return sharedRatio ? parseFloat(sharedRatio) : 0;
}

/** The ratio as a form input shows it ("0.6900" -> "0.69"); the default
 *  ratio when there is none. */
export function ratioInput(sharedRatio: string | null): string {
  return String(parseFloat(sharedRatio ?? DEFAULT_SHARED_RATIO));
}

export function fixedExpensePersonalAmount(
  expense: Pick<FixedExpense, "amount" | "isShared" | "sharedRatio">
): number {
  return calcPersonalAmount(
    expense.amount,
    expense.isShared,
    sharedRatioValue(expense.sharedRatio)
  );
}

/** Monthly totals over the expenses active on `today` only. */
export function fixedExpenseTotals(
  expenses: readonly Omit<FixedExpense, "id" | "name">[],
  today: string
): { personal: number; full: number; activeCount: number } {
  let personal = 0;
  let full = 0;
  let activeCount = 0;
  for (const expense of expenses) {
    if (fixedExpenseStatus(expense, today) !== "active") continue;
    personal += fixedExpensePersonalAmount(expense);
    full += expense.amount;
    activeCount += 1;
  }
  return { personal, full, activeCount };
}

/** "Sin límite", "Desde dd-mm-aaaa", "Hasta dd-mm-aaaa" or both dates. */
export function formatActiveWindow({
  activeFrom,
  activeTo,
}: ActiveWindow): string {
  if (activeFrom && activeTo) {
    return `${formatPlainDateEs(activeFrom)} al ${formatPlainDateEs(activeTo)}`;
  }
  if (activeFrom) return `Desde ${formatPlainDateEs(activeFrom)}`;
  if (activeTo) return `Hasta ${formatPlainDateEs(activeTo)}`;
  return "Sin límite";
}

/** A real calendar date in YYYY-MM-DD form (rejects 2026-02-30, 2026-9-1). */
export function isCalendarDate(value: unknown): value is string {
  return typeof value === "string" && isValidDay(value);
}

// ---------------------------------------------------------------------------
// PUT /api/fixed-expenses: the checks against the stored row (the body
// itself is validated by updateFixedExpenseSchema in lib/api/schemas)

/** The fields of a PUT body, as the columns store them (ratio as a string). */
export type FixedExpenseFields = Partial<{
  name: string;
  amount: number;
  isShared: boolean;
  sharedRatio: string | null;
  activeFrom: string | null;
  activeTo: string | null;
}>;

export type StoredFixedExpense = ActiveWindow & {
  isShared: boolean;
  sharedRatio: string | null;
};

function fail(error: string, field?: string): ValidationFailure {
  return { ok: false, status: 400, error, field };
}

function windowFailure(
  { activeFrom, activeTo }: ActiveWindow,
  field: "activeFrom" | "activeTo"
): ValidationFailure | null {
  if (activeFrom != null && activeTo != null && activeFrom > activeTo) {
    return fail("The active window must not end before it starts", field);
  }
  return null;
}

/**
 * Check an update against the stored row and return the values to write:
 * the merged active window must still be ordered (a lone `activeTo` before
 * the stored `activeFrom` is refused), an expense that ends up unshared
 * drops its ratio, and one that ends up shared without any ratio gets
 * DEFAULT_SHARED_RATIO (a null ratio would zero its personal amount).
 */
export function mergeFixedExpenseUpdate(
  existing: StoredFixedExpense,
  fields: FixedExpenseFields
): ValidationResult<FixedExpenseFields> {
  const merged: ActiveWindow = {
    activeFrom:
      fields.activeFrom !== undefined ? fields.activeFrom : existing.activeFrom,
    activeTo: fields.activeTo !== undefined ? fields.activeTo : existing.activeTo,
  };
  const failure = windowFailure(
    merged,
    fields.activeTo !== undefined ? "activeTo" : "activeFrom"
  );
  if (failure) return failure;

  const isShared = fields.isShared ?? existing.isShared;
  const ratio =
    fields.sharedRatio !== undefined ? fields.sharedRatio : existing.sharedRatio;
  const sharedRatio = isShared ? (ratio ?? DEFAULT_SHARED_RATIO) : null;

  const values: FixedExpenseFields = { ...fields };
  if (fields.sharedRatio !== undefined || sharedRatio !== existing.sharedRatio) {
    values.sharedRatio = sharedRatio;
  }
  return { ok: true, value: values };
}

// ---------------------------------------------------------------------------
// Client form parsing

export type FixedExpenseFormInput = {
  name: string;
  amount: string;
  isShared: boolean;
  sharedRatio: string;
  activeFrom: string;
  activeTo: string;
};

export type FixedExpenseBody = {
  name: string;
  amount: number;
  isShared: boolean;
  sharedRatio: number | null;
  activeFrom: string | null;
  activeTo: string | null;
};

export type FormResult =
  | { ok: true; body: FixedExpenseBody }
  | { ok: false; error: string };

/** Shared by the add form and the edit dialog; empty dates mean no bound. */
export function parseFixedExpenseForm(input: FixedExpenseFormInput): FormResult {
  const name = input.name.trim();
  if (!name) return { ok: false, error: "El nombre es obligatorio" };

  const rawAmount = input.amount.trim();
  const amount = Number(rawAmount);
  if (!rawAmount || !Number.isFinite(amount)) {
    return { ok: false, error: "El monto debe ser un número" };
  }
  const rounded = Math.round(amount);
  if (rounded < INT32_MIN || rounded > INT32_MAX) {
    return { ok: false, error: "El monto está fuera de rango" };
  }

  let sharedRatio: number | null = null;
  if (input.isShared) {
    const rawRatio = input.sharedRatio.trim();
    const ratio = Number(rawRatio);
    if (!rawRatio || !Number.isFinite(ratio) || ratio < 0 || ratio > 1) {
      return { ok: false, error: "El ratio debe estar entre 0 y 1" };
    }
    sharedRatio = ratio;
  }

  const activeFrom = input.activeFrom.trim() || null;
  const activeTo = input.activeTo.trim() || null;
  if (activeFrom != null && !isCalendarDate(activeFrom)) {
    return { ok: false, error: "La fecha de inicio no es válida" };
  }
  if (activeTo != null && !isCalendarDate(activeTo)) {
    return { ok: false, error: "La fecha de término no es válida" };
  }
  if (activeFrom != null && activeTo != null && activeFrom > activeTo) {
    return {
      ok: false,
      error: "La fecha de inicio no puede ser posterior a la de término",
    };
  }

  return {
    ok: true,
    body: {
      name,
      amount: rounded,
      isShared: input.isShared,
      sharedRatio,
      activeFrom,
      activeTo,
    },
  };
}
