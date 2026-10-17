// Request validation for the aliases API. Pure (no db, no next imports) so it
// unit-tests in isolation and the UI can share the limits. Every failure
// carries the HTTP shape the routes return (`{ error, field? }` with status
// 400). Matching itself lives in Postgres (`alias_for_transaction`, V035).
//
// The shared primitives (UUID_RE, isUuid, failureBody, the validation types,
// the priority bounds) come from lib/categories so the two validators stay in
// step; aliases reuse the same priority range as rules.

import {
  RULE_PRIORITY_MAX,
  RULE_PRIORITY_MIN,
  type ValidationFailure,
  type ValidationResult,
} from "@/lib/categories";
import { MATCH_TYPES, type MatchType } from "@/lib/db/schema";

export { UUID_RE, isUuid, failureBody } from "@/lib/categories";
export { MATCH_TYPES, type MatchType } from "@/lib/db/schema";

export const MATCH_DESCRIPTION_MAX_LENGTH = 500;
export const ALIAS_MAX_LENGTH = 200;
// Aliases share the category rules' priority range (see lib/categories), so the
// shared form helpers (parsePriority, PRIORITY_ERROR) apply unchanged.
export const ALIAS_PRIORITY_MIN = RULE_PRIORITY_MIN;
export const ALIAS_PRIORITY_MAX = RULE_PRIORITY_MAX;
// Amount bounds stay in the same magnitude as lib/api/schemas' moneyAmount, so
// `n * 100` stays an exact integer and the value sits far above any real figure.
export const AMOUNT_BOUND_LIMIT = 1e13;
// A currency code is short; the DB column is TEXT (no enum) so any currency a
// product carries (CLP, USD, EUR, BTC, ...) is accepted, scoped by the CHECK in
// V036, not by a hard-coded list. 12 chars covers ISO codes and crypto tickers.
export const CURRENCY_MAX_LENGTH = 12;

/** An alias write, validated and normalized. */
export type NormalizedAliasInput = {
  matchDescription: string;
  matchType: MatchType;
  alias: string;
  priority: number;
  // Optional amount condition (V036). null = unbounded on that side / no scope.
  amountMin: number | null;
  amountMax: number | null;
  currency: string | null;
};

