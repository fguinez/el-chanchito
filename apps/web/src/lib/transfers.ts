// Request validation for internal transfers: money moving between two of the
// user's own products. Pure (no db, no next imports) so it unit-tests in
// isolation: the route loads the products a body names and hands them in, and
// every failure already carries the `{ error, field }` shape the route returns
// with status 400.

/** Transfers carry one integer amount, formatted as CLP everywhere, so both
 *  endpoints must hold CLP. */
export const TRANSFER_CURRENCY = "CLP";

/** `internal_transfers.amount` is a Postgres `integer`. */
const MAX_AMOUNT = 2_147_483_647;

export type TransferValidationFailure = {
  ok: false;
  status: 400;
  error: string;
  field?: string;
};

export type TransferValidationResult<T> =
  | { ok: true; value: T }
  | TransferValidationFailure;

/** A create body, validated: both endpoints set, distinct and lowercased,
 *  amount a positive whole number. */
export type NewTransferInput = {
  description: string;
  amount: number;
  fromProductId: string;
  toProductId: string;
  transferDate: string;
  notes: string | null;
};

/** What the endpoint check needs to know about a product. */
export interface TransferEndpointProduct {
  id: string;
  currency: string;
}

/** A transfer endpoint as GET /api/transfers returns it. */
export interface TransferProductRef {
  id: string;
  name: string;
  slug: string;
  institutionSlug: string;
  institutionName: string;
}

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

function fail(error: string, field?: string): TransferValidationFailure {
  return { ok: false, status: 400, error, field };
}

function isUuid(value: unknown): value is string {
  return typeof value === "string" && UUID_RE.test(value);
}

/** A real calendar day in YYYY-MM-DD form (rejects 2026-02-30). */
function isPlainDate(value: unknown): value is string {
  if (typeof value !== "string" || !DATE_RE.test(value)) return false;
  const date = new Date(`${value}T00:00:00Z`);
  return (
    !Number.isNaN(date.getTime()) && date.toISOString().slice(0, 10) === value
  );
}

/**
 * Shape-check a POST body. `fromAccountId`/`toAccountId` are accepted as
 * legacy aliases of the product ids (pre-V009 naming).
 */
export function parseNewTransfer(
  body: unknown
): TransferValidationResult<NewTransferInput> {
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    return fail("Body must be a JSON object");
  }
  const b = body as Record<string, unknown>;

  const description =
    typeof b.description === "string" ? b.description.trim() : "";
  if (!description) {
    return fail("Field 'description' is required", "description");
  }

  if (typeof b.amount !== "number" || !Number.isFinite(b.amount)) {
    return fail("Field 'amount' must be a number", "amount");
  }
  const amount = Math.round(b.amount);
  if (amount <= 0 || amount > MAX_AMOUNT) {
    return fail(
      `Field 'amount' must be between 1 and ${MAX_AMOUNT}`,
      "amount"
    );
  }

  if (!isPlainDate(b.transferDate)) {
    return fail("Field 'transferDate' must be a YYYY-MM-DD date", "transferDate");
  }

  const fromProductId = b.fromProductId ?? b.fromAccountId;
  if (!isUuid(fromProductId)) {
    return fail("Field 'fromProductId' must be a product id", "fromProductId");
  }
  const toProductId = b.toProductId ?? b.toAccountId;
  if (!isUuid(toProductId)) {
    return fail("Field 'toProductId' must be a product id", "toProductId");
  }
  if (fromProductId.toLowerCase() === toProductId.toLowerCase()) {
    return fail("Source and target products must differ", "toProductId");
  }

  let notes: string | null = null;
  if (b.notes !== undefined && b.notes !== null) {
    if (typeof b.notes !== "string") {
      return fail("Field 'notes' must be a string", "notes");
    }
    notes = b.notes.trim() || null;
  }

  return {
    ok: true,
    value: {
      description,
      amount,
      fromProductId: fromProductId.toLowerCase(),
      toProductId: toProductId.toLowerCase(),
      transferDate: b.transferDate,
      notes,
    },
  };
}

/** Check that both endpoints exist and hold the transfer currency. Ids are
 *  compared as Postgres returns them (lowercase), like `parseNewTransfer`
 *  emits them. */
export function checkTransferEndpoints(
  input: Pick<NewTransferInput, "fromProductId" | "toProductId">,
  found: TransferEndpointProduct[]
): TransferValidationResult<null> {
  const byId = new Map(found.map((p) => [p.id, p]));
  const endpoints = [
    ["fromProductId", input.fromProductId],
    ["toProductId", input.toProductId],
  ] as const;
  for (const [field, id] of endpoints) {
    const product = byId.get(id);
    if (!product) return fail(`Unknown product '${id}'`, field);
    if (product.currency !== TRANSFER_CURRENCY) {
      return fail(
        `Transfers are in ${TRANSFER_CURRENCY}; product '${id}' holds ${product.currency}`,
        field
      );
    }
  }
  return { ok: true, value: null };
}
