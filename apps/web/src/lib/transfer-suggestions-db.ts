// Database side of the transfer suggestions (issue #95): find the candidates,
// confirm one (a transfer linked through the #94 logic, plus a learned
// pattern), link the safest ones without asking, or dismiss one. Scoring and
// wording live in lib/transfer-suggestions.
//
// Sign convention (transactions.amount): negative = money out of the product,
// positive = money in, on every kind. A card charge is negative and a card
// payment (abono) positive, so paying a card from checking is a negative row
// on checking and a positive one on the card, like any other transfer.

import { alias, type AnyPgColumn } from "drizzle-orm/pg-core";
import {
  and,
  eq,
  gte,
  inArray,
  isNull,
  lt,
  ne,
  not,
  or,
  sql,
  type SQL,
} from "drizzle-orm";
import { KIND_INFO, type ProductKind } from "@chanchito/product-model";
import { db } from "@/lib/db";
import {
  accounts,
  confirmedTransferPatterns,
  dismissedTransferSuggestions,
  institutions,
  internalTransfers,
  productSnapshots,
  products,
  transactions,
} from "@/lib/db/schema";
import { isForeignKeyViolation } from "@/lib/monitors/adjustments";
import { formatLocalDate } from "@/lib/monitors/dates";
import { displayProductName } from "@/lib/product-names";
import {
  BALANCE_MATCH_TOLERANCE,
  SUGGESTION_WINDOW_DAYS,
  buildSuggestions,
  containsWords,
  daysBetween,
  descriptionPrefix,
  isMatchableName,
  lookbackStart,
  matchesPattern,
  normalizeText,
  pickAutoLinks,
  withoutSettled,
  type BalanceMatch,
  type ConfirmedPattern,
  type OneSidedCandidate,
  type PairCandidate,
  type SuggestionCandidate,
  type SuggestionProductLeg,
  type SuggestionTransactionLeg,
  type TransferSuggestion,
} from "@/lib/transfer-suggestions";
import { TRANSFER_CURRENCY, checkTransferEndpoints, shiftDay } from "@/lib/transfers";
import { applyTransferLinks, hasFeed, linkFieldOfViolation } from "@/lib/transfers-db";
import { displayDescriptionOf, notDeleted } from "@/lib/db/transactions";

// ---------------------------------------------------------------------------
// Shared conditions
// ---------------------------------------------------------------------------

/** No transfer links the row on either side. */
function notLinked(txnId: AnyPgColumn): SQL {
  return sql`not exists (select 1 from ${internalTransfers} where ${internalTransfers.fromTransactionId} = ${txnId} or ${internalTransfers.toTransactionId} = ${txnId})`;
}

/** The user has not dismissed (source, target); a null target is the
 *  one-sided suggestion for the source. */
function notDismissed(sourceId: AnyPgColumn, targetId: AnyPgColumn | null): SQL {
  const target =
    targetId === null
      ? sql`${dismissedTransferSuggestions.targetTransactionId} is null`
      : sql`${dismissedTransferSuggestions.targetTransactionId} = ${targetId}`;
  return sql`not exists (select 1 from ${dismissedTransferSuggestions} where ${dismissedTransferSuggestions.sourceTransactionId} = ${sourceId} and ${target})`;
}

/** The columns the helpers below read off an aliased chain. */
type TxnColumns = Record<
  | "id"
  | "productId"
  | "transactionDate"
  | "description"
  | "customDescription"
  | "amount"
  | "isInternalTransfer"
  | "deletedAt",
  AnyPgColumn
>;
type ProductColumns = Record<"name" | "slug" | "kind" | "currency", AnyPgColumn>;
type InstitutionColumns = Record<"name" | "slug", AnyPgColumn>;

/** A row a suggestion may use: CLP, recent, not flagged and not linked. */
function openRow(
  txn: TxnColumns,
  product: ProductColumns,
  since: string
): SQL {
  return and(
    eq(product.currency, TRANSFER_CURRENCY),
    gte(txn.transactionDate, since),
    not(txn.isInternalTransfer),
    // A soft-deleted row (V033) is never suggested as a transfer leg.
    isNull(txn.deletedAt),
    notLinked(txn.id)
  ) as SQL;
}

