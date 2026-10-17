// Database side of internal transfers: the joined listing GET /api/transfers
// returns, and the writes that keep `transactions.is_internal_transfer` in step
// with the transfer links (V028). The checks themselves are pure and live in
// lib/transfers.

import { alias, type AnyPgColumn } from "drizzle-orm/pg-core";
import {
  and,
  asc,
  between,
  desc,
  eq,
  inArray,
  ne,
  or,
  sql,
  type SQL,
} from "drizzle-orm";
import { db } from "@/lib/db";
import { isUniqueViolation } from "@/lib/management";
import {
  accounts,
  dismissedTransferSuggestions,
  institutions,
  internalTransfers,
  products,
  transactions,
} from "@/lib/db/schema";
import {
  LINK_FIELD,
  TRANSFER_SIDES,
  candidateWindow,
  checkTransferLink,
  expectedSideAmount,
  findLinkClash,
  sideProductId,
  sideTransactionId,
  transferIsAccountedFor,
  type TransferLinkedTransaction,
  type TransferProductRef,
  type TransferSide,
  type TransferValidationFailure,
} from "@/lib/transfers";

type Tx = Parameters<Parameters<typeof db.transaction>[0]>[0];

const fromProduct = alias(products, "from_product");
const fromAccount = alias(accounts, "from_account");
const fromInstitution = alias(institutions, "from_institution");
const toProduct = alias(products, "to_product");
const toAccount = alias(accounts, "to_account");
const toInstitution = alias(institutions, "to_institution");
const fromTxn = alias(transactions, "from_txn");
const toTxn = alias(transactions, "to_txn");

type Nullable<T> = { [K in keyof T]: T[K] | null };

/** The left joins leave every column null on a legacy null endpoint; the
 *  chain's FKs are NOT NULL, so a product id means the rest is there too. */
function productRef(row: Nullable<TransferProductRef>): TransferProductRef | null {
  return row.id === null ? null : (row as TransferProductRef);
}

function linkedTransaction(
  row: Nullable<Omit<TransferLinkedTransaction, "amount">> & {
    amount: number | null;
  }
): TransferLinkedTransaction | null {
  if (row.id === null) return null;
  return {
    id: row.id,
    transactionDate: row.transactionDate as string,
    description: row.description as string,
    amount: Number(row.amount),
    currency: row.currency as string,
  };
}

/** Whether the product has at least one stored transaction: the stand-in for
 *  "has a transaction feed" until the registry declares it per kind. */
export function hasFeed(productId: AnyPgColumn) {
  return sql<boolean>`exists (select 1 from ${transactions} where ${transactions.productId} = ${productId})`;
}

/**
 * Every transfer matching `where` (all of them without one), newest first,
 * with its products (`fromProduct`/`toProduct`, null on legacy rows), the
 * transactions it links (`fromTransaction`/`toTransaction`) and whether each
 * product has a transaction feed (`fromHasFeed`/`toHasFeed`).
 */
