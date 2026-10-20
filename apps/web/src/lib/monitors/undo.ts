// Undoing a transaction on its product, for variaciones (V040): which metric
// fields a transaction moves, and in which direction. A transaction amount is
// signed in its product's currency (negative = expense or charge, positive =
// income or payment), so undoing it moves each field by `direction * amount`.
// Pure; the engine applies the deltas to refs before currency conversion (see
// evaluate.ts resolveRef), so a foreign-currency product's undo follows the
// live rate like the product's own value does.

import type { ProductKind } from "@chanchito/product-model";
import type { MonitorAdjustment } from "./types";

/** -1: the field rose with the amount (an asset), so undoing subtracts it.
 *  +1: the field fell with the amount (a debt), so undoing adds it back. */
export type UndoDirection = 1 | -1;

/** Per kind, the METRIC_FIELDS a transaction moves. Fields left out (limits,
 *  rates, percentages, counts, installment figures, crypto units whose
 *  movements are never stored as transactions) do not move. */
export const UNDO_DIRECTIONS: Record<
  ProductKind,
  Readonly<Record<string, UndoDirection>>
> = {
  // accounting_balance moves too: it is the same account's posted balance.
  checking: { balance: -1, accounting_balance: -1 },
  savings: { balance: -1, accounting_balance: -1 },
  vista: { balance: -1, accounting_balance: -1 },
  wallet: { balance: -1 },
  term_deposit: { balance: -1 },
  // A -X purchase raised owed by X and lowered available by X.
  credit_card: { owed: 1, available: -1 },
  debit_card: {},
  prepaid_card: { balance: -1 },
  line_of_credit: { owed: 1, available: -1 },
  // A +X payment lowered owed by X.
  loan: { owed: 1 },
  mortgage: { owed: 1 },
  // A +X deposit (or a stock buy) raised both the value and the deposits.
  investment: { nav: -1, deposited: -1 },
  stock: { nav: -1, deposited: -1 },
  crypto: {},
  other: { balance: -1 },
};

/** Per product id, the field deltas (product currency) to add to its refs. */
export type UndoDeltas = ReadonlyMap<string, Readonly<Record<string, number>>>;

/** Whether undoing a transaction of a `kind` product moves `field`. */
export function undoMoves(kind: ProductKind, field: string): boolean {
  return UNDO_DIRECTIONS[kind][field] != null;
}

/** The summed field deltas that undo `adjustments`; `kindOf` names each
 *  product's kind (unknown products contribute nothing). Deleted ones are
 *  skipped. */
export function undoDeltas(
  adjustments: readonly MonitorAdjustment[],
  kindOf: (productId: string) => ProductKind | undefined
): UndoDeltas {
  const deltas = new Map<string, Record<string, number>>();
  for (const adjustment of adjustments) {
    if (adjustment.deleted) continue;
    const kind = kindOf(adjustment.productId);
    if (kind == null) continue;
    const directions = Object.entries(UNDO_DIRECTIONS[kind]);
    if (directions.length === 0) continue;
    const fields = deltas.get(adjustment.productId) ?? {};
    for (const [field, direction] of directions) {
      fields[field] = (fields[field] ?? 0) + direction * adjustment.amount;
    }
    deltas.set(adjustment.productId, fields);
  }
  return deltas;
}