/** The columns a suggestion leg shows, for one aliased transaction chain. */
function legColumns(
  txn: TxnColumns,
  product: ProductColumns,
  institution: InstitutionColumns
) {
  return {
    transactionId: txn.id,
    productId: txn.productId,
    productName: product.name,
    productSlug: product.slug,
    productKind: product.kind,
    institutionName: institution.name,
    institutionSlug: institution.slug,
    date: txn.transactionDate,
    // Scoring, patterns and name matches read the raw scraped text; the
    // resolved one (custom description or alias) is for display and search.
    description: txn.description,
    displayDescription: displayDescriptionOf(txn, product),
    amount: txn.amount,
    currency: product.currency,
  };
}

interface LegRow {
  transactionId: string;
  productId: string;
  productName: string;
  productSlug: string;
  productKind: ProductKind;
  institutionName: string;
  institutionSlug: string;
  date: string;
  description: string;
  displayDescription: string;
  amount: number;
  currency: string;
}

function toLeg(row: LegRow): SuggestionTransactionLeg {
  return {
    transactionId: row.transactionId,
    productId: row.productId,
    productName: displayProductName(
      { name: row.productName, kind: row.productKind, currency: row.currency },
      row.institutionName
    ),
    productSlug: row.productSlug,
    institutionName: row.institutionName,
    institutionSlug: row.institutionSlug,
    date: row.date,
    description: row.description,
    displayDescription: row.displayDescription,
    amount: Number(row.amount),
    currency: row.currency,
  };
}

// ---------------------------------------------------------------------------
// Pairs
// ---------------------------------------------------------------------------

const src = alias(transactions, "src");
const srcProduct = alias(products, "src_product");
const srcAccount = alias(accounts, "src_account");
const srcInstitution = alias(institutions, "src_institution");
const dst = alias(transactions, "dst");
const dstProduct = alias(products, "dst_product");
const dstAccount = alias(accounts, "dst_account");
const dstInstitution = alias(institutions, "dst_institution");

/**
 * Every (debit, credit) pair on two different CLP products moving exactly the
 * same amount, dated within SUGGESTION_WINDOW_DAYS of each other, both since
 * `since`, neither flagged nor linked, and not dismissed. A row with several
 * possible partners comes back once per partner; the debit is always the
 * source, so no pair is listed twice.
 */
export async function loadPairCandidates(since: string): Promise<PairCandidate[]> {
  const rows = await db
    .select({
      source: legColumns(src, srcProduct, srcInstitution),
      target: legColumns(dst, dstProduct, dstInstitution),
    })
    .from(src)
    .innerJoin(srcProduct, eq(src.productId, srcProduct.id))
    .innerJoin(srcAccount, eq(srcProduct.accountId, srcAccount.id))
    .innerJoin(srcInstitution, eq(srcAccount.institutionId, srcInstitution.id))
    .innerJoin(
      dst,
      and(
        eq(dst.amount, sql`-${src.amount}`),
        ne(dst.productId, src.productId),
        sql`${dst.transactionDate} between ${src.transactionDate} - ${SUGGESTION_WINDOW_DAYS}::int and ${src.transactionDate} + ${SUGGESTION_WINDOW_DAYS}::int`
      )
    )
    .innerJoin(dstProduct, eq(dst.productId, dstProduct.id))
    .innerJoin(dstAccount, eq(dstProduct.accountId, dstAccount.id))
    .innerJoin(dstInstitution, eq(dstAccount.institutionId, dstInstitution.id))
    .where(
      and(
        lt(src.amount, 0),
        openRow(src, srcProduct, since),
        openRow(dst, dstProduct, since),
        notDismissed(src.id, dst.id)
      )
    );

  return rows.map((row) => ({
    kind: "pair",
    source: toLeg(row.source as LegRow),
    target: toLeg(row.target as LegRow),
  }));
}

// ---------------------------------------------------------------------------
// One-sided
// ---------------------------------------------------------------------------

/** Names too generic to match on: the kind labels a product falls back to. */
const GENERIC_NAMES = new Set(
  Object.values(KIND_INFO).map((info) => normalizeText(info.labelEs))
);

interface FeedlessProduct {
  leg: SuggestionProductLeg;
  institutionId: string;
  /** Names a debit description may mention, most specific first. */
  productNames: string[];
}