export async function loadTransfers(where?: SQL) {
  const rows = await db
    .select({
      transfer: internalTransfers,
      from: {
        id: fromProduct.id,
        name: fromProduct.name,
        slug: fromProduct.slug,
        institutionSlug: fromInstitution.slug,
        institutionName: fromInstitution.name,
      },
      to: {
        id: toProduct.id,
        name: toProduct.name,
        slug: toProduct.slug,
        institutionSlug: toInstitution.slug,
        institutionName: toInstitution.name,
      },
      fromTxn: {
        id: fromTxn.id,
        transactionDate: fromTxn.transactionDate,
        description: fromTxn.description,
        amount: fromTxn.amount,
        currency: fromProduct.currency,
      },
      toTxn: {
        id: toTxn.id,
        transactionDate: toTxn.transactionDate,
        description: toTxn.description,
        amount: toTxn.amount,
        currency: toProduct.currency,
      },
      fromHasFeed: hasFeed(fromProduct.id),
      toHasFeed: hasFeed(toProduct.id),
    })
    .from(internalTransfers)
    .leftJoin(fromProduct, eq(internalTransfers.fromProductId, fromProduct.id))
    .leftJoin(fromAccount, eq(fromProduct.accountId, fromAccount.id))
    .leftJoin(
      fromInstitution,
      eq(fromAccount.institutionId, fromInstitution.id)
    )
    .leftJoin(toProduct, eq(internalTransfers.toProductId, toProduct.id))
    .leftJoin(toAccount, eq(toProduct.accountId, toAccount.id))
    .leftJoin(toInstitution, eq(toAccount.institutionId, toInstitution.id))
    .leftJoin(fromTxn, eq(internalTransfers.fromTransactionId, fromTxn.id))
    .leftJoin(toTxn, eq(internalTransfers.toTransactionId, toTxn.id))
    .where(where)
    .orderBy(
      desc(internalTransfers.transferDate),
      desc(internalTransfers.createdAt)
    );

  return rows.map((r) => ({
    ...r.transfer,
    fromProduct: productRef(r.from),
    toProduct: productRef(r.to),
    fromTransaction: linkedTransaction(r.fromTxn),
    toTransaction: linkedTransaction(r.toTxn),
    fromHasFeed: Boolean(r.fromHasFeed),
    toHasFeed: Boolean(r.toHasFeed),
  }));
}

export type TransferListItem = Awaited<ReturnType<typeof loadTransfers>>[number];

async function flagRows(tx: Tx, ids: string[]) {
  if (ids.length === 0) return;
  await tx
    .update(transactions)
    .set({ isInternalTransfer: true, updatedAt: new Date() })
    .where(inArray(transactions.id, ids));
}

/** Clear the flag on `ids`, except on rows another transfer still links. Runs
 *  after the transfer's own row was updated or deleted. */
async function unflagReleasedRows(tx: Tx, ids: string[]) {
  if (ids.length === 0) return;
  await tx
    .update(transactions)
    .set({ isInternalTransfer: false, updatedAt: new Date() })
    .where(
      and(
        inArray(transactions.id, ids),
        sql`not exists (select 1 from ${internalTransfers} where ${internalTransfers.fromTransactionId} = ${transactions.id} or ${internalTransfers.toTransactionId} = ${transactions.id})`
      )
    );
}

/** A pair of rows the user took apart (by deleting the transfer that linked
 *  them or editing its links), automatic or not: dismiss it, so the
 *  suggestions do not link it again on their own. A side whose row is gone
 *  has nothing to dismiss. */
async function dismissReleasedPair(
  tx: Tx,
  transfer: {
    fromTransactionId: string | null;
    toTransactionId: string | null;
  }
) {
  if (!transfer.fromTransactionId || !transfer.toTransactionId) return;
  await tx
    .insert(dismissedTransferSuggestions)
    .values({
      sourceTransactionId: transfer.fromTransactionId,
      targetTransactionId: transfer.toTransactionId,
    })
    .onConflictDoNothing();
}

export type LinkOutcome =
  | { ok: true }
  | { ok: false; status: 404 }
  | TransferValidationFailure;

/**
 * Set or clear the transactions a transfer links, in the caller's db
 * transaction. `input` holds a row id to link, null to clear, or undefined to
 * leave a side as it is. Each linked row gets `is_internal_transfer`; a row
 * the transfer lets go loses it unless another transfer links it. A pending
 * transfer turns `resolved` once every side whose product has a transaction
 * feed is linked (see transferIsAccountedFor); the status never moves back on
 * its own, so unlinking leaves a resolved transfer resolved. Changing the
 * links dismisses the pair the transfer had (when it linked both sides) and
 * makes an automatic transfer the user's (`autoLinked` cleared).
 */
