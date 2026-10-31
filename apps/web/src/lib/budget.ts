// The monthly budget (Presupuesto): how the monthly income splits among the
// budget items (fixed_expenses rows, by kind) and what is left over. Pure, so
// the page can recompute it on every keystroke and it unit-tests in isolation.
//
// An item allocates its personal amount (the shared ratio applied, a negative
// one counting as 0) and counts only while it is active on `today`, the
// caller's local day (formatLocalDate).
// Shares are raw percents of the income (12.5 means 12,5%), null while the
// income is 0.

import { FIXED_EXPENSE_KINDS, type FixedExpenseKind } from "./db/schema";
import {
  DEFAULT_SHARED_RATIO,
  INT32_MAX,
  fixedExpensePersonalAmount,
  fixedExpenseStatus,
  isCalendarDate,
  type FixedExpense,
} from "./fixed-expenses";

/** Id of the draft item (the add form's unsaved values) in a summary. */
export const DRAFT_ITEM_ID = "draft";

/** An unsaved item previewed in the summary before it is added. */
export type BudgetDraft = Omit<FixedExpense, "id">;

export interface BudgetItemShare {
  id: string;
  name: string;
  kind: FixedExpenseKind;
  /** Personal amount, the shared ratio applied. */
  amount: number;
  share: number | null;
  draft: boolean;
}

export interface BudgetKindTotal {
  kind: FixedExpenseKind;
  /** Every active item of the kind, the draft included. */
  total: number;
  /** The draft's part of `total` (0 without a draft of this kind). */
  draftAmount: number;
  share: number | null;
  /** Largest first. */
  items: BudgetItemShare[];
}

export interface BudgetSummary {
  income: number;
  /** Sum of every kind's total. */
  allocated: number;
  allocatedShare: number | null;
  /** income - allocated, never below 0. */
  remaining: number;
  remainingShare: number | null;
  /** allocated - income when the allocations exceed the income, else 0. */
  deficit: number;
  deficitShare: number | null;
  /** Always the three kinds, in FIXED_EXPENSE_KINDS order. */
  kinds: BudgetKindTotal[];
}

/** `amount` as a percent of `income`, or null while there is no income. */
export function incomeShare(amount: number, income: number): number | null {
  return income > 0 ? (amount / income) * 100 : null;
}

export function budgetSummary(
  items: readonly FixedExpense[],
  income: number,
  today: string,
  draft?: BudgetDraft | null
): BudgetSummary {
  const all: (FixedExpense & { draft: boolean })[] = items.map((item) => ({
    ...item,
    draft: false,
  }));
  if (draft) all.push({ ...draft, id: DRAFT_ITEM_ID, draft: true });

  const kinds = FIXED_EXPENSE_KINDS.map((kind) => {
    const shares: BudgetItemShare[] = all
      .filter(
        (item) =>
          item.kind === kind && fixedExpenseStatus(item, today) === "active"
      )
      .map((item) => {
        // A negative amount (only legacy rows can hold one) would push Sobra
        // past the income, so it allocates nothing.
        const amount = Math.max(fixedExpensePersonalAmount(item), 0);
        return {
          id: item.id,
          name: item.name,
          kind,
          amount,
          share: incomeShare(amount, income),
          draft: item.draft,
        };
      })
      .sort((a, b) => b.amount - a.amount);
    const total = shares.reduce((sum, item) => sum + item.amount, 0);
    const draftAmount = shares
      .filter((item) => item.draft)
      .reduce((sum, item) => sum + item.amount, 0);
    return {
      kind,
      total,
      draftAmount,
      share: incomeShare(total, income),
      items: shares,
    };
  });

  const allocated = kinds.reduce((sum, kind) => sum + kind.total, 0);
  const remaining = Math.max(income - allocated, 0);
  const deficit = Math.max(allocated - income, 0);
  return {
    income,
    allocated,
    allocatedShare: incomeShare(allocated, income),
    remaining,
    remainingShare: incomeShare(remaining, income),
    deficit,
    deficitShare: incomeShare(deficit, income),
    kinds,
  };
}

/**
 * The add form's values as a draft to preview, or null while there is
 * nothing to show. The amount is read as parseFixedExpenseForm reads it
 * (Number, rounded, int32), and only one above 0 previews: 0 has nothing to
 * draw, and the form refuses a negative one. An empty name reads as "Nuevo
 * item"; a shared draft with a half-typed ratio previews at the default
 * ratio; bad dates count as no bound (the form will refuse them on Agregar).
 */
export function budgetDraft(input: {
  name: string;
  kind: FixedExpenseKind;
  amount: string;
  isShared: boolean;
  sharedRatio: string;
  activeFrom: string;
  activeTo: string;
}): BudgetDraft | null {
  const rawAmount = input.amount.trim();
  const amount = Math.round(Number(rawAmount));
  if (!rawAmount || !Number.isFinite(amount) || amount <= 0 || amount > INT32_MAX) {
    return null;
  }

  let sharedRatio: string | null = null;
  if (input.isShared) {
    const ratio = Number(input.sharedRatio.trim());
    sharedRatio =
      input.sharedRatio.trim() && Number.isFinite(ratio) && ratio >= 0 && ratio <= 1
        ? String(ratio)
        : DEFAULT_SHARED_RATIO;
  }

  const bound = (value: string) =>
    isCalendarDate(value.trim()) ? value.trim() : null;
  return {
    name: input.name.trim() || "Nuevo item",
    kind: input.kind,
    amount,
    isShared: input.isShared,
    sharedRatio,
    activeFrom: bound(input.activeFrom),
    activeTo: bound(input.activeTo),
  };
}

export type IncomeParse =
  | { ok: true; value: number }
  | { ok: false; reason: "format" | "range" };

/** Whole pesos: plain digits, or "." thousands groups ("2.500.000"); no
 *  leading zero, so "0.500" (a decimal, not 500) is refused. */
const INCOME_FORMAT = /^(0|[1-9]\d*)$|^[1-9]\d{0,2}(\.\d{3})+$/;
/** The start of an INCOME_FORMAT text, as it reads while being typed
 *  ("2.5" on the way to "2.500.000"). */
const INCOME_PREFIX = /^(0|[1-9]\d*)$|^[1-9]\d{0,2}(\.\d{3})*(\.\d{0,2})?$/;

function compactIncome(raw: string): string {
  return raw.replace(/[$\s]/g, "");
}

/**
 * The income field's text as whole CLP. Empty means 0; "$" and spaces are
 * ignored, so "2.500.000", "$ 2.500.000" and "2500000" all read as 2500000.
 * Anything else (decimals, a leading zero, a sign) fails with "format";
 * past the int32 column with "range".
 */
export function parseIncomeInput(raw: string): IncomeParse {
  const text = compactIncome(raw);
  if (!text) return { ok: true, value: 0 };
  if (!INCOME_FORMAT.test(text)) return { ok: false, reason: "format" };
  const value = Number(text.replace(/\./g, ""));
  return value <= INT32_MAX
    ? { ok: true, value }
    : { ok: false, reason: "range" };
}

/** Whether the text is valid or still on its way to a valid amount, so the
 *  field holds back its error while the user types. */
export function isIncomePrefix(raw: string): boolean {
  const text = compactIncome(raw);
  return !text || INCOME_PREFIX.test(text);
}

/** Whole CLP with es-CL thousands separators and no symbol ("2.500.000"). */
export function formatIncomeInput(value: number): string {
  return new Intl.NumberFormat("es-CL", { maximumFractionDigits: 0 }).format(
    value
  );
}