/** Active CLP products without a single transaction, with the names a
 *  description may mention them by. */
async function loadFeedlessProducts(): Promise<FeedlessProduct[]> {
  const rows = await db
    .select({
      id: products.id,
      name: products.name,
      slug: products.slug,
      kind: products.kind,
      currency: products.currency,
      institutionId: institutions.id,
      institutionName: institutions.name,
      institutionSlug: institutions.slug,
    })
    .from(products)
    .innerJoin(accounts, eq(products.accountId, accounts.id))
    .innerJoin(institutions, eq(accounts.institutionId, institutions.id))
    .where(
      and(
        eq(products.currency, TRANSFER_CURRENCY),
        eq(products.isActive, true),
        not(hasFeed(products.id))
      )
    );

  return rows.map((row) => {
    const productName = displayProductName(row, row.institutionName);
    return {
      leg: {
        transactionId: null,
        productId: row.id,
        productName,
        productSlug: row.slug,
        institutionName: row.institutionName,
        institutionSlug: row.institutionSlug,
        date: null,
        description: null,
        displayDescription: null,
        amount: null,
        currency: row.currency,
      },
      institutionId: row.institutionId,
      productNames: [...new Set([productName, row.name])].filter(usableName),
    };
  });
}

function usableName(name: string): boolean {
  return isMatchableName(name) && !GENERIC_NAMES.has(normalizeText(name));
}

/**
 * The name `description` mentions `product` by, or null. The institution name
 * counts only for a debit at another institution: a bank's own name in its own
 * descriptions says nothing about which of its products got the money.
 */
export function mentionedName(
  description: string,
  sourceInstitutionId: string,
  product: Pick<FeedlessProduct, "leg" | "institutionId" | "productNames">
): { name: string; by: "product" | "institution" } | null {
  for (const name of product.productNames) {
    if (containsWords(description, name)) {
      return { name: product.leg.productName, by: "product" };
    }
  }
  const institution = product.leg.institutionName;
  if (
    sourceInstitutionId !== product.institutionId &&
    usableName(institution) &&
    containsWords(description, institution)
  ) {
    return { name: institution, by: "institution" };
  }
  return null;
}

/** Rises in each product's balance history dated since `since`, as
 *  `{ date, delta }` (local day of the snapshot, rise over the previous one). */
async function loadBalanceRises(
  productIds: string[],
  since: string
): Promise<Map<string, BalanceMatch[]>> {
  const rises = new Map<string, BalanceMatch[]>();
  if (productIds.length === 0) return rises;

  const deltas = db.$with("deltas").as(
    db
      .select({
        productId: productSnapshots.productId,
        asOf: productSnapshots.asOf,
        delta: sql<string | null>`${productSnapshots.balance} - lag(${productSnapshots.balance}) over (partition by ${productSnapshots.productId} order by ${productSnapshots.asOf})`.as(
          "delta"
        ),
      })
      .from(productSnapshots)
      .where(inArray(productSnapshots.productId, productIds))
  );
  const rows = await db
    .with(deltas)
    .select()
    .from(deltas)
    .where(
      and(
        gte(deltas.asOf, new Date(`${since}T00:00:00`)),
        sql`${deltas.delta} > 0`
      )
    );

  for (const row of rows) {
    const list = rises.get(row.productId) ?? [];
    list.push({
      date: formatLocalDate(new Date(row.asOf)),
      delta: Number(row.delta),
    });
    rises.set(row.productId, list);
  }
  return rises;
}

/** The rise closest in date to the debit that is within
 *  SUGGESTION_WINDOW_DAYS and BALANCE_MATCH_TOLERANCE of it, or null. */
export function findBalanceMatch(
  source: Pick<SuggestionTransactionLeg, "date" | "amount">,
  rises: readonly BalanceMatch[]
): BalanceMatch | null {
  const amount = Math.abs(source.amount);
  let best: BalanceMatch | null = null;
  for (const rise of rises) {
    const gap = daysBetween(source.date, rise.date);
    if (gap > SUGGESTION_WINDOW_DAYS) continue;
    if (Math.abs(rise.delta - amount) > amount * BALANCE_MATCH_TOLERANCE) continue;
    if (!best || gap < daysBetween(source.date, best.date)) best = rise;
  }
  return best;
}

