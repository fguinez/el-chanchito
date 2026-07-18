// Request validation for the monitors API. Pure (no db, no next imports) so
// it unit-tests in isolation: the product catalog arrives as a parameter and
// every failure already carries the HTTP shape the routes return
// (`{ error, field?, position? }` with status 400).
//
// Expressions are accepted in display form (institution:product:field), the
// stored uuid-ref form, or a mix, and always normalized to the stored form:
// parse -> bind slug refs to product uuids -> validate every reference
// against the catalog -> serialize back in uuid mode.

import {
  ExprError,
  bindExpression,
  parseExpression,
  serializeExpression,
  validateExpression,
  type ProductCatalog,
} from "./expr";
import type {
  Comparator,
  MonitorDisplay,
  MonitorThreshold,
  ThresholdSeverity,
} from "./types";

/** A monitor write, validated and normalized (expressions in stored form).
 *  Create mode returns all fields (defaults applied); partial mode returns
 *  only the fields present in the body. */
export type NormalizedMonitorInput = {
  name: string;
  description: string | null;
  currency: string;
  expression: string;
  thresholds: MonitorThreshold[];
  display: MonitorDisplay;
  isActive: boolean;
};

export type ValidationFailure = {
  ok: false;
  status: 400;
  error: string;
  field?: string;
  /** 0-based character offset into the offending expression, when known. */
  position?: number;
};

export type ValidationResult<T> = { ok: true; value: T } | ValidationFailure;

const SEVERITIES: readonly ThresholdSeverity[] = ["alert", "warning"];
const COMPARATORS: readonly Comparator[] = ["<", "<=", ">", ">=", "=", "!="];
const CURRENCY_RE = /^[A-Z]{3,}$/;

const DEFAULT_DISPLAY: MonitorDisplay = { chart: "line", show_margin: true };