export async function applyTransferLinks(
  tx: Tx,
  transferId: string,
  input: { fromTransactionId?: string | null; toTransactionId?: string | null }
): Promise<LinkOutcome> {
  const [transfer] = await tx
    .select()
    .from(internalTransfers)
    .where(eq(internalTransfers.id, transferId))
    .for("update");
  if (!transfer) return { ok: false, status: 404 };

  // A side resubmitted unchanged keeps its link as is: re-checking it would
  // fail the whole save once a re-scrape stops that row matching.
  const requested: Partial<Record<TransferSide, string>> = {};
  for (const side of TRANSFER_SIDES) {
    const value = input[LINK_FIELD[side]];
    if (value && value !== sideTransactionId(transfer, side)) {
      requested[side] = value;
    }
  }
  const requestedIds = Object.values(requested);

  if (requestedIds.length > 0) {
    const found = await tx
      .select({
        id: transactions.id,
        productId: transactions.productId,
        amount: transactions.amount,
      })
      .from(transactions)
      .where(inArray(transactions.id, requestedIds))
      // Held until commit: a row deleted between this check and the UPDATE
      // below would otherwise fail the FK (23503) as a 500. KEY SHARE is
      // what the FK itself takes, so scraper updates are not blocked.
      .for("key share");
    const byId = new Map(found.map((t) => [t.id, t]));
    for (const side of TRANSFER_SIDES) {
      const txnId = requested[side];
      if (!txnId) continue;
      const check = checkTransferLink(transfer, side, byId.get(txnId));
      if (!check.ok) return check;
    }

    const holders = await tx
      .select({
        id: internalTransfers.id,
        fromTransactionId: internalTransfers.fromTransactionId,
        toTransactionId: internalTransfers.toTransactionId,
      })
      .from(internalTransfers)
      .where(
        and(
          ne(internalTransfers.id, transferId),
          or(
            inArray(internalTransfers.fromTransactionId, requestedIds),
            inArray(internalTransfers.toTransactionId, requestedIds)
          )
        )
      );
    const clash = findLinkClash(transferId, requested, holders);
    if (clash) return clash;
  }

  const next = {
    fromTransactionId:
      input.fromTransactionId === undefined
        ? transfer.fromTransactionId
        : input.fromTransactionId,
    toTransactionId:
      input.toTransactionId === undefined
        ? transfer.toTransactionId
        : input.toTransactionId,
  };
  // An unchanged side skips its sign check, so one row could end up on both.
  if (
    next.fromTransactionId !== null &&
    next.fromTransactionId === next.toTransactionId
  ) {
    return {
      ok: false,
      status: 400,
      error: "La misma transacción no puede ser origen y destino",
      field: LINK_FIELD.to,
    };
  }
  const linkedNow = new Set(
    [next.fromTransactionId, next.toTransactionId].filter(
      (id): id is string => id !== null
    )
  );
  const released = TRANSFER_SIDES.map((side) => sideTransactionId(transfer, side))
    .filter((id): id is string => id !== null && !linkedNow.has(id));

  let status = transfer.status;
  if (status === "pending") {
    const productIds = TRANSFER_SIDES.map((side) =>
      sideProductId(transfer, side)
    ).filter((id): id is string => id !== null);
    // One EXISTS per product (it stops at the first row), not a scan of the
    // products' whole history.
    const withFeed =
      productIds.length === 0
        ? []
        : await tx
            .select({ id: products.id })
            .from(products)
            .where(and(inArray(products.id, productIds), hasFeed(products.id)));
    if (
      transferIsAccountedFor(
        { ...transfer, ...next },
        new Set(withFeed.map((row) => row.id))
      )
    ) {
      status = "resolved";
    }
  }

  const relinked =
    next.fromTransactionId !== transfer.fromTransactionId ||
    next.toTransactionId !== transfer.toTransactionId;
  if (relinked) await dismissReleasedPair(tx, transfer);

  await tx
    .update(internalTransfers)
    .set({
      ...next,
      status,
      autoLinked: transfer.autoLinked && !relinked,
      updatedAt: new Date(),
    })
    .where(eq(internalTransfers.id, transferId));
  await flagRows(tx, [...linkedNow]);
  await unflagReleasedRows(tx, released);
  return { ok: true };
}

