// Monitor evaluation: resolve product references against a point-in-time
// context, convert currency-denominated values to the monitor currency, and
// fold thresholds into a status + margin. Pure: rates and product state
// always arrive via the context (no fetching here).

import {
  KIND_INFO,
  METRIC_FIELDS,
  type MetricDenomination,
  type ProductKind,
  type ProductMetrics,
} from "@chanchito/product-model";
import { ratesFor, type RateBook } from "@/lib/rates";
import { formatLocalDate } from "./dates";
import { undoDeltas, type UndoDeltas } from "./undo";
import {
  ExprError,
  collectRefs,
  parseExpression,
  type Expr,
  type RefExpr,
} from "./expr";
import type {
  Comparator,
  MonitorAdjustment,
  MonitorDefinition,
  MonitorEvaluation,
  MonitorStatus,
  ThresholdEvaluation,
} from "./types";

export type ProductInfo = {
  id: string;
  kind: ProductKind;
  currency: string;
  isActive: boolean;
  metrics: ProductMetrics | null;
  balanceAsOf: Date | null;
  slug: string;
  institutionSlug: string;
  name: string;
};

export type EvalContext = {
  /** Evaluation date; DAY_OF_MONTH/DAYS_IN_MONTH use its LOCAL date parts. */
  date: Date;
  products: Map<string, ProductInfo>;
  /** Provider rates plus institution/product overrides; each reference
   *  converts with its own product's rates (see lib/rates ratesFor). */
  rates: RateBook;
  /** Monitor currency: currency-denominated refs convert into it. */
  currency: string;
  /** Field deltas (product currency) added to refs before conversion, to
   *  evaluate as if some transactions never happened (see undo.ts). */
  undo?: UndoDeltas;
};

/** A value, or why it can't be computed (a Spanish, user-facing reason). */
export type EvalResult =
  | { ok: true; value: number }
  | { ok: false; reason: string };

// The dashboard is single-user, so date helpers follow local-time semantics;
// both helpers read the same local date parts to stay consistent.
function dayOfMonth(date: Date): number {
  return date.getDate();
}

function daysInMonth(date: Date): number {
  return new Date(date.getFullYear(), date.getMonth() + 1, 0).getDate();
}

/** The variaciones in effect on `date`: their transaction is dated in the
 *  same local calendar month, on or before that day, and not deleted. */
export function adjustmentsInEffect(
  adjustments: readonly MonitorAdjustment[] | undefined,
  date: Date
): MonitorAdjustment[] {
  if (adjustments == null || adjustments.length === 0) return [];
  const day = formatLocalDate(date);
  const month = day.slice(0, 7);
  return adjustments.filter(
    (a) =>
      !a.deleted &&
      a.transactionDate.slice(0, 7) === month &&
      a.transactionDate <= day
  );
}

/** A threshold shift: its value, or why it cannot be computed. `reason` is
 *  null when an expression already has no data without the undo (the
 *  evaluation reports that itself), and a Spanish reason when only the undo
 *  breaks it (e.g. a division by zero once a transaction is undone). */
export type ShiftResult =
  | { ok: true; value: number }
  | { ok: false; reason: string | null };

/** How much `expr` moves once `undo` applies: f(undone) - f(real). */
function undoneDelta(
  expr: Expr,
  base: EvalResult,
  undoCtx: EvalContext
): ShiftResult {
  if (!base.ok) return { ok: false, reason: null };
  const after = evaluateExpression(expr, undoCtx);
  if (!after.ok) {
    return {
      ok: false,
      reason: `No se pudieron aplicar las variaciones: ${after.reason}`,
    };
  }
  return { ok: true, value: after.value - base.value };
}

/**
 * The shift that ignoring `adjustments` means for one threshold, whatever
 * their date: `-[(L(u) - T(u)) - (L - T)]` in the monitor currency, where L
 * is the left expression, T the threshold (null: a constant one) and u the
 * refs with the transactions undone. Comparing the real L against T + shift
 * is then the same as comparing everything on the undone values. Exact for
 * any expression (not a derivative), so signs and coefficients like
 * `- owed * 0.7` come out right; 0 when nothing either side reads moves.
 * `bases` are the plain results when the caller already has them.
 */