function fail(
  error: string,
  field?: string,
  position?: number
): ValidationFailure {
  return { ok: false, status: 400, error, field, position };
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Parse -> bind display refs to uuids -> validate refs against the catalog
 *  -> serialize to the stored (uuid-ref) form. */
function normalizeExpression(
  source: unknown,
  catalog: ProductCatalog,
  field: string
): ValidationResult<string> {
  if (typeof source !== "string") {
    return fail(`Field '${field}' must be an expression string`, field);
  }
  try {
    const bound = bindExpression(parseExpression(source), catalog);
    const issues = validateExpression(bound, catalog);
    if (issues.length > 0) {
      return fail(issues[0].message, field, issues[0].position);
    }
    return { ok: true, value: serializeExpression(bound, "uuid") };
  } catch (e) {
    if (e instanceof ExprError) return fail(e.message, field, e.position);
    throw e;
  }
}

function normalizeThresholds(
  raw: unknown,
  catalog: ProductCatalog
): ValidationResult<MonitorThreshold[]> {
  if (!Array.isArray(raw) || raw.length === 0) {
    return fail("At least one threshold is required", "thresholds");
  }
  const out: MonitorThreshold[] = [];
  const seen = new Set<ThresholdSeverity>();
  for (let i = 0; i < raw.length; i++) {
    const item: unknown = raw[i];
    if (!isPlainObject(item)) {
      return fail(`Threshold ${i} must be an object`, `thresholds[${i}]`);
    }
    const severity = item.severity;
    if (
      typeof severity !== "string" ||
      !SEVERITIES.includes(severity as ThresholdSeverity)
    ) {
      return fail(
        "Invalid severity: expected 'alert' or 'warning'",
        `thresholds[${i}].severity`
      );
    }
    const comparator = item.comparator;
    if (
      typeof comparator !== "string" ||
      !COMPARATORS.includes(comparator as Comparator)
    ) {
      return fail(
        "Invalid comparator: expected one of <, <=, >, >=, =, !=",
        `thresholds[${i}].comparator`
      );
    }
    if (seen.has(severity as ThresholdSeverity)) {
      return fail(`Duplicate '${severity}' threshold`, "thresholds");
    }
    seen.add(severity as ThresholdSeverity);

    const expression = normalizeExpression(
      item.expression,
      catalog,
      `thresholds[${i}].expression`
    );
    if (!expression.ok) return expression;
    out.push({
      severity: severity as ThresholdSeverity,
      comparator: comparator as Comparator,
      expression: expression.value,
    });
  }
  if (!seen.has("alert")) {
    return fail("An 'alert' threshold is required", "thresholds");
  }
  return { ok: true, value: out };
}

function normalizeDisplay(raw: unknown): ValidationResult<MonitorDisplay> {
  if (raw === undefined) return { ok: true, value: { ...DEFAULT_DISPLAY } };
  if (!isPlainObject(raw)) {
    return fail("Field 'display' must be an object", "display");
  }
  const { chart, show_margin: showMargin } = raw;
  if (chart !== undefined && chart !== "line" && chart !== "stat") {
    return fail("Invalid display.chart: expected 'line' or 'stat'", "display");
  }
  if (showMargin !== undefined && typeof showMargin !== "boolean") {
    return fail("Invalid display.show_margin: expected a boolean", "display");
  }
  return {
    ok: true,
    value: {
      chart: chart === "stat" ? "stat" : "line",
      show_margin: typeof showMargin === "boolean" ? showMargin : true,
    },
  };
}

/**
 * Validate and normalize a monitor write. Create mode (default) requires
 * name, expression, and thresholds, and fills the defaults (currency CLP,
 * line chart with margin, active). `{ partial: true }` validates only the
 * fields present, for PUT; a partial body that includes `thresholds` must
 * still carry an `alert` threshold.
 */
export function validateMonitorInput(
  body: unknown,
  catalog: ProductCatalog
): ValidationResult<NormalizedMonitorInput>;
export function validateMonitorInput(
  body: unknown,
  catalog: ProductCatalog,
  opts: { partial: true }
): ValidationResult<Partial<NormalizedMonitorInput>>;
export function validateMonitorInput(
  body: unknown,
  catalog: ProductCatalog,
  opts?: { partial?: boolean }
): ValidationResult<Partial<NormalizedMonitorInput>> {
  const partial = opts?.partial === true;
  if (!isPlainObject(body)) {
    return fail("Request body must be a JSON object");
  }
  const out: Partial<NormalizedMonitorInput> = {};

  if (body.name !== undefined) {
    if (typeof body.name !== "string" || body.name.trim() === "") {
      return fail("Field 'name' must be a non-empty string", "name");
    }
    out.name = body.name.trim();
  } else if (!partial) {
    return fail("Field 'name' is required", "name");
  }

  if (body.description !== undefined) {
    if (body.description !== null && typeof body.description !== "string") {
      return fail(
        "Field 'description' must be a string or null",
        "description"
      );
    }
    out.description = body.description;
  } else if (!partial) {
    out.description = null;
  }

  if (body.currency !== undefined) {
    const currency =
      typeof body.currency === "string"
        ? body.currency.trim().toUpperCase()
        : null;
    if (currency == null || !CURRENCY_RE.test(currency)) {
      return fail(
        "Field 'currency' must be a currency code (3+ letters)",
        "currency"
      );
    }
    out.currency = currency;
  } else if (!partial) {
    out.currency = "CLP";
  }

  if (body.expression !== undefined || !partial) {
    const expression = normalizeExpression(
      body.expression,
      catalog,
      "expression"
    );
    if (!expression.ok) return expression;
    out.expression = expression.value;
  }

  if (body.thresholds !== undefined || !partial) {
    const thresholds = normalizeThresholds(body.thresholds, catalog);
    if (!thresholds.ok) return thresholds;
    out.thresholds = thresholds.value;
  }

  if (body.display !== undefined || !partial) {
    const display = normalizeDisplay(body.display);
    if (!display.ok) return display;
    out.display = display.value;
  }

  if (body.isActive !== undefined) {
    if (typeof body.isActive !== "boolean") {
      return fail("Field 'isActive' must be a boolean", "isActive");
    }
    out.isActive = body.isActive;
  } else if (!partial) {
    out.isActive = true;
  }

  return { ok: true, value: out };
}