const debit = alias(transactions, "debit");
const debitProduct = alias(products, "debit_product");
const debitAccount = alias(accounts, "debit_account");
const debitInstitution = alias(institutions, "debit_institution");

/**
 * Debits since `since` (CLP, not flagged, not linked, no one-sided dismissal)
 * whose description names an active CLP product without a transaction feed,
 * or that match a confirmed pattern towards one. A rise of about the same
 * size in that product's balance history near the debit date is attached as
 * `balanceMatch`.
 */
export async function loadOneSidedCandidates(
  since: string,
  patterns: readonly ConfirmedPattern[]
): Promise<OneSidedCandidate[]> {
  const feedless = await loadFeedlessProducts();
  if (feedless.length === 0) return [];

  const debits = await db
    .select({
      leg: legColumns(debit, debitProduct, debitInstitution),
      institutionId: debitInstitution.id,
    })
    .from(debit)
    .innerJoin(debitProduct, eq(debit.productId, debitProduct.id))
    .innerJoin(debitAccount, eq(debitProduct.accountId, debitAccount.id))
    .innerJoin(
      debitInstitution,
      eq(debitAccount.institutionId, debitInstitution.id)
    )
    .where(
      and(
        lt(debit.amount, 0),
        openRow(debit, debitProduct, since),
        notDismissed(debit.id, null)
      )
    );

  const perInstitution = new Map<string, number>();
  for (const product of feedless) {
    perInstitution.set(
      product.institutionId,
      (perInstitution.get(product.institutionId) ?? 0) + 1
    );
  }

  const matches: {
    source: SuggestionTransactionLeg;
    target: FeedlessProduct;
    name: ReturnType<typeof mentionedName>;
  }[] = [];
  for (const row of debits) {
    const source = toLeg(row.leg as LegRow);
    for (const product of feedless) {
      if (product.leg.productId === source.productId) continue;
      const name = mentionedName(source.description, row.institutionId, product);
      if (name || matchesPattern(source, product.leg.productId, patterns)) {
        matches.push({ source, target: product, name });
      }
    }
  }
  if (matches.length === 0) return [];

  const rises = await loadBalanceRises(
    [...new Set(matches.map((m) => m.target.leg.productId))],
    shiftDay(since, -SUGGESTION_WINDOW_DAYS)
  );
  return matches.map(({ source, target, name }) => ({
    kind: "one_sided",
    source,
    target: target.leg,
    matchedName: name?.name ?? null,
    matchedBy: name?.by ?? null,
    institutionSiblings: (perInstitution.get(target.institutionId) ?? 1) - 1,
    balanceMatch: findBalanceMatch(
      source,
      rises.get(target.leg.productId) ?? []
    ),
  }));
}

// ---------------------------------------------------------------------------
// Listing
// ---------------------------------------------------------------------------

export async function loadConfirmedPatterns(): Promise<ConfirmedPattern[]> {
  return db
    .select({
      sourceProductId: confirmedTransferPatterns.sourceProductId,
      targetProductId: confirmedTransferPatterns.targetProductId,
      descriptionPrefix: confirmedTransferPatterns.descriptionPrefix,
    })
    .from(confirmedTransferPatterns);
}

/**
 * Every current suggestion, scored and ordered (highest score first, then the
 * most recent source row). Looks at rows dated since SUGGESTION_LOOKBACK_DAYS
 * before `today` (the server's local day by default).
 */
export async function loadTransferSuggestions(
  today: string = formatLocalDate(new Date())
): Promise<TransferSuggestion[]> {
  const since = lookbackStart(today);
  const patterns = await loadConfirmedPatterns();
  const [pairs, oneSided] = await Promise.all([
    loadPairCandidates(since),
    loadOneSidedCandidates(since, patterns),
  ]);
  const candidates: SuggestionCandidate[] = [...pairs, ...oneSided];
  return buildSuggestions(candidates, patterns);
}

// ---------------------------------------------------------------------------
// Confirm and dismiss
// ---------------------------------------------------------------------------

export interface ConfirmSuggestionInput {
  sourceTransactionId: string;
  targetTransactionId: string | null;
  targetProductId: string | null;
}

