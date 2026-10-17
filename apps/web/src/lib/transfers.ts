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

/** A transfer side: the outgoing row on the source product or the incoming
 *  row on the target product. */
export type TransferSide = "from" | "to";

export const TRANSFER_SIDES: readonly TransferSide[] = ["from", "to"];

/** The body field that names a side's transaction. */
export const LINK_FIELD = {
  from: "fromTransactionId",
  to: "toTransactionId",
} as const satisfies Record<TransferSide, string>;

/** How many days before or after `transfer_date` a candidate may be dated. */
export const CANDIDATE_WINDOW_DAYS = 5;

/** What the link checks need to know about a transfer. */
export interface LinkableTransfer {
  id: string;
  amount: number;
  fromProductId: string | null;
  toProductId: string | null;
  fromTransactionId: string | null;
  toTransactionId: string | null;
}

/** What the link checks need to know about a transaction. */
export interface LinkableTransaction {
  id: string;
  productId: string;
  /** Signed: negative = money out. */
  amount: number;
}

/** A linked transaction as GET /api/transfers returns it. */
export interface TransferLinkedTransaction {
  id: string;
  transactionDate: string;
  description: string;
  amount: number;
  currency: string;
}

/** The product a side's transaction must belong to. */
export function sideProductId(
  transfer: Pick<LinkableTransfer, "fromProductId" | "toProductId">,
  side: TransferSide
): string | null {
  return side === "from" ? transfer.fromProductId : transfer.toProductId;
}

/** The transaction a side links today. */
export function sideTransactionId(
  transfer: Pick<LinkableTransfer, "fromTransactionId" | "toTransactionId">,
  side: TransferSide
): string | null {
  return side === "from" ? transfer.fromTransactionId : transfer.toTransactionId;
}

/**
 * Check that `txn` may be linked to `side` of `transfer`: it exists, belongs to
 * that side's product, moves money the right way (out of the source, into the
 * target) and moves exactly the transfer amount. No tolerance for fees: a
 * transfer whose bank charged a fee on top has no exact match to link.
 * Messages are Spanish: the Vincular dialog shows them as they come.
 */
export function checkTransferLink(
  transfer: LinkableTransfer,
  side: TransferSide,
  txn: LinkableTransaction | undefined
): TransferValidationResult<null> {
  const field = LINK_FIELD[side];
  const productId = sideProductId(transfer, side);
  if (!productId) {
    return fail(
      `Este movimiento no tiene producto de ${side === "from" ? "origen" : "destino"}`,
      field
    );
  }
  if (!txn) return fail("La transacción no existe", field);
  if (txn.productId !== productId) {
    return fail(
      `La transacción no pertenece al producto de ${
        side === "from" ? "origen" : "destino"
      }`,
      field
    );
  }
  if (side === "from" && !(txn.amount < 0)) {
    return fail("La transacción de origen debe ser un cargo (monto negativo)", field);
  }
  if (side === "to" && !(txn.amount > 0)) {
    return fail("La transacción de destino debe ser un abono (monto positivo)", field);
  }
  if (Math.abs(txn.amount) !== transfer.amount) {
    return fail(
      "El monto de la transacción no coincide exactamente con el del movimiento",
      field
    );
  }
  return { ok: true, value: null };
}

/** The other transfers already holding one of `ids`, as `{ id, from, to }`. */
export interface LinkHolder {
  id: string;
  fromTransactionId: string | null;
  toTransactionId: string | null;
}

/** The first requested link another transfer already holds, as a 400. */
export function findLinkClash(
  transferId: string,
  requested: Partial<Record<TransferSide, string>>,
  holders: readonly LinkHolder[]
): TransferValidationFailure | null {
  for (const side of TRANSFER_SIDES) {
    const txnId = requested[side];
    if (!txnId) continue;
    const holder = holders.find(
      (h) =>
        h.id !== transferId &&
        (h.fromTransactionId === txnId || h.toTransactionId === txnId)
    );
    if (holder) {
      return fail(
        "La transacción ya está vinculada a otro movimiento",
        LINK_FIELD[side]
      );
    }
  }
  return null;
}

/**
 * Whether a transfer is fully accounted for and may turn `resolved` on its
 * own: at least one side is linked, and every side whose product has a
 * transaction feed (`productsWithFeed`: products with at least one stored
 * transaction) is linked. A transfer between two feedless products never
 * resolves itself; the user resolves it by hand.
 */
export function transferIsAccountedFor(
  transfer: Pick<
    LinkableTransfer,
    "fromProductId" | "toProductId" | "fromTransactionId" | "toTransactionId"
  >,
  productsWithFeed: ReadonlySet<string>
): boolean {
  let linked = 0;
  for (const side of TRANSFER_SIDES) {
    const productId = sideProductId(transfer, side);
    const txnId = sideTransactionId(transfer, side);
    if (txnId) linked++;
    else if (productId && productsWithFeed.has(productId)) return false;
  }
  return linked > 0;
}

/** `day` moved by `offset` days; YYYY-MM-DD in and out, in UTC so the
 *  server's time zone cannot shift it. */
export function shiftDay(day: string, offset: number): string {
  const date = new Date(`${day}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + offset);
  return date.toISOString().slice(0, 10);
}

/** The inclusive date range candidates for a transfer dated `day` fall in. */
export function candidateWindow(
  day: string,
  days: number = CANDIDATE_WINDOW_DAYS
): { from: string; to: string } {
  return { from: shiftDay(day, -days), to: shiftDay(day, days) };
}

export function expectedSideAmount(amount: number, side: TransferSide): number {
  return side === "from" ? -amount : amount;
}