export function thresholdShift(
  left: Expr,
  threshold: Expr | null,
  adjustments: readonly MonitorAdjustment[],
  ctx: EvalContext,
  bases: { left?: EvalResult; threshold?: EvalResult } = {}
): ShiftResult {
  const undo = undoDeltas(adjustments, (id) => ctx.products.get(id)?.kind);
  if (undo.size === 0) return { ok: true, value: 0 };
  const undoCtx = { ...ctx, undo };
  const leftDelta = undoneDelta(
    left,
    bases.left ?? evaluateExpression(left, ctx),
    undoCtx
  );
  if (!leftDelta.ok) return leftDelta;
  let thresholdDelta = 0;
  if (threshold != null) {
    const delta = undoneDelta(
      threshold,
      bases.threshold ?? evaluateExpression(threshold, ctx),
      undoCtx
    );
    if (!delta.ok) return delta;
    thresholdDelta = delta.value;
  }
  // Back to an 8-decimal scale, so float noise from the conversion and the
  // difference does not leak into thresholds (and -0 reads as 0).
  const value = Number((thresholdDelta - leftDelta.value).toFixed(8)) || 0;
  return { ok: true, value };
}

/** The shift on `ctx.date` for one threshold: the variaciones in effect that
 *  day (same-day ones together), see thresholdShift. */
export function adjustmentOnDate(
  adjustments: readonly MonitorAdjustment[] | undefined,
  left: Expr,
  threshold: Expr | null,
  ctx: EvalContext,
  bases: { left?: EvalResult; threshold?: EvalResult } = {}
): ShiftResult {
  const inEffect = adjustmentsInEffect(adjustments, ctx.date);
  if (inEffect.length === 0) return { ok: true, value: 0 };
  return thresholdShift(left, threshold, inEffect, ctx, bases);
}

/** The threshold the monitor-level shift and the per-variación effects
 *  report: the alert one, else the first. */
export function primaryThresholdIndex(
  thresholds: readonly { severity: string }[]
): number {
  const alert = thresholds.findIndex((t) => t.severity === "alert");
  return alert >= 0 ? alert : 0;
}

/** Metrics are a discriminated-union JSONB payload with dynamic field names,
 *  so read through a plain record with a runtime typeof check (the same
 *  narrowing idiom as lib/networth). Non-numbers count as missing. */
function readMetricField(metrics: ProductMetrics, field: string): number | null {
  const value = (metrics as unknown as Record<string, unknown>)[field];
  return typeof value === "number" ? value : null;
}

/** Human-readable name for a reference, for no-data reasons. */
function refLabel(ref: RefExpr, product?: ProductInfo): string {
  if (product) return `${product.institutionSlug}:${product.slug}:${ref.field}`;
  if (ref.institutionSlug != null && ref.productSlug != null) {
    return `${ref.institutionSlug}:${ref.productSlug}:${ref.field}`;
  }
  return `@{${ref.productId}:${ref.field}}`;
}

