// Endpoint checks for internal transfers: money moving between two of the
// user's own products. The body's shape is checked by createTransferSchema
// (lib/api/schemas); this module is pure (no db, no next imports) so it
// unit-tests in isolation: the route loads the products a body names and hands
// them in, and a failure carries the `{ error, field }` the route returns as a
// 400.

/** Transfers carry one integer amount, formatted as CLP everywhere, so both
 *  endpoints must hold CLP. */
export const TRANSFER_CURRENCY = "CLP";

export type TransferValidationFailure = {
  ok: false;
  status: 400;
  error: string;
  field?: string;
};

export type TransferValidationResult<T> =
  | { ok: true; value: T }
  | TransferValidationFailure;

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

function fail(error: string, field?: string): TransferValidationFailure {
  return { ok: false, status: 400, error, field };
}

/** Check that both endpoints exist and hold the transfer currency. Ids are
 *  compared as Postgres returns them (lowercase), like createTransferSchema
 *  emits them. */
export function checkTransferEndpoints(
  input: { fromProductId: string; toProductId: string },
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