export type SuggestionFailure = {
  ok: false;
  status: 400 | 409;
  error: string;
  field?: string;
};

export type ConfirmOutcome = { ok: true; transferId: string } | SuggestionFailure;

const ALREADY_LINKED = "La transacción ya está vinculada a otro movimiento";

function failure(
  status: 400 | 409,
  error: string,
  field?: string
): SuggestionFailure {
  return { ok: false, status, error, field };
}

/** Thrown inside the db transaction to roll it back with a failure. */
class Rollback extends Error {
  constructor(readonly outcome: SuggestionFailure) {
    super(outcome.error);
  }
}

type Tx = Parameters<Parameters<typeof db.transaction>[0]>[0];

/** The body field naming a side, for the error a failed check reports. */
function sideField(
  side: "from" | "to",
  input: ConfirmSuggestionInput
): string {
  if (side === "from") return "sourceTransactionId";
  return input.targetTransactionId ? "targetTransactionId" : "targetProductId";
}

async function confirmIn(
  tx: Tx,
  input: ConfirmSuggestionInput,
  auto: boolean
): Promise<ConfirmOutcome> {
  const ids = [input.sourceTransactionId, input.targetTransactionId].filter(
    (id): id is string => id !== null
  );
  const rows = await tx
    .select({
      id: transactions.id,
      productId: transactions.productId,
      amount: transactions.amount,
      transactionDate: transactions.transactionDate,
      description: transactions.description,
      isInternalTransfer: transactions.isInternalTransfer,
    })
    .from(transactions)
    // A soft-deleted row (V033) cannot take part in a transfer.
    .where(and(inArray(transactions.id, ids), notDeleted))
    // Held until commit, as applyTransferLinks does: a row deleted mid-confirm
    // would otherwise fail the FK as a 500.
    .for("key share");
  const byId = new Map(rows.map((row) => [row.id, row]));

  const source = byId.get(input.sourceTransactionId);
  if (!source) return failure(400, "La transacción no existe", "sourceTransactionId");
  const sourceAmount = Number(source.amount);
  if (!(sourceAmount < 0)) {
    return failure(
      400,
      "La transacción de origen debe ser un cargo (monto negativo)",
      "sourceTransactionId"
    );
  }
  const amount = Math.abs(sourceAmount);
  if (!Number.isInteger(amount)) {
    return failure(
      400,
      "El monto de la transacción debe ser un número entero de pesos",
      "sourceTransactionId"
    );
  }

  let targetProductId: string;
  if (input.targetTransactionId) {
    const target = byId.get(input.targetTransactionId);
    if (!target) {
      return failure(400, "La transacción no existe", "targetTransactionId");
    }
    if (!(Number(target.amount) > 0)) {
      return failure(
        400,
        "La transacción de destino debe ser un abono (monto positivo)",
        "targetTransactionId"
      );
    }
    if (Number(target.amount) !== amount) {
      return failure(
        400,
        "Los montos de las dos transacciones no coinciden exactamente",
        "targetTransactionId"
      );
    }
    targetProductId = target.productId;
  } else {
    targetProductId = input.targetProductId as string;
  }
  if (targetProductId === source.productId) {
    return failure(
      400,
      "El origen y el destino deben ser productos distintos",
      sideField("to", input)
    );
  }

  const found = await tx
    .select({
      id: products.id,
      currency: products.currency,
      isActive: products.isActive,
    })
    .from(products)
    .where(inArray(products.id, [source.productId, targetProductId]));
  const endpoints = checkTransferEndpoints(
    { fromProductId: source.productId, toProductId: targetProductId },
    found
  );
  if (!endpoints.ok) {
    // checkTransferEndpoints speaks English for POST /api/transfers; the
    // suggestions UI shows its errors as they come, so say it in Spanish.
    const side = endpoints.field === "fromProductId" ? "from" : "to";
    const product = found.find(
      (p) => p.id === (side === "from" ? source.productId : targetProductId)
    );
    return failure(
      400,
      product
        ? `Las transferencias son en ${TRANSFER_CURRENCY}; este producto está en ${product.currency}`
        : "El producto no existe",
      sideField(side, input)
    );
  }
  if (
    !input.targetTransactionId &&
    !found.some((p) => p.id === targetProductId && p.isActive)
  ) {
    return failure(400, "El producto de destino está inactivo", "targetProductId");
  }
  // The feed check sits in WHERE: in a single-table SELECT list Drizzle drops
  // the table names, so `product_id = id` would compare transactions.id.
  const targetWithFeed = input.targetTransactionId
    ? []
    : await tx
        .select({ id: products.id })
        .from(products)
        .where(and(eq(products.id, targetProductId), hasFeed(products.id)));
  if (targetWithFeed.length > 0) {
    return failure(
      400,
      "El producto de destino tiene transacciones: elige la transacción de destino",
      "targetProductId"
    );
  }

  const holders = await tx
    .select({
      fromTransactionId: internalTransfers.fromTransactionId,
      toTransactionId: internalTransfers.toTransactionId,
    })
    .from(internalTransfers)
    .where(
      or(
        inArray(internalTransfers.fromTransactionId, ids),
        inArray(internalTransfers.toTransactionId, ids)
      )
    );
  for (const id of ids) {
    if (holders.some((h) => h.fromTransactionId === id || h.toTransactionId === id)) {
      return failure(
        409,
        ALREADY_LINKED,
        id === source.id ? "sourceTransactionId" : "targetTransactionId"
      );
    }
  }
  // A row flagged by hand (or left flagged) without a link is hidden from
  // the suggestions too; confirming it would hand it a second meaning.
  for (const id of ids) {
    if (byId.get(id)?.isInternalTransfer) {
      return failure(
        409,
        "La transacción ya está marcada como transferencia interna",
        id === source.id ? "sourceTransactionId" : "targetTransactionId"
      );
    }
  }

  // A load that picked this pair before the user undid or dismissed it must
  // not bring it back.
  if (auto) {
    const dismissed = await tx
      .select({ id: dismissedTransferSuggestions.id })
      .from(dismissedTransferSuggestions)
      .where(
        and(
          eq(dismissedTransferSuggestions.sourceTransactionId, source.id),
          input.targetTransactionId
            ? eq(
                dismissedTransferSuggestions.targetTransactionId,
                input.targetTransactionId
              )
            : isNull(dismissedTransferSuggestions.targetTransactionId)
        )
      );
    if (dismissed.length > 0) {
      return failure(409, "La sugerencia fue descartada", "sourceTransactionId");
    }
  }

  const [created] = await tx
    .insert(internalTransfers)
    .values({
      description: source.description,
      amount,
      fromProductId: source.productId,
      toProductId: targetProductId,
      transferDate: source.transactionDate,
    })
    .returning({ id: internalTransfers.id });

  // The #94 link logic: re-checks both rows, flags them and resolves the
  // transfer once every side with a feed is linked.
  const linked = await applyTransferLinks(tx, created.id, {
    fromTransactionId: source.id,
    toTransactionId: input.targetTransactionId,
  });
  if (!linked.ok) {
    // Unreachable after the checks above unless the rows changed meanwhile;
    // the throw rolls the new transfer back.
    if (linked.status === 404) {
      throw new Rollback(failure(409, "El movimiento creado ya no existe"));
    }
    const side = linked.field === "fromTransactionId" ? "from" : "to";
    throw new Rollback(failure(400, linked.error, sideField(side, input)));
  }

  if (auto) {
    // Set after linking: applyTransferLinks clears it on any link change.
    await tx
      .update(internalTransfers)
      .set({ autoLinked: true })
      .where(eq(internalTransfers.id, created.id));
    // No pattern: only the user's own confirms teach the scorer, so an
    // automatic link cannot boost the next one past the bar.
    return { ok: true, transferId: created.id };
  }

  const prefix = descriptionPrefix(source.description);
  if (prefix) {
    await tx
      .insert(confirmedTransferPatterns)
      .values({
        sourceProductId: source.productId,
        targetProductId,
        descriptionPrefix: prefix,
      })
      .onConflictDoNothing();
  }

  return { ok: true, transferId: created.id };
}