function fail(error: string, field?: string): ValidationFailure {
  return { ok: false, status: 400, error, field };
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Trim the matched description; case is preserved (the function lowercases at
 *  match time). 1..MATCH_DESCRIPTION_MAX_LENGTH after trimming. Shared by alias
 *  writes and the preview so both match the same text. */
export function normalizeMatchDescription(raw: unknown): ValidationResult<string> {
  const value = typeof raw === "string" ? raw.trim() : "";
  if (value === "" || value.length > MATCH_DESCRIPTION_MAX_LENGTH) {
    return fail(
      `Field 'matchDescription' must be a string of 1 to ${MATCH_DESCRIPTION_MAX_LENGTH} characters`,
      "matchDescription"
    );
  }
  return { ok: true, value };
}

/** Trim the alias label; 1..ALIAS_MAX_LENGTH after trimming. */
export function normalizeAlias(raw: unknown): ValidationResult<string> {
  const value = typeof raw === "string" ? raw.trim() : "";
  if (value === "" || value.length > ALIAS_MAX_LENGTH) {
    return fail(
      `Field 'alias' must be a string of 1 to ${ALIAS_MAX_LENGTH} characters`,
      "alias"
    );
  }
  return { ok: true, value };
}

/** One of MATCH_TYPES; an absent value (undefined/null/"") defaults to
 *  'exact', so create mode and the preview share the default. */
export function normalizeMatchType(raw: unknown): ValidationResult<MatchType> {
  if (raw === undefined || raw === null || raw === "") {
    return { ok: true, value: "exact" };
  }
  if (
    typeof raw !== "string" ||
    !(MATCH_TYPES as readonly string[]).includes(raw)
  ) {
    return fail(
      `Field 'matchType' must be one of: ${MATCH_TYPES.join(", ")}`,
      "matchType"
    );
  }
  return { ok: true, value: raw as MatchType };
}

function normalizePriority(raw: unknown): ValidationResult<number> {
  if (
    typeof raw !== "number" ||
    !Number.isInteger(raw) ||
    raw < ALIAS_PRIORITY_MIN ||
    raw > ALIAS_PRIORITY_MAX
  ) {
    return fail(
      `Field 'priority' must be an integer from ${ALIAS_PRIORITY_MIN} to ${ALIAS_PRIORITY_MAX}`,
      "priority"
    );
  }
  return { ok: true, value: raw };
}

/** One amount bound: null (and absent/blank) passes as "no bound"; otherwise a
 *  finite number rounded to 2 decimals (the NUMERIC(18,2) column) and within
 *  +/- AMOUNT_BOUND_LIMIT. Accepts a JSON number (POST/PATCH body) or a numeric
 *  string (preview query param), so writes and the preview share one check. The
 *  `field` names the offending bound (amountMin/amountMax) in the error. */
export function normalizeAmountBound(
  raw: unknown,
  field = "amount"
): ValidationResult<number | null> {
  if (raw === undefined || raw === null) return { ok: true, value: null };
  let num: number;
  if (typeof raw === "number") {
    num = raw;
  } else if (typeof raw === "string") {
    const trimmed = raw.trim();
    if (trimmed === "") return { ok: true, value: null };
    num = Number(trimmed);
  } else {
    return fail(`Field '${field}' must be a number or null`, field);
  }
  if (!Number.isFinite(num)) {
    return fail(`Field '${field}' must be a finite number`, field);
  }
  const rounded = Math.round(num * 100) / 100;
  if (rounded < -AMOUNT_BOUND_LIMIT || rounded > AMOUNT_BOUND_LIMIT) {
    return fail(
      `Field '${field}' must be between ${-AMOUNT_BOUND_LIMIT} and ${AMOUNT_BOUND_LIMIT}`,
      field
    );
  }
  return { ok: true, value: rounded };
}

/** A currency scope: null (and absent/blank) passes; otherwise a trimmed,
 *  uppercased, non-empty code of at most CURRENCY_MAX_LENGTH characters. Not
 *  restricted to a fixed list: the column is TEXT and products carry many
 *  currencies; V036's CHECK only requires one to be present when a bound is. */
export function normalizeCurrency(
  raw: unknown,
  field = "currency"
): ValidationResult<string | null> {
  if (raw === undefined || raw === null) return { ok: true, value: null };
  if (typeof raw !== "string") {
    return fail(`Field '${field}' must be a string or null`, field);
  }
  const value = raw.trim().toUpperCase();
  if (value === "") return { ok: true, value: null };
  if (value.length > CURRENCY_MAX_LENGTH) {
    return fail(
      `Field '${field}' must be at most ${CURRENCY_MAX_LENGTH} characters`,
      field
    );
  }
  return { ok: true, value };
}

/**
 * Cross-field check for a COMPLETE amount condition (CREATE, or a PATCH merged
 * over the stored row). Returns a failure, or null when the condition holds:
 *   - both bounds present and amountMin > amountMax -> fail on 'amountMax'.
 *   - any bound present and no currency -> fail on 'currency'.
 * It mirrors V036's two CHECK constraints, so the API answers a friendly 400
 * instead of letting the write hit a DB CHECK 500.
 */
export function amountConditionError(input: {
  amountMin: number | null;
  amountMax: number | null;
  currency: string | null;
}): ValidationFailure | null {
  const { amountMin, amountMax, currency } = input;
  if (amountMin !== null && amountMax !== null && amountMin > amountMax) {
    return fail(
      "'amountMax' must be greater than or equal to 'amountMin'",
      "amountMax"
    );
  }
  if (
    (amountMin !== null || amountMax !== null) &&
    (currency === null || currency === "")
  ) {
    return fail(
      "'currency' is required when an amount condition is set",
      "currency"
    );
  }
  return null;
}

/**
 * Validate and normalize an alias write. Create mode (default) requires
 * `matchDescription` and `alias`, defaults `matchType` to 'exact' and
 * `priority` to 0. `{ partial: true }` validates only the fields present, for
 * PATCH, and rejects a body with none.
 */
export function validateAliasInput(
  body: unknown
): ValidationResult<NormalizedAliasInput>;
export function validateAliasInput(
  body: unknown,
  opts: { partial: true }
): ValidationResult<Partial<NormalizedAliasInput>>;
export function validateAliasInput(
  body: unknown,
  opts?: { partial?: boolean }
): ValidationResult<Partial<NormalizedAliasInput>> {
  const partial = opts?.partial === true;
  if (!isPlainObject(body)) {
    return fail("Request body must be a JSON object");
  }
  const out: Partial<NormalizedAliasInput> = {};

  if (body.matchDescription !== undefined || !partial) {
    if (body.matchDescription === undefined) {
      return fail("Field 'matchDescription' is required", "matchDescription");
    }
    const matchDescription = normalizeMatchDescription(body.matchDescription);
    if (!matchDescription.ok) return matchDescription;
    out.matchDescription = matchDescription.value;
  }

  if (body.alias !== undefined || !partial) {
    if (body.alias === undefined) {
      return fail("Field 'alias' is required", "alias");
    }
    const alias = normalizeAlias(body.alias);
    if (!alias.ok) return alias;
    out.alias = alias.value;
  }

  if (body.matchType !== undefined) {
    const matchType = normalizeMatchType(body.matchType);
    if (!matchType.ok) return matchType;
    out.matchType = matchType.value;
  } else if (!partial) {
    out.matchType = "exact";
  }

  if (body.priority !== undefined) {
    const priority = normalizePriority(body.priority);
    if (!priority.ok) return priority;
    out.priority = priority.value;
  } else if (!partial) {
    out.priority = 0;
  }

  if (body.amountMin !== undefined) {
    const amountMin = normalizeAmountBound(body.amountMin, "amountMin");
    if (!amountMin.ok) return amountMin;
    out.amountMin = amountMin.value;
  } else if (!partial) {
    out.amountMin = null;
  }

  if (body.amountMax !== undefined) {
    const amountMax = normalizeAmountBound(body.amountMax, "amountMax");
    if (!amountMax.ok) return amountMax;
    out.amountMax = amountMax.value;
  } else if (!partial) {
    out.amountMax = null;
  }

  if (body.currency !== undefined) {
    const currency = normalizeCurrency(body.currency);
    if (!currency.ok) return currency;
    out.currency = currency.value;
  } else if (!partial) {
    out.currency = null;
  }

  if (partial && Object.keys(out).length === 0) {
    return fail(
      "Request body must include at least one of: matchDescription, matchType, " +
        "alias, priority, amountMin, amountMax, currency"
    );
  }

  if (partial) {
    // The row is incomplete here: the full currency-requires-a-bound check waits
    // for the PATCH route to merge with the stored row (amountConditionError).
    // Only the order of two bounds BOTH present in the patch is safe to check.
    if (
      out.amountMin != null &&
      out.amountMax != null &&
      out.amountMin > out.amountMax
    ) {
      return fail(
        "'amountMax' must be greater than or equal to 'amountMin'",
        "amountMax"
      );
    }
  } else {
    const conditionError = amountConditionError({
      amountMin: out.amountMin ?? null,
      amountMax: out.amountMax ?? null,
      currency: out.currency ?? null,
    });
    if (conditionError) return conditionError;
  }

  return { ok: true, value: out };
}
