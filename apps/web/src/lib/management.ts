// Request validation and update planning for the management API (issue #17):
// editing products, institutions and accounts. Pure (no db, no next imports)
// so it unit-tests in isolation; every failure already carries the HTTP shape
// the routes return (`{ error, field? }` with its status).
//
// What is editable follows who owns each column:
// - product kind, currency and external_ref are the scrapers' identity key
//   (`uq_products_identity`), and the typed attributes/metrics payloads are
//   keyed by kind; editing any of them would fork the product on its next
//   scrape, so they are read-only here. The one exception inside attributes
//   is a credit card's billing days (`statement_day` / `due_day`), for cards
//   whose scraper never reports them; the writer merges attributes key by
//   key, so a scraper that does report them overwrites the manual value.
// - an institution's slug is the key scrapers and the refresh endpoint resolve
//   by, so it is read-only too. Everything else is display metadata.

import type {
  CreditCardAttributes,
  ProductAttributes,
  ProductKind,
} from "@chanchito/product-model";
import { normalizeSlug } from "@/lib/db/slug";
import { isRetiredGhost } from "@/lib/retired-products";

export const INSTITUTION_KINDS = [
  "bank",
  "fintech",
  "exchange",
  "asset_manager",
  "other",
] as const;
export type InstitutionKind = (typeof INSTITUTION_KINDS)[number];

// Institution kinds (bank/fintech/...) are a dashboard-local vocabulary,
// mirrored by the CHECK constraint on institutions.kind (V009).
export const INSTITUTION_KIND_LABELS: Record<string, string> = {
  bank: "Banco",
  fintech: "Fintech",
  exchange: "Exchange",
  asset_manager: "Gestora",
  other: "Otro",
};

export const PRODUCT_NAME_MAX = 120;
export const PRODUCT_SLUG_MAX = 80;
export const INSTITUTION_NAME_MAX = 80;
export const INSTITUTION_URL_MAX = 300;
export const ACCOUNT_NAME_MAX = 80;
/** Well inside a Postgres integer; display orders are small by nature. */
export const DISPLAY_ORDER_LIMIT = 1_000_000;
export const BILLING_DAY_MIN = 1;
export const BILLING_DAY_MAX = 31;

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const COUNTRY_RE = /^[A-Z]{2}$/;

/** An ISO 3166-1 alpha-2 code, uppercase (e.g. "CL"). */
export function isCountryCode(value: string): boolean {
  return COUNTRY_RE.test(value);
}

/** An absolute http(s) address. */
export function isHttpUrl(value: string): boolean {
  try {
    const { protocol } = new URL(value);
    return protocol === "http:" || protocol === "https:";
  } catch {
    return false;
  }
}

export type ManagementFailure = {
  ok: false;
  status: 400 | 409;
  error: string;
  field?: string;
};

export type ManagementResult<T> = { ok: true; value: T } | ManagementFailure;

function fail(
  error: string,
  field?: string,
  status: 400 | 409 = 400
): ManagementFailure {
  return { ok: false, status, error, field };
}

export function isUuid(value: unknown): value is string {
  return typeof value === "string" && UUID_RE.test(value);
}