/** One reference's value in the monitor currency, as the evaluation sums it. */
export function resolveRef(ref: RefExpr, ctx: EvalContext): EvalResult {
  if (ref.productId == null) {
    return {
      ok: false,
      reason: `La referencia ${refLabel(ref)} no está asociada a un producto`,
    };
  }
  const product = ctx.products.get(ref.productId);
  if (!product) {
    return {
      ok: false,
      reason: `La referencia ${refLabel(ref)} apunta a un producto que ya no existe`,
    };
  }
  const label = refLabel(ref, product);
  if (!product.isActive) {
    return { ok: false, reason: `El producto ${label} está inactivo` };
  }

  const fieldInfo = METRIC_FIELDS[product.kind][ref.field];
  if (!fieldInfo) {
    return {
      ok: false,
      reason: `El campo '${ref.field}' no es válido para productos de tipo '${KIND_INFO[product.kind].labelEs}' (referencia ${label})`,
    };
  }
  const denomination: MetricDenomination = fieldInfo.denomination;
  if (product.metrics == null) {
    return {
      ok: false,
      reason: `Todavía no hay datos para ${label} (actualiza su institución)`,
    };
  }
  const observed = readMetricField(product.metrics, ref.field);
  if (observed == null) {
    return { ok: false, reason: `Falta el campo '${ref.field}' en ${label}` };
  }
  // Undone transactions move the raw value, before any conversion.
  const amount = observed + (ctx.undo?.get(product.id)?.[ref.field] ?? 0);

  // Only currency-denominated values convert; percent/count pass raw.
  if (denomination !== "currency") return { ok: true, value: amount };
  const productCurrency = product.currency.toUpperCase();
  const monitorCurrency = ctx.currency.toUpperCase();
  if (productCurrency === monitorCurrency) return { ok: true, value: amount };
  // Both legs come from the referenced product's rates, so an institution's
  // own rate converts its products whichever side of the cross it is on.
  const rates = ratesFor(ctx.rates, {
    productId: product.id,
    institutionSlug: product.institutionSlug,
  });
  const productRate = rates[productCurrency];
  if (productRate == null) {
    return {
      ok: false,
      reason: `No hay tipo de cambio para ${product.currency} (referencia ${label})`,
    };
  }
  const monitorRate = rates[monitorCurrency];
  if (monitorRate == null) {
    return {
      ok: false,
      reason: `No hay tipo de cambio para la moneda del monitor ${ctx.currency} (referencia ${label})`,
    };
  }
  // Cross rate through CLP: CLP-per-productCurrency over CLP-per-monitorCurrency.
  return { ok: true, value: (amount * productRate) / monitorRate };
}

/** Evaluate an AST against a context. Any missing input (unknown/inactive
 *  product, absent field, missing rate, division by zero) yields
 *  `{ ok: false, reason }`: no-data never silently becomes 0. */
export function evaluateExpression(expr: Expr, ctx: EvalContext): EvalResult {
  switch (expr.type) {
    case "number":
      return { ok: true, value: expr.value };
    case "func":
      return {
        ok: true,
        value:
          expr.name === "DAY_OF_MONTH"
            ? dayOfMonth(ctx.date)
            : daysInMonth(ctx.date),
      };
    case "ref":
      return resolveRef(expr, ctx);
    case "unary": {
      const operand = evaluateExpression(expr.operand, ctx);
      if (!operand.ok) return operand;
      return { ok: true, value: -operand.value };
    }
    case "binary": {
      const left = evaluateExpression(expr.left, ctx);
      if (!left.ok) return left;
      const right = evaluateExpression(expr.right, ctx);
      if (!right.ok) return right;
      switch (expr.op) {
        case "+":
          return { ok: true, value: left.value + right.value };
        case "-":
          return { ok: true, value: left.value - right.value };
        case "*":
          return { ok: true, value: left.value * right.value };
        case "/":
          if (right.value === 0) {
            return { ok: false, reason: "División por cero" };
          }
          return { ok: true, value: left.value / right.value };
      }
    }
  }
}

/** Whether `left cmp right` holds. The stored comparator is the breach
 *  condition; `=`/`!=` compare exactly (the builder warns on float metrics). */
export function comparatorHolds(
  cmp: Comparator,
  left: number,
  right: number
): boolean {
  switch (cmp) {
    case "<":
      return left < right;
    case "<=":
      return left <= right;
    case ">":
      return left > right;
    case ">=":
      return left >= right;
    case "=":
      return left === right;
    case "!=":
      return left !== right;
  }
}

/** Distance left before crossing: how much the value can still drop (for
 *  `<`/`<=`) or rise (for `>`/`>=`); null (hidden) for `=`/`!=`. */
function thresholdMargin(
  cmp: Comparator,
  value: number,
  threshold: number
): number | null {
  switch (cmp) {
    case "<":
    case "<=":
      return value - threshold;
    case ">":
    case ">=":
      return threshold - value;
    case "=":
    case "!=":
      return null;
  }
}

