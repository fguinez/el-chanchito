// The monthly budget (Presupuesto): how the monthly income splits among the
// budget items (fixed_expenses rows, by kind) and what is left over. Pure, so
// the page can recompute it on every keystroke and it unit-tests in isolation.
//
// An item allocates its personal amount (the shared ratio applied) and counts
// only while it is active on `today`, the caller's local day (formatLocalDate).
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
        const amount = fixedExpensePersonalAmount(item);
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
 * nothing to show: no usable amount yet (empty, not a number, not above 0,
 * out of range). An empty name reads as "Nuevo item"; a shared draft with a
 * half-typed ratio previews at the default ratio. Unlike
 * parseFixedExpenseForm it never fails: bad dates count as no bound.
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

/**
 * The income field's text as whole CLP, or null when it is not one. Empty
 * means 0; "$", spaces and "." thousands separators are ignored, so
 * "2.500.000" and "$ 2.500.000" read as 2500000. A decimal comma or point
 * followed by other than three digits is refused rather than guessed.
 */
export function parseIncomeInput(raw: string): number | null {
  const text = raw.replace(/[$\s]/g, "");
  if (!text) return 0;
  if (!/^\d{1,3}(\.\d{3})*$|^\d+$/.test(text)) return null;
  const value = Number(text.replace(/\./g, ""));
  return Number.isSafeInteger(value) && value <= INT32_MAX ? value : null;
}

/** Whole CLP with es-CL thousands separators and no symbol ("2.500.000"). */
export function formatIncomeInput(value: number): string {
  return new Intl.NumberFormat("es-CL", { maximumFractionDigits: 0 }).format(
    value
  );
}