export function isBillingDay(value: unknown): value is number {
  return (
    typeof value === "number" &&
    Number.isInteger(value) &&
    value >= BILLING_DAY_MIN &&
    value <= BILLING_DAY_MAX
  );
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Shared body checks: an object, only known keys, at least one of them. */
function checkBody(
  body: unknown,
  allowed: readonly string[],
  readOnly: Record<string, string>
): ManagementResult<Record<string, unknown>> {
  if (!isPlainObject(body)) return fail("Body must be a JSON object");
  for (const key of Object.keys(body)) {
    if (Object.hasOwn(readOnly, key)) return fail(readOnly[key], key);
    if (!allowed.includes(key)) return fail(`Unknown field '${key}'`, key);
  }
  if (!allowed.some((key) => body[key] !== undefined)) {
    return fail("Nothing to update");
  }
  return { ok: true, value: body };
}

function requiredText(
  value: unknown,
  field: string,
  max: number
): ManagementResult<string> {
  if (typeof value !== "string") {
    return fail(`Field '${field}' must be a string`, field);
  }
  const trimmed = value.trim();
  if (!trimmed) return fail(`Field '${field}' cannot be empty`, field);
  if (trimmed.length > max) {
    return fail(`Field '${field}' must be at most ${max} characters`, field);
  }
  return { ok: true, value: trimmed };
}

/** A nullable text field: null, or a string that trims to empty, clears it. */
function optionalText(
  value: unknown,
  field: string
): ManagementResult<string | null> {
  if (value === null) return { ok: true, value: null };
  if (typeof value !== "string") {
    return fail(`Field '${field}' must be a string or null`, field);
  }
  return { ok: true, value: value.trim() || null };
}

// ---------------------------------------------------------------------------
// Products
// ---------------------------------------------------------------------------

export type ProductPatch = {
  /** A new display name, or null to reset it to the product's default name
   *  (`products.default_name`, the name the scrapers give it). */
  name?: string | null;
  slug?: string;
  parentProductId?: string | null;
  isActive?: boolean;
  displayOrder?: number;
  /** A credit card's statement day (facturación), or null to clear it. */
  statementDay?: number | null;
  /** A credit card's due day (vencimiento), or null to clear it. */
  dueDay?: number | null;
};

const PRODUCT_FIELDS = [
  "name",
  "slug",
  "parentProductId",
  "isActive",
  "displayOrder",
  "statementDay",
  "dueDay",
] as const;

const BILLING_DAY_ATTRIBUTES = {
  statementDay: "statement_day",
  dueDay: "due_day",
} as const;
export type BillingDayField = keyof typeof BILLING_DAY_ATTRIBUTES;
export const BILLING_DAY_FIELDS = Object.keys(
  BILLING_DAY_ATTRIBUTES
) as readonly BillingDayField[];

const PRODUCT_READ_ONLY: Record<string, string> = {
  kind: "Field 'kind' is part of the product's scraper identity and cannot be edited",
  currency:
    "Field 'currency' is part of the product's scraper identity and cannot be edited",
  externalRef:
    "Field 'externalRef' is part of the product's scraper identity and cannot be edited",
};

/**
 * Validate a product PATCH body. `name: null` asks for the default name back
 * (a blank string is still refused). The slug is canonicalized with the same
 * rules as generated slugs (so "Cuenta Ahorro" becomes "cuenta-ahorro");
 * uniqueness is checked later, against the institution's products.
 */
export function validateProductPatch(
  body: unknown
): ManagementResult<ProductPatch> {
  const parsed = checkBody(body, PRODUCT_FIELDS, PRODUCT_READ_ONLY);
  if (!parsed.ok) return parsed;
  const checked = parsed.value;
  const out: ProductPatch = {};

  if (checked.name === null) {
    out.name = null;
  } else if (checked.name !== undefined) {
    const name = requiredText(checked.name, "name", PRODUCT_NAME_MAX);
    if (!name.ok) return name;
    out.name = name.value;
  }

  if (checked.slug !== undefined) {
    if (typeof checked.slug !== "string") {
      return fail("Field 'slug' must be a string", "slug");
    }
    const slug = normalizeSlug(checked.slug);
    if (slug == null) {
      return fail("Field 'slug' must contain a letter or a digit", "slug");
    }
    if (slug.length > PRODUCT_SLUG_MAX) {
      return fail(
        `Field 'slug' must be at most ${PRODUCT_SLUG_MAX} characters`,
        "slug"
      );
    }
    out.slug = slug;
  }

  if (checked.parentProductId !== undefined) {
    if (checked.parentProductId !== null && !isUuid(checked.parentProductId)) {
      return fail(
        "Field 'parentProductId' must be a product id or null",
        "parentProductId"
      );
    }
    out.parentProductId = checked.parentProductId;
  }

  if (checked.isActive !== undefined) {
    if (typeof checked.isActive !== "boolean") {
      return fail("Field 'isActive' must be a boolean", "isActive");
    }
    out.isActive = checked.isActive;
  }

  if (checked.displayOrder !== undefined) {
    const order = checked.displayOrder;
    if (
      typeof order !== "number" ||
      !Number.isInteger(order) ||
      Math.abs(order) > DISPLAY_ORDER_LIMIT
    ) {
      return fail(
        `Field 'displayOrder' must be an integer between -${DISPLAY_ORDER_LIMIT} and ${DISPLAY_ORDER_LIMIT}`,
        "displayOrder"
      );
    }
    out.displayOrder = order;
  }

  for (const field of BILLING_DAY_FIELDS) {
    const day = checked[field];
    if (day === undefined) continue;
    if (day !== null && !isBillingDay(day)) {
      return fail(
        `Field '${field}' must be an integer between ${BILLING_DAY_MIN} and ${BILLING_DAY_MAX}, or null`,
        field
      );
    }
    out[field] = day;
  }

  return { ok: true, value: out };
}

/** The slice of a product row the update rules look at. `currentBalance` may
 *  arrive as a string: postgres-js returns numeric columns as strings. */
export interface ProductRuleRow {
  id: string;
  slug: string;
  parentProductId: string | null;
  isActive: boolean;
  currentBalance: string | number | null;
}

/** The product being edited: its rule row plus the name a reset restores
 *  and the kind and attributes the billing days merge into. */
export interface ProductEditRow extends ProductRuleRow {
  defaultName: string;
  kind: ProductKind;
  attributes: ProductAttributes | Record<string, never>;
}

/** The columns a product PATCH writes: a reset is already resolved to the
 *  default name, and billing days to the merged attributes. */
export type ProductChanges = Omit<ProductPatch, "name" | BillingDayField> & {
  name?: string;
  attributes?: CreditCardAttributes;
};

/**
 * True when making `parentId` the parent of `productId` would close a loop,
 * i.e. `productId` already sits on `parentId`'s ancestor chain. A loop that
 * already exists in the data ends the walk instead of spinning forever.
 */
export function createsParentCycle(
  productId: string,
  parentId: string,
  parentOf: ReadonlyMap<string, string | null>
): boolean {
  const seen = new Set<string>();
  let current: string | null = parentId;
  while (current != null && !seen.has(current)) {
    if (current === productId) return true;
    seen.add(current);
    current = parentOf.get(current) ?? null;
  }
  return false;
}

/**
 * Merge a patch's billing days into a credit card's attributes, keeping every
 * other key; null removes the day (the writer never stores nulls either).
 * Null when the patch sets no billing day or each already holds its value.
 * Any other kind is refused: only `CreditCardAttributes` has these days.
 */
function planBillingDays(
  product: ProductEditRow,
  patch: ProductPatch
): ManagementResult<CreditCardAttributes | null> {
  const fields = BILLING_DAY_FIELDS.filter((f) => patch[f] !== undefined);
  if (fields.length === 0) return { ok: true, value: null };
  if (product.kind !== "credit_card") {
    return fail(`Field '${fields[0]}' only applies to credit cards`, fields[0]);
  }

  // The kind is a credit card; only the stored payload may still be `{}`.
  const current = product.attributes as Partial<CreditCardAttributes>;
  const next: CreditCardAttributes = { ...current, kind: "credit_card" };
  let changed = false;
  for (const field of fields) {
    const key = BILLING_DAY_ATTRIBUTES[field];
    const day = patch[field] ?? null;
    if ((current[key] ?? null) === day) continue;
    changed = true;
    if (day === null) delete next[key];
    else next[key] = day;
  }
  return { ok: true, value: changed ? next : null };
}

/**
 * Check a validated patch against the product and every product of its
 * institution (retired ghosts included, since they keep their slugs
 * reserved), and return only the columns that actually change.
 *
 * - `name: null` resets the name to the product's default name.
 * - Billing days apply to a credit card only and merge into its attributes.
 * - Deactivating a product with no balance is refused: inactive + no balance
 *   is exactly the retired-ghost shape (lib/retired-products), which the UI
 *   hides everywhere, so the product would vanish with no way to reactivate
 *   it from the dashboard.
 * - A new slug must be free across the institution, like a generated one.
 * - The parent must be a visible product of the same institution, not the
 *   product itself, and must not turn the parent links into a loop.
 */
export function planProductUpdate(
  product: ProductEditRow,
  patch: ProductPatch,
  institutionProducts: readonly ProductRuleRow[]
): ManagementResult<ProductChanges> {
  const changes: ProductChanges = {};

  const billing = planBillingDays(product, patch);
  if (!billing.ok) return billing;
  if (billing.value) changes.attributes = billing.value;

  if (patch.name !== undefined) changes.name = patch.name ?? product.defaultName;

  if (patch.isActive !== undefined && patch.isActive !== product.isActive) {
    if (!patch.isActive && product.currentBalance == null) {
      return fail(
        "A product with no balance cannot be deactivated: it would disappear from the dashboard",
        "isActive",
        409
      );
    }
    changes.isActive = patch.isActive;
  }

  if (patch.slug !== undefined && patch.slug !== product.slug) {
    const taken = institutionProducts.some(
      (p) => p.id !== product.id && p.slug === patch.slug
    );
    if (taken) {
      return fail(
        `Slug '${patch.slug}' is already used by another product of this institution`,
        "slug",
        409
      );
    }
    changes.slug = patch.slug;
  }

  if (
    patch.parentProductId !== undefined &&
    patch.parentProductId !== product.parentProductId
  ) {
    const parentId = patch.parentProductId;
    if (parentId !== null) {
      if (parentId === product.id) {
        return fail(
          "A product cannot be its own parent",
          "parentProductId"
        );
      }
      const parent = institutionProducts.find((p) => p.id === parentId);
      if (!parent || isRetiredGhost(parent)) {
        return fail(
          "The parent must be a product of the same institution",
          "parentProductId"
        );
      }
      const parentOf = new Map(
        institutionProducts.map((p) => [p.id, p.parentProductId])
      );
      if (createsParentCycle(product.id, parentId, parentOf)) {
        return fail(
          "That parent would make the product its own ancestor",
          "parentProductId"
        );
      }
    }
    changes.parentProductId = parentId;
  }

  if (patch.displayOrder !== undefined) {
    changes.displayOrder = patch.displayOrder;
  }

  return { ok: true, value: changes };
}

// ---------------------------------------------------------------------------
// Institutions
// ---------------------------------------------------------------------------

export type InstitutionPatch = {
  name?: string;
  kind?: InstitutionKind;
  country?: string | null;
  url?: string | null;
};

const INSTITUTION_FIELDS = ["name", "kind", "country", "url"] as const;

const INSTITUTION_READ_ONLY: Record<string, string> = {
  slug: "Field 'slug' is the key scrapers resolve the institution by and cannot be edited",
};

/** Validate an institution PATCH body. `country` is an ISO 3166-1 alpha-2
 *  code (uppercased); `url` an http(s) address. Both clear with null or "". */
export function validateInstitutionPatch(
  body: unknown
): ManagementResult<InstitutionPatch> {
  const parsed = checkBody(body, INSTITUTION_FIELDS, INSTITUTION_READ_ONLY);
  if (!parsed.ok) return parsed;
  const checked = parsed.value;
  const out: InstitutionPatch = {};

  if (checked.name !== undefined) {
    const name = requiredText(checked.name, "name", INSTITUTION_NAME_MAX);
    if (!name.ok) return name;
    out.name = name.value;
  }

  if (checked.kind !== undefined) {
    if (!INSTITUTION_KINDS.includes(checked.kind as InstitutionKind)) {
      return fail(
        `Field 'kind' must be one of: ${INSTITUTION_KINDS.join(", ")}`,
        "kind"
      );
    }
    out.kind = checked.kind as InstitutionKind;
  }

  if (checked.country !== undefined) {
    const country = optionalText(checked.country, "country");
    if (!country.ok) return country;
    const code = country.value?.toUpperCase() ?? null;
    if (code != null && !isCountryCode(code)) {
      return fail(
        "Field 'country' must be a two-letter country code (e.g. CL)",
        "country"
      );
    }
    out.country = code;
  }

  if (checked.url !== undefined) {
    const url = optionalText(checked.url, "url");
    if (!url.ok) return url;
    if (url.value != null) {
      if (url.value.length > INSTITUTION_URL_MAX) {
        return fail(
          `Field 'url' must be at most ${INSTITUTION_URL_MAX} characters`,
          "url"
        );
      }
      if (!isHttpUrl(url.value)) {
        return fail("Field 'url' must be an http(s) address", "url");
      }
    }
    out.url = url.value;
  }

  return { ok: true, value: out };
}

// ---------------------------------------------------------------------------
// Accounts
// ---------------------------------------------------------------------------

export type AccountPatch = { name?: string };

/** Validate an account PATCH body (rename only). */
export function validateAccountPatch(
  body: unknown
): ManagementResult<AccountPatch> {
  const parsed = checkBody(body, ["name"], {});
  if (!parsed.ok) return parsed;
  const checked = parsed.value;
  const name = requiredText(checked.name, "name", ACCOUNT_NAME_MAX);
  if (!name.ok) return name;
  return { ok: true, value: { name: name.value } };
}

// ---------------------------------------------------------------------------
// Database errors
// ---------------------------------------------------------------------------

/**
 * True for a Postgres unique violation (SQLSTATE 23505), optionally on one
 * named constraint. Drizzle wraps driver errors, so the cause chain is
 * walked too.
 */
export function isUniqueViolation(error: unknown, constraint?: string): boolean {
  let current: unknown = error;
  for (let depth = 0; current != null && depth < 5; depth += 1) {
    if (typeof current !== "object") return false;
    const e = current as {
      code?: unknown;
      constraint_name?: unknown;
      cause?: unknown;
    };
    if (e.code === "23505") {
      return constraint == null || e.constraint_name === constraint;
    }
    current = e.cause;
  }
  return false;
}