/**
 * Confirm a suggestion in one db transaction: re-check it, create the
 * transfer (source row's date and description, its absolute amount), link its
 * rows with the #94 logic (which flags them `is_internal_transfer` and turns
 * the transfer `resolved` once every side with a feed is linked) and save the
 * pattern. Nothing is written when a check fails. 409 when a row is already
 * linked, including a concurrent confirm losing the race. `auto` marks the
 * transfer `autoLinked` and saves no pattern (see autoLinkSuggestions).
 */
export async function confirmTransferSuggestion(
  input: ConfirmSuggestionInput,
  { auto = false }: { auto?: boolean } = {}
): Promise<ConfirmOutcome> {
  try {
    return await db.transaction(async (tx) => {
      const outcome = await confirmIn(tx, input, auto);
      if (!outcome.ok) throw new Rollback(outcome);
      return outcome;
    });
  } catch (error) {
    if (error instanceof Rollback) return error.outcome;
    const field = linkFieldOfViolation(error);
    if (field) {
      return failure(
        409,
        ALREADY_LINKED,
        field === "fromTransactionId" ? "sourceTransactionId" : "targetTransactionId"
      );
    }
    throw error;
  }
}

/**
 * Link the current suggestions pickAutoLinks deems safe (confident pairs that
 * share no row with another suggestion), each in its own db transaction
 * through confirmTransferSuggestion, and return what is left to show plus the
 * ids of the transfers created. Idempotent: a linked row drops out of the
 * candidates, and an undone link is dismissed. A confirm that fails (a
 * concurrent load linked the rows first, or the user dismissed the pair
 * meanwhile) or throws is skipped, so the list still loads; the next load
 * settles it.
 */