type ParsedSource =
  | { expr: Expr; error: null }
  | { expr: null; error: string };

function parseSource(source: string): ParsedSource {
  try {
    return { expr: parseExpression(source), error: null };
  } catch (e) {
    if (e instanceof ExprError) return { expr: null, error: e.message };
    throw e;
  }
}

/**
 * Evaluate a monitor (left expression + thresholds) at a point in time.
 * Every threshold value includes its own shift from the variaciones in
 * effect that day (see thresholdShift); the value itself stays the real one,
 * and `adjustment` reports the alert threshold's shift.
 * Status precedence: no_data (anything unresolvable, on either side) beats
 * breached (an alert threshold's condition holds) beats warning beats ok.
 */
export function evaluateMonitor(
  def: MonitorDefinition,
  ctx: EvalContext
): MonitorEvaluation {
  const leftParsed = parseSource(def.expression);
  const leftResult: EvalResult = leftParsed.expr
    ? evaluateExpression(leftParsed.expr, ctx)
    : { ok: false, reason: `Expresión inválida: ${leftParsed.error}` };

  let noDataReason = leftResult.ok ? null : leftResult.reason;
  const refs: RefExpr[] = leftParsed.expr ? collectRefs(leftParsed.expr) : [];

  let breached = false;
  let warned = false;
  const thresholds: ThresholdEvaluation[] = def.thresholds.map((t) => {
    const parsed = parseSource(t.expression);
    if (parsed.expr) refs.push(...collectRefs(parsed.expr));
    const result: EvalResult = parsed.expr
      ? evaluateExpression(parsed.expr, ctx)
      : { ok: false, reason: `Umbral inválido: ${parsed.error}` };
    if (!result.ok && noDataReason == null) noDataReason = result.reason;

    // Each threshold gets its own shift: it may read undone products too.
    let adjustment = 0;
    let value: number | null = null;
    if (result.ok) {
      const shift =
        leftParsed.expr && parsed.expr
          ? adjustmentOnDate(def.adjustments, leftParsed.expr, parsed.expr, ctx, {
              left: leftResult,
              threshold: result,
            })
          : ({ ok: true, value: 0 } as const);
      if (shift.ok) {
        adjustment = shift.value;
        value = result.value + adjustment;
      } else if (shift.reason == null) {
        // The left side has no data: already the reason; keep T unshifted.
        value = result.value;
      } else if (noDataReason == null) {
        noDataReason = shift.reason;
      }
    }

    let margin: number | null = null;
    if (leftResult.ok && value != null) {
      margin = thresholdMargin(t.comparator, leftResult.value, value);
      if (comparatorHolds(t.comparator, leftResult.value, value)) {
        if (t.severity === "alert") breached = true;
        else warned = true;
      }
    }
    return {
      severity: t.severity,
      comparator: t.comparator,
      value,
      margin,
      adjustment,
    };
  });
  const adjustment =
    thresholds[primaryThresholdIndex(thresholds)]?.adjustment ?? 0;

  const status: MonitorStatus =
    noDataReason != null
      ? "no_data"
      : breached
        ? "breached"
        : warned
          ? "warning"
          : "ok";

  // Monitor margin: the threshold nearest to crossing (minimum margin).
  const margins = thresholds
    .map((t) => t.margin)
    .filter((m): m is number => m != null);
  const margin =
    status === "no_data" || margins.length === 0 ? null : Math.min(...margins);

  // Staleness: the oldest observation among referenced products.
  const seen = new Set<string>();
  let oldest: Date | null = null;
  for (const ref of refs) {
    if (ref.productId == null || seen.has(ref.productId)) continue;
    seen.add(ref.productId);
    const product = ctx.products.get(ref.productId);
    if (product?.balanceAsOf && (oldest == null || product.balanceAsOf < oldest)) {
      oldest = product.balanceAsOf;
    }
  }

  return {
    status,
    value: leftResult.ok ? leftResult.value : null,
    thresholds,
    margin,
    adjustment,
    staleAsOf: oldest ? oldest.toISOString() : null,
    noDataReason,
  };
}
