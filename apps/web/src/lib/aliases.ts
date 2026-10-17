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

/** An alias write, validated and normalized. */
export type NormalizedAliasInput = {
  matchDescription: string;
  matchType: MatchType;
  alias: string;
  priority: number;
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

  if (partial && Object.keys(out).length === 0) {
    return fail(
      "Request body must include at least one of: matchDescription, matchType, alias, priority"
    );
  }
  return { ok: true, value: out };
}