export async function autoLinkSuggestions(
  today: string = formatLocalDate(new Date())
): Promise<{ suggestions: TransferSuggestion[]; autoLinked: string[] }> {
  let suggestions = await loadTransferSuggestions(today);
  const autoLinked: string[] = [];
  for (const suggestion of pickAutoLinks(suggestions)) {
    let outcome: ConfirmOutcome;
    try {
      outcome = await confirmTransferSuggestion(
        {
          sourceTransactionId: suggestion.source.transactionId,
          targetTransactionId: suggestion.target.transactionId,
          targetProductId: null,
        },
        { auto: true }
      );
    } catch (error) {
      console.error(`Auto-linking suggestion ${suggestion.id} failed:`, error);
      continue;
    }
    if (!outcome.ok) continue;
    autoLinked.push(outcome.transferId);
    suggestions = withoutSettled(suggestions, suggestion, "confirm");
  }
  return { suggestions, autoLinked };
}

export interface DismissSuggestionInput {
  sourceTransactionId: string;
  targetTransactionId: string | null;
}

/** Remember a dismissed suggestion; dismissing it again is a no-op. 400 when
 *  a row does not exist or the source is not a debit. */
export async function dismissTransferSuggestion(
  input: DismissSuggestionInput
): Promise<{ ok: true } | SuggestionFailure> {
  const ids = [input.sourceTransactionId, input.targetTransactionId].filter(
    (id): id is string => id !== null
  );
  const found = await db
    .select({ id: transactions.id, amount: transactions.amount })
    .from(transactions)
    // A soft-deleted row (V033) is not a suggestion to dismiss.
    .where(and(inArray(transactions.id, ids), notDeleted));
  const byId = new Map(found.map((row) => [row.id, row]));
  const source = byId.get(input.sourceTransactionId);
  if (!source) {
    return failure(400, "La transacción no existe", "sourceTransactionId");
  }
  if (!(Number(source.amount) < 0)) {
    return failure(
      400,
      "La transacción de origen debe ser un cargo (monto negativo)",
      "sourceTransactionId"
    );
  }
  if (input.targetTransactionId && !byId.has(input.targetTransactionId)) {
    return failure(400, "La transacción no existe", "targetTransactionId");
  }

  try {
    await db
      .insert(dismissedTransferSuggestions)
      .values({
        sourceTransactionId: input.sourceTransactionId,
        targetTransactionId: input.targetTransactionId,
      })
      .onConflictDoNothing();
  } catch (error) {
    // A row deleted between the check and the insert.
    if (isForeignKeyViolation(error)) {
      return failure(400, "Una de las transacciones ya no existe");
    }
    throw error;
  }
  return { ok: true };
}