/** Delete a transfer and clear the flag on the rows it linked (unless another
 *  transfer links them), in one db transaction. The pair it linked is
 *  dismissed too, so the suggestions do not recreate it (for an automatic
 *  transfer this is its undo). False when it did not exist. */
export async function deleteTransfer(transferId: string): Promise<boolean> {
  return db.transaction(async (tx) => {
    const [deleted] = await tx
      .delete(internalTransfers)
      .where(eq(internalTransfers.id, transferId))
      .returning({
        fromTransactionId: internalTransfers.fromTransactionId,
        toTransactionId: internalTransfers.toTransactionId,
      });
    if (!deleted) return false;
    await dismissReleasedPair(tx, deleted);
    await unflagReleasedRows(
      tx,
      [deleted.fromTransactionId, deleted.toTransactionId].filter(
        (id): id is string => id !== null
      )
    );
    return true;
  });
}

/** One row a side may link, as GET /api/transfers/[id]/candidates returns it. */
export interface TransferCandidate {
  id: string;
  transactionDate: string;
  description: string;
  amount: number;
  source: string;
  isInternalTransfer: boolean;
  /** This transfer links it today. */
  linked: boolean;
}

/**
 * The transactions each side of a transfer may link: on that side's product,
 * moving exactly the transfer amount the right way, dated within
 * CANDIDATE_WINDOW_DAYS of the transfer, and linked by no other transfer. The
 * row the side links today is always included (`linked: true`), even outside
 * the window. Null for the transfer when it does not exist.
 */
export async function loadTransferCandidates(transferId: string) {
  const [transfer] = await db
    .select()
    .from(internalTransfers)
    .where(eq(internalTransfers.id, transferId));
  if (!transfer) return null;

  const window = candidateWindow(transfer.transferDate);
  const sides = await Promise.all(
    TRANSFER_SIDES.map(async (side) => {
      const productId = sideProductId(transfer, side);
      if (!productId) return { productId, hasFeed: false, candidates: [] };
      const linkedId = sideTransactionId(transfer, side);
      const [rows, feed] = await Promise.all([
        db
          .select({
            id: transactions.id,
            transactionDate: transactions.transactionDate,
            description: transactions.description,
            amount: transactions.amount,
            source: transactions.source,
            isInternalTransfer: transactions.isInternalTransfer,
          })
          .from(transactions)
          .where(
            and(
              eq(transactions.productId, productId),
              or(
                and(
                  eq(
                    transactions.amount,
                    expectedSideAmount(transfer.amount, side)
                  ),
                  between(transactions.transactionDate, window.from, window.to),
                  sql`not exists (select 1 from ${internalTransfers} where ${internalTransfers.id} <> ${transferId} and (${internalTransfers.fromTransactionId} = ${transactions.id} or ${internalTransfers.toTransactionId} = ${transactions.id}))`
                ),
                // The current link always shows, so the picker reflects it
                // even when it no longer fits the window.
                linkedId ? eq(transactions.id, linkedId) : undefined
              )
            )
          )
          .orderBy(
            sql`abs(${transactions.transactionDate} - ${transfer.transferDate}::date)`,
            asc(transactions.transactionDate)
          ),
        db
          .select({ id: transactions.id })
          .from(transactions)
          .where(eq(transactions.productId, productId))
          .limit(1),
      ]);
      const candidates: TransferCandidate[] = rows.map((row) => ({
        ...row,
        amount: Number(row.amount),
        linked: row.id === linkedId,
      }));
      return { productId, hasFeed: feed.length > 0, candidates };
    })
  );
  return { from: sides[0], to: sides[1] };
}

/** The body field a link unique violation is about, or null for any other
 *  error. */
export function linkFieldOfViolation(error: unknown): string | null {
  if (isUniqueViolation(error, "uq_internal_transfers_to_transaction")) {
    return LINK_FIELD.to;
  }
  if (isUniqueViolation(error, "uq_internal_transfers_from_transaction")) {
    return LINK_FIELD.from;
  }
  return null;
}
