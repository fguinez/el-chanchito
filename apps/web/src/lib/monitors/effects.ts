// Variación effects (V040): which products a monitor reads, and what each
// linked transaction moves in its thresholds right now. Pure; the API routes
// call it with today's catalog and rates.

import type { ProductKind } from "@chanchito/product-model";
import type { ApiMonitorAdjustment, MonitorAdjustmentRow } from "./adjustments";
import {
  primaryThresholdIndex,
  thresholdShift,
  type EvalContext,
} from "./evaluate";
import { ExprError, collectRefs, parseExpression, type Expr } from "./expr";
import type { MonitorAdjustment } from "./types";
import { undoMoves } from "./undo";

/** The parsed expression, or null when it does not parse. */
function parseOrNull(expression: string): Expr | null {
  try {
    return parseExpression(expression);
  } catch (e) {
    if (e instanceof ExprError) return null;
    throw e;
  }
}

/** The monitor parts a variación is evaluated against (stored uuid form). */
export type MonitorExpressions = {
  expression: string;
  thresholds: readonly { severity: string; expression: string }[];
};

/** Every expression of a monitor that parses: the left one, then each
 *  threshold (null where one does not parse). */
function parseAll(monitor: MonitorExpressions): (Expr | null)[] {
  return [monitor.expression, ...monitor.thresholds.map((t) => t.expression)].map(
    parseOrNull
  );
}

/** Ids of the products a monitor references, in its left expression or any
 *  threshold: the only ones whose transactions can be variaciones of it. */
export function monitorProductIds(monitor: MonitorExpressions): string[] {
  const ids = new Set<string>();
  for (const expr of parseAll(monitor)) {
    if (expr == null) continue;
    for (const ref of collectRefs(expr)) {
      if (ref.productId != null) ids.add(ref.productId);
    }
  }
  return [...ids];
}

/** Whether undoing a transaction of `productId` moves any field the monitor
 *  reads (left side or a threshold). */
export function affectsMonitor(
  monitor: MonitorExpressions,
  productId: string,
  kind: ProductKind | undefined
): boolean {
  if (kind == null) return false;
  return parseAll(monitor).some(
    (expr) =>
      expr != null &&
      collectRefs(expr).some(
        (ref) => ref.productId === productId && undoMoves(kind, ref.field)
      )
  );
}

/**
 * Each row's current effect on the monitor's primary threshold (the alert
 * one, as the evaluation's `adjustment`), evaluated with `ctx` (today's
 * products and rates) whatever the row's month, so a past month still shows
 * what its variaciones would move now. A variación of a product the monitor
 * does not read has effect 0; null when there is no data to compute it.
 */
export function withEffects(
  rows: readonly MonitorAdjustmentRow[],
  monitor: MonitorExpressions,
  ctx: EvalContext
): ApiMonitorAdjustment[] {
  const left = parseOrNull(monitor.expression);
  const primary = monitor.thresholds[primaryThresholdIndex(monitor.thresholds)];
  const threshold = primary ? parseOrNull(primary.expression) : null;
  const usable = left != null && (primary == null || threshold != null);
  // One combined shift per day, shared by that day's rows.
  const monthToDate = new Map<string, number | null>();
  const live = rows.filter((row) => !row.deleted);
  const shift = (adjustments: readonly MonitorAdjustment[]) => {
    if (!usable) return null;
    const result = thresholdShift(left, threshold, adjustments, ctx);
    return result.ok ? result.value : null;
  };

  return rows.map((row) => {
    const kind = ctx.products.get(row.productId)?.kind;
    const affects = affectsMonitor(monitor, row.productId, kind);
    if (row.deleted) {
      return { ...row, affects, effect: null, monthToDateEffect: null };
    }
    const day = row.transactionDate;
    if (!monthToDate.has(day)) {
      const month = day.slice(0, 7);
      monthToDate.set(
        day,
        shift(
          live.filter(
            (a) => a.transactionDate.slice(0, 7) === month && a.transactionDate <= day
          )
        )
      );
    }
    return {
      ...row,
      affects,
      effect: shift([row]),
      monthToDateEffect: monthToDate.get(day) ?? null,
    };
  });
}
