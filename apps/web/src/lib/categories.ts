// Request validation for the categories API. Pure (no db, no next imports) so
// it unit-tests in isolation and the UI can share the icon list and limits.
// Every failure already carries the HTTP shape the routes return
// (`{ error, field? }` with status 400).
//
// Rule keywords are normalized (trimmed, lowercased) the same way on write and
// in the preview, so "probar regla" counts exactly what the rule will match.
// Matching itself lives in Postgres (`category_for_description`, V021).

export const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export const CATEGORY_NAME_MAX_LENGTH = 50;
export const RULE_KEYWORD_MAX_LENGTH = 100;
export const RULE_PRIORITY_MIN = -1000;
export const RULE_PRIORITY_MAX = 1000;

/** Icons a category may use: kebab-case lucide names, the first eight being
 *  the V007 seeds. The UI maps each name to its lucide component. */
export const CATEGORY_ICON_NAMES = [
  "shopping-cart",
  "car",
  "utensils",
  "gamepad-2",
  "heart-pulse",
  "settings",
  "arrow-left-right",
  "circle",
  "house",
  "zap",
  "wifi",
  "smartphone",
  "fuel",
  "plane",
  "coffee",
  "shirt",
  "pill",
  "graduation-cap",
  "gift",
  "paw-print",
  "piggy-bank",
  "receipt",
  "briefcase",
] as const;

export type CategoryIconName = (typeof CATEGORY_ICON_NAMES)[number];

const HEX_COLOR_RE = /^#[0-9a-f]{6}$/;

/** A category write, validated and normalized. Create mode returns every
 *  field (color and icon default to null); partial mode returns only the
 *  fields present in the body. */
export type NormalizedCategoryInput = {
  name: string;
  color: string | null;
  icon: CategoryIconName | null;
};

/** A rule write, validated and normalized (keyword trimmed and lowercased).
 *  Create mode defaults priority to 0. */
export type NormalizedRuleInput = {
  keyword: string;
  categoryId: string;
  priority: number;
};

export type ValidationFailure = {
  ok: false;
  status: 400;
  error: string;
  field?: string;
};

export type ValidationResult<T> = { ok: true; value: T } | ValidationFailure;

