// Monitor evaluation: resolve product references against a point-in-time
// context, convert currency-denominated values to the monitor currency, and
// fold thresholds into a status + margin. Pure: rates and product state
// always arrive via the context (no fetching here).

import {
  METRIC_FIELDS,
  type MetricDenomination,
  type ProductKind,
  type ProductMetrics,
} from "@chanchito/product-model";
import type { ClpRates } from "@/lib/rates";
import {
  ExprError,
  collectRefs,
  parseExpression,
  type Expr,
  type RefExpr,
} from "./expr";
import type {
  Comparator,
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
  /** CLP per unit of each currency (see lib/rates getClpRates). */
  rates: ClpRates;
  /** Monitor currency: currency-denominated refs convert into it. */
  currency: string;
};

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

function resolveRef(ref: RefExpr, ctx: EvalContext): EvalResult {
  if (ref.productId == null) {
    return {
      ok: false,
      reason: `Reference ${refLabel(ref)} is not bound to a product`,
    };
  }
  const product = ctx.products.get(ref.productId);
  if (!product) {
    return {
      ok: false,
      reason: `Unknown product for reference ${refLabel(ref)}`,
    };
  }
  const label = refLabel(ref, product);
  if (!product.isActive) {
    return { ok: false, reason: `Product ${label} is inactive` };
  }

  const fieldInfo = METRIC_FIELDS[product.kind][ref.field];
  if (!fieldInfo) {
    return {
      ok: false,
      reason: `Field '${ref.field}' is not valid for kind '${product.kind}' (reference ${label})`,
    };
  }
  const denomination: MetricDenomination = fieldInfo.denomination;
  if (product.metrics == null) {
    return { ok: false, reason: `Product ${label} has no metrics` };
  }
  const amount = readMetricField(product.metrics, ref.field);
  if (amount == null) {
    return { ok: false, reason: `Field '${ref.field}' is missing for ${label}` };
  }

  // Only currency-denominated values convert; percent/count pass raw.
  if (denomination !== "currency") return { ok: true, value: amount };
  const productCurrency = product.currency.toUpperCase();
  const monitorCurrency = ctx.currency.toUpperCase();
  if (productCurrency === monitorCurrency) return { ok: true, value: amount };
  const productRate = ctx.rates[productCurrency];
  if (productRate == null) {
    return {
      ok: false,
      reason: `No rate for ${product.currency} (reference ${label})`,
    };
  }
  const monitorRate = ctx.rates[monitorCurrency];
  if (monitorRate == null) {
    return {
      ok: false,
      reason: `No rate for monitor currency ${ctx.currency} (reference ${label})`,
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
            return { ok: false, reason: "Division by zero" };
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
    : { ok: false, reason: `Invalid expression: ${leftParsed.error}` };

  let noDataReason = leftResult.ok ? null : leftResult.reason;
  const refs: RefExpr[] = leftParsed.expr ? collectRefs(leftParsed.expr) : [];

  let breached = false;
  let warned = false;
  const thresholds: ThresholdEvaluation[] = def.thresholds.map((t) => {
    const parsed = parseSource(t.expression);
    if (parsed.expr) refs.push(...collectRefs(parsed.expr));
    const result: EvalResult = parsed.expr
      ? evaluateExpression(parsed.expr, ctx)
      : { ok: false, reason: `Invalid threshold expression: ${parsed.error}` };
    if (!result.ok && noDataReason == null) noDataReason = result.reason;

    let margin: number | null = null;
    if (leftResult.ok && result.ok) {
      margin = thresholdMargin(t.comparator, leftResult.value, result.value);
      if (comparatorHolds(t.comparator, leftResult.value, result.value)) {
        if (t.severity === "alert") breached = true;
        else warned = true;
      }
    }
    return {
      severity: t.severity,
      comparator: t.comparator,
      value: result.ok ? result.value : null,
      margin,
    };
  });

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
    staleAsOf: oldest ? oldest.toISOString() : null,
    noDataReason,
  };
}