function fail(error: string, field?: string): ValidationFailure {
  return { ok: false, status: 400, error, field };
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function isUuid(value: unknown): value is string {
  return typeof value === "string" && UUID_RE.test(value);
}

function normalizeName(raw: unknown): ValidationResult<string> {
  const name = typeof raw === "string" ? raw.trim() : "";
  if (name === "" || name.length > CATEGORY_NAME_MAX_LENGTH) {
    return fail(
      `Field 'name' must be a string of 1 to ${CATEGORY_NAME_MAX_LENGTH} characters`,
      "name"
    );
  }
  return { ok: true, value: name };
}

function normalizeColor(raw: unknown): ValidationResult<string | null> {
  if (raw === null) return { ok: true, value: null };
  const color = typeof raw === "string" ? raw.trim().toLowerCase() : "";
  if (!HEX_COLOR_RE.test(color)) {
    return fail("Field 'color' must be a #rrggbb hex color or null", "color");
  }
  return { ok: true, value: color };
}

function normalizeIcon(raw: unknown): ValidationResult<CategoryIconName | null> {
  if (raw === null) return { ok: true, value: null };
  if (
    typeof raw !== "string" ||
    !(CATEGORY_ICON_NAMES as readonly string[]).includes(raw)
  ) {
    return fail("Field 'icon' must be one of the supported icons or null", "icon");
  }
  return { ok: true, value: raw as CategoryIconName };
}

/** Trim and lowercase a rule keyword; 1..100 characters after trimming. Used
 *  by rule writes and by the preview, so both match the same text. */
export function normalizeKeyword(raw: unknown): ValidationResult<string> {
  const keyword = typeof raw === "string" ? raw.trim().toLowerCase() : "";
  if (keyword === "" || keyword.length > RULE_KEYWORD_MAX_LENGTH) {
    return fail(
      `Field 'keyword' must be a string of 1 to ${RULE_KEYWORD_MAX_LENGTH} characters`,
      "keyword"
    );
  }
  return { ok: true, value: keyword };
}

function normalizePriority(raw: unknown): ValidationResult<number> {
  if (
    typeof raw !== "number" ||
    !Number.isInteger(raw) ||
    raw < RULE_PRIORITY_MIN ||
    raw > RULE_PRIORITY_MAX
  ) {
    return fail(
      `Field 'priority' must be an integer from ${RULE_PRIORITY_MIN} to ${RULE_PRIORITY_MAX}`,
      "priority"
    );
  }
  return { ok: true, value: raw };
}

/**
 * Validate and normalize a category write. Create mode (default) requires
 * `name` and defaults `color` and `icon` to null. `{ partial: true }`
 * validates only the fields present, for PATCH, and rejects a body with none.
 */
export function validateCategoryInput(
  body: unknown
): ValidationResult<NormalizedCategoryInput>;
export function validateCategoryInput(
  body: unknown,
  opts: { partial: true }
): ValidationResult<Partial<NormalizedCategoryInput>>;
export function validateCategoryInput(
  body: unknown,
  opts?: { partial?: boolean }
): ValidationResult<Partial<NormalizedCategoryInput>> {
  const partial = opts?.partial === true;
  if (!isPlainObject(body)) {
    return fail("Request body must be a JSON object");
  }
  const out: Partial<NormalizedCategoryInput> = {};

  if (body.name !== undefined || !partial) {
    if (body.name === undefined) {
      return fail("Field 'name' is required", "name");
    }
    const name = normalizeName(body.name);
    if (!name.ok) return name;
    out.name = name.value;
  }

  if (body.color !== undefined) {
    const color = normalizeColor(body.color);
    if (!color.ok) return color;
    out.color = color.value;
  } else if (!partial) {
    out.color = null;
  }

  if (body.icon !== undefined) {
    const icon = normalizeIcon(body.icon);
    if (!icon.ok) return icon;
    out.icon = icon.value;
  } else if (!partial) {
    out.icon = null;
  }

  if (partial && Object.keys(out).length === 0) {
    return fail("Request body must include at least one of: name, color, icon");
  }
  return { ok: true, value: out };
}

/**
 * Validate and normalize a rule write. Create mode (default) requires
 * `keyword` and `categoryId` and defaults `priority` to 0. `{ partial: true }`
 * validates only the fields present, for PATCH, and rejects a body with none.
 * Whether the category exists is checked by the route.
 */
export function validateRuleInput(
  body: unknown
): ValidationResult<NormalizedRuleInput>;
export function validateRuleInput(
  body: unknown,
  opts: { partial: true }
): ValidationResult<Partial<NormalizedRuleInput>>;
export function validateRuleInput(
  body: unknown,
  opts?: { partial?: boolean }
): ValidationResult<Partial<NormalizedRuleInput>> {
  const partial = opts?.partial === true;
  if (!isPlainObject(body)) {
    return fail("Request body must be a JSON object");
  }
  const out: Partial<NormalizedRuleInput> = {};

  if (body.keyword !== undefined || !partial) {
    if (body.keyword === undefined) {
      return fail("Field 'keyword' is required", "keyword");
    }
    const keyword = normalizeKeyword(body.keyword);
    if (!keyword.ok) return keyword;
    out.keyword = keyword.value;
  }

  if (body.categoryId !== undefined || !partial) {
    if (body.categoryId === undefined) {
      return fail("Field 'categoryId' is required", "categoryId");
    }
    if (!isUuid(body.categoryId)) {
      return fail("Field 'categoryId' must be a UUID", "categoryId");
    }
    out.categoryId = body.categoryId;
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
      "Request body must include at least one of: keyword, categoryId, priority"
    );
  }
  return { ok: true, value: out };
}
