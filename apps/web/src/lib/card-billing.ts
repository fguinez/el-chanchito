// A credit card's billing (facturación) at read time: its billing cycle, each
// movement's effective billing status, and the billed / unbilled totals the
// card page shows. Pure, so the product route, the transaction detail route
// and the client can share it.
//
// Dates are plain YYYY-MM-DD strings, compared as text. `today` is the
// America/Santiago calendar day (rates.santiagoDay). A statement closes at the
// end of its closing day: a movement dated on the closing day is on that
// statement, and the closing counts as passed only from the next day on.

import type {
  ProductAttributes,
  ProductKind,
  ProductMetrics,
} from "@chanchito/product-model";
import type { BillingStatus } from "@/lib/db/schema";

/** A cycle date and whether it is a guess rather than the bank's own figure. */
export interface BillingDate {
  /** Plain YYYY-MM-DD. */
  date: string;
  estimated: boolean;
}

/** Where the cycle's last closing comes from, most reliable first. */
export type BillingCycleBasis =
  /** metrics.statement_date: the latest statement the scraper read. */
  | "statement"
  /** The newest statement date stored on the card's billed movements. */
  | "transactions"
  /** One cycle before the next closing the bank printed (metrics). */
  | "next_statement"
  /** The card's día de facturación (attributes.statement_day) alone. */
  | "statement_day";

export interface BillingCycle {
  /** The latest closing that has passed (fecha de facturación). */
  lastClosing: BillingDate;
  /** The closing before `lastClosing`; always derived. A movement still
   *  unbilled from on or before it is taken as billed. */
  previousClosing: string;
  /** The next closing: today or later (próxima facturación). */
  nextClosing: BillingDate;
  /** Payment due date of `lastClosing`'s statement (vencimiento); null when
   *  neither the statement nor a due day says. */
  dueDate: BillingDate | null;
  basis: BillingCycleBasis;
  /** A closing passed after the closing the bank last reported, so the
   *  statement figures in metrics belong to the statement before it. */
  rolledOver: boolean;
}

/** A movement's effective billing status. */
export interface TransactionBilling {
  status: BillingStatus;
  /** Derived from the cycle instead of reported by the bank. */
  estimated: boolean;
  /** Closing date of the statement that billed it, when the bank said. */
  statementDate: string | null;
}

/** What the billing helpers read from one movement. */
export interface BillingTransactionInput {
  /** Signed, in the card's currency: negative = charge, positive = payment
   *  or refund. */
  amount: number;
  /** Plain YYYY-MM-DD. */
  transactionDate: string;
  /** As the bank reported it (V044); null when it did not say. */
  billingStatus: BillingStatus | null;
  statementDate: string | null;
  /** Linked to an internal transfer: a card payment, never a reversal. */
  isInternalTransfer?: boolean;
}

/** The figures of the latest statement the scraper read (metrics). */
export interface StatementFigures {
  /** Its closing date, when reported. */
  date: string | null;
  /** Monto facturado, in the card's currency. */
  billedAmount: number | null;
  /** Pago mínimo, in the card's currency. */
  minimumPayment: number | null;
  /** Its payment due date, when reported. */
  paymentDueDate: string | null;
}

/** A credit card's billing state, as the card page shows it. */
export interface CardBillingSummary {
  /** The day the figures are computed for (America/Santiago). */
  asOf: string;
  /** Null when nothing tells the card's closing dates. */
  cycle: BillingCycle | null;
  statement: StatementFigures;
  /** Positive movements (payments, refunds) after the last closing, plus
   *  those the bank still lists as unbilled; null without a cycle. Internal
   *  transfers count: a payment from checking is linked as one and still
   *  pays the card. A reversal of an unbilled charge does not. */
  paidSinceClosing: number | null;
  /** The statement's billed amount minus `paidSinceClosing`, never below 0.
   *  Null when the billed amount is unknown or belongs to an older statement
   *  than the last closing. */
  billedPending: number | null;
  /** `billedPending` subtracted payments, so it is an estimate. */
  billedPendingEstimated: boolean;
  /** `billedPending > 0`; null when `billedPending` is unknown. */
  hasBilledDebt: boolean | null;
  /** Sum of the effectively unbilled charges (as a positive amount).
   *  Unbilled payments are never netted against it; a reversed charge drops
   *  out with its reversal. */
  unbilledCharges: number;
  /** How many charges make up `unbilledCharges`. */
  unbilledCount: number;
  /** Movements with an effective status, reported or derived. */
  statusCount: number;
  /** False when the movement list was cut and may not reach back to the
   *  previous closing, so the totals may miss older movements. */
  complete: boolean;
}

export interface CardBilling {
  summary: CardBillingSummary;
  /** Each input movement's effective status, in input order. */
  statuses: (TransactionBilling | null)[];
}

export interface BillingCycleInput {
  attributes: ProductAttributes | Record<string, never> | null;
  metrics: ProductMetrics | Record<string, never> | null;
  /** The newest statement date stored on the card's movements, if any. */
  latestStatementDate?: string | null;
  /** YYYY-MM-DD, America/Santiago. */
  today: string;
}

// ---------------------------------------------------------------------------
// Calendar arithmetic on plain dates
// ---------------------------------------------------------------------------

const PLAIN_DATE = /^(\d{4})-(\d{2})-(\d{2})/;

function daysInMonth(year: number, month: number): number {
  return new Date(Date.UTC(year, month, 0)).getUTCDate();
}

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

function parts(date: string): [number, number, number] {
  const [year, month, day] = date.slice(0, 10).split("-").map(Number);
  return [year, month, day];
}

/** The YYYY-MM-DD prefix of a valid calendar date, else null. */
function plainDate(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const match = PLAIN_DATE.exec(value);
  if (!match) return null;
  const [year, month, day] = match.slice(1).map(Number);
  if (month < 1 || month > 12 || day < 1 || day > daysInMonth(year, month)) {
    return null;
  }
  return value.slice(0, 10);
}

/** The date `day` falls on in a month: that day, or the month's last day when
 *  the month is shorter (day 31 in February closes on the 28th or 29th).
 *  `month` is 1-based and may overflow into other years. */
export function dayInMonth(year: number, month: number, day: number): string {
  const y = year + Math.floor((month - 1) / 12);
  const m = ((((month - 1) % 12) + 12) % 12) + 1;
  return `${y}-${pad(m)}-${pad(Math.min(day, daysInMonth(y, m)))}`;
}

/** `day`'s date in the month `offset` months from `date`'s month. */
function dayInMonthFrom(date: string, offset: number, day: number): string {
  const [year, month] = parts(date);
  return dayInMonth(year, month + offset, day);
}

/** The first date strictly after `date` that falls on `day` (clamped). */
function firstDayAfter(date: string, day: number): string {
  const sameMonth = dayInMonthFrom(date, 0, day);
  return sameMonth > date ? sameMonth : dayInMonthFrom(date, 1, day);
}

/** The latest date strictly before `date` that falls on `day` (clamped). */
function lastDayBefore(date: string, day: number): string {
  const sameMonth = dayInMonthFrom(date, 0, day);
  return sameMonth < date ? sameMonth : dayInMonthFrom(date, -1, day);
}

/** `date` moved by `days` calendar days. */
function addDays(date: string, days: number): string {
  const [year, month, day] = parts(date);
  return new Date(Date.UTC(year, month - 1, day + days))
    .toISOString()
    .slice(0, 10);
}

/** The fewest days between two closings: a closing the bank moved across a
 *  month boundary never puts a second closing a day or two away. */
const MIN_CYCLE_DAYS = 20;

/** The closing after `closing` on `day`, at least MIN_CYCLE_DAYS later. */
function closingAfter(closing: string, day: number): string {
  return firstDayAfter(addDays(closing, MIN_CYCLE_DAYS - 1), day);
}

/** The closing before `closing` on `day`, at least MIN_CYCLE_DAYS earlier. */
function closingBefore(closing: string, day: number): string {
  return lastDayBefore(addDays(closing, -(MIN_CYCLE_DAYS - 1)), day);
}

/** Whole days from `from` to `to` (negative when `to` is earlier). */
export function daysBetween(from: string, to: string): number {
  const [fy, fm, fd] = parts(from);
  const [ty, tm, td] = parts(to);
  return Math.round(
    (Date.UTC(ty, tm - 1, td) - Date.UTC(fy, fm - 1, fd)) / 86_400_000
  );
}

/** Rounds away float noise so a paid-off USD statement lands on 0. */
function roundCents(value: number): number {
  return Math.round(value * 100) / 100 || 0;
}

// ---------------------------------------------------------------------------
// Reading the card's payloads
// ---------------------------------------------------------------------------

function isDayOfMonth(value: unknown): value is number {
  return (
    typeof value === "number" &&
    Number.isInteger(value) &&
    value >= 1 &&
    value <= 31
  );
}

function cardDays(attributes: BillingCycleInput["attributes"]): {
  statementDay: number | null;
  dueDay: number | null;
} {
  const a = attributes ?? {};
  const statementDay = "statement_day" in a ? a.statement_day : null;
  const dueDay = "due_day" in a ? a.due_day : null;
  return {
    statementDay: isDayOfMonth(statementDay) ? statementDay : null,
    dueDay: isDayOfMonth(dueDay) ? dueDay : null,
  };
}

function finiteOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** The latest statement's figures from a credit card's metrics (all null for
 *  any other payload). */
export function statementFigures(
  metrics: BillingCycleInput["metrics"]
): StatementFigures {
  if (!metrics || !("kind" in metrics) || metrics.kind !== "credit_card") {
    return {
      date: null,
      billedAmount: null,
      minimumPayment: null,
      paymentDueDate: null,
    };
  }
  return {
    date: plainDate(metrics.statement_date),
    billedAmount: finiteOrNull(metrics.billed_amount),
    minimumPayment: finiteOrNull(metrics.minimum_payment),
    paymentDueDate: plainDate(metrics.payment_due_date),
  };
}

function nextStatementDate(
  metrics: BillingCycleInput["metrics"]
): string | null {
  if (!metrics || !("kind" in metrics) || metrics.kind !== "credit_card") {
    return null;
  }
  return plainDate(metrics.next_statement_date);
}

// ---------------------------------------------------------------------------
// Cycle, status, totals
// ---------------------------------------------------------------------------

/**
 * The card's billing cycle as of `today`, or null when nothing tells it.
 *
 * The last closing is, in order: the latest statement's closing date
 * (metrics), unless a stored movement was billed on a newer statement; the
 * newest statement date on the movements; one cycle before the bank's next
 * closing date; the latest closing day (attributes.statement_day) before
 * today. A date after today is ignored. From there the cycle rolls forward:
 * while the next closing (the bank's, else the next cycle day) is before
 * today, it becomes the last closing, estimated. So the next closing is
 * always today or later.
 *
 * The cycle day is statement_day when known, else the last closing's own day;
 * a month shorter than it closes on its last day. A derived closing is the
 * first (or last) cycle day at least MIN_CYCLE_DAYS away, so a closing the
 * bank moved across a month boundary keeps its cycle's length. The due date
 * is the statement's when it belongs to the last closing, else the first
 * `due_day` after the last closing.
 */
export function resolveBillingCycle(
  input: BillingCycleInput
): BillingCycle | null {
  const { today } = input;
  const { statementDay, dueDay } = cardDays(input.attributes);
  const statement = statementFigures(input.metrics);
  const notFuture = (date: string | null) =>
    date && date <= today ? date : null;
  const statementDate = notFuture(statement.date);
  const rowsDate = notFuture(plainDate(input.latestStatementDate));
  let bankNext = nextStatementDate(input.metrics);

  let last: string;
  let lastEstimated = false;
  let basis: BillingCycleBasis;
  if (statementDate && !(rowsDate && rowsDate > statementDate)) {
    last = statementDate;
    basis = "statement";
  } else if (rowsDate) {
    last = rowsDate;
    basis = "transactions";
  } else if (bankNext) {
    // The bank printed only the next closing: the last one is a cycle before,
    // or as many as it takes to pass (a closing today has not passed yet).
    // Stepping back means a closing falls before the bank's: that one is next.
    const day = statementDay ?? parts(bankNext)[2];
    last = closingBefore(bankNext, day);
    for (let i = 0; last >= today && i < 1200; i++) {
      last = closingBefore(last, day);
      bankNext = null;
    }
    lastEstimated = true;
    basis = "next_statement";
  } else if (statementDay) {
    last = lastDayBefore(today, statementDay);
    lastEstimated = true;
    basis = "statement_day";
  } else {
    return null;
  }

  const cycleDay = statementDay ?? parts(last)[2];
  let rolledOver = false;
  let next: BillingDate;
  // Bounded so a malformed date can never spin: 1200 months is a century.
  for (let i = 0; ; i++) {
    next =
      bankNext && bankNext > last
        ? { date: bankNext, estimated: false }
        : { date: closingAfter(last, cycleDay), estimated: true };
    if (next.date >= today || i >= 1200) break;
    // That closing has passed since: it is the last one now.
    last = next.date;
    lastEstimated = true;
    rolledOver = true;
    bankNext = null;
  }

  let dueDate: BillingDate | null = null;
  if (
    basis === "statement" &&
    !rolledOver &&
    statement.paymentDueDate &&
    statement.paymentDueDate > last
  ) {
    dueDate = { date: statement.paymentDueDate, estimated: false };
  } else if (dueDay) {
    dueDate = { date: firstDayAfter(last, dueDay), estimated: true };
  }

  return {
    lastClosing: { date: last, estimated: lastEstimated },
    previousClosing: closingBefore(last, cycleDay),
    nextClosing: next,
    dueDate,
    basis,
    rolledOver,
  };
}

/**
 * A movement's effective billing status. The bank's "billed" always wins. The
 * bank's "unbilled" holds, except for a movement dated on or before the
 * previous closing (a charge does not stay unposted for a whole cycle, so the
 * source missed the billed view) or, after a rollover, on or before the last
 * closing (that closing passed after the bank said so): both count as billed
 * (estimated). Without a reported status the cycle decides: on or before the
 * last closing is billed, after it unbilled, both estimated. Null when
 * neither is known.
 */
export function effectiveBillingStatus(
  txn: Pick<
    BillingTransactionInput,
    "transactionDate" | "billingStatus" | "statementDate"
  >,
  cycle: BillingCycle | null
): TransactionBilling | null {
  const date = txn.transactionDate.slice(0, 10);
  if (txn.billingStatus === "billed") {
    return {
      status: "billed",
      estimated: false,
      statementDate: plainDate(txn.statementDate),
    };
  }
  if (txn.billingStatus === "unbilled") {
    const billedSince = cycle?.rolledOver
      ? cycle.lastClosing.date
      : cycle?.previousClosing;
    if (billedSince && date <= billedSince) {
      return { status: "billed", estimated: true, statementDate: null };
    }
    return { status: "unbilled", estimated: false, statementDate: null };
  }
  if (!cycle) return null;
  return {
    status: date <= cycle.lastClosing.date ? "billed" : "unbilled",
    estimated: true,
    statementDate: null,
  };
}

/**
 * Indexes of the unbilled charges reversed (anulación) and their reversals.
 * Conservative: a reversal is a credit after the last closing, on or after an
 * effectively unbilled charge of exactly its amount, that is neither a linked
 * transfer, reported billed, nor a statement figure (billed or minimum).
 */
function reversalPairs(
  transactions: readonly BillingTransactionInput[],
  statuses: readonly (TransactionBilling | null)[],
  cycle: BillingCycle | null,
  statement: StatementFigures
): Set<number> {
  const cents = (amount: number) => Math.round(Math.abs(amount) * 100);
  const figures = new Set(
    [statement.billedAmount, statement.minimumPayment]
      .filter((v): v is number => v != null)
      .map(cents)
  );
  const day = (i: number) => transactions[i].transactionDate.slice(0, 10);
  // Oldest first, charges before credits on the same day.
  const order = transactions
    .map((_, i) => i)
    .sort(
      (a, b) =>
        day(a).localeCompare(day(b)) ||
        transactions[a].amount - transactions[b].amount
    );

  const open = new Map<number, number[]>();
  const paired = new Set<number>();
  for (const i of order) {
    const t = transactions[i];
    const key = cents(t.amount);
    if (t.amount < 0 && statuses[i]?.status === "unbilled") {
      open.set(key, [...(open.get(key) ?? []), i]);
      continue;
    }
    const isReversal =
      t.amount > 0 &&
      !t.isInternalTransfer &&
      t.billingStatus !== "billed" &&
      (!cycle || day(i) > cycle.lastClosing.date) &&
      !figures.has(key);
    const charge = isReversal ? open.get(key)?.shift() : undefined;
    if (charge !== undefined) {
      paired.add(charge);
      paired.add(i);
    }
  }
  return paired;
}

/**
 * A credit card's billing summary plus each movement's effective status; null
 * for any other kind. `transactions` are the loaded movements in any order;
 * `truncated` says older ones were left out.
 *
 * The billed debt still owed is the statement's billed amount minus the
 * payments since its closing (`paidSinceClosing`), floored at 0; it is only
 * computed while that statement is the last closing's (no rollover). The
 * unbilled total sums the effectively unbilled charges only; payments, which
 * the bank also lists as unbilled, never shrink it. A reversed unbilled
 * charge and its reversal leave both totals (see `reversalPairs`).
 */
export function summarizeBilling(input: {
  kind: ProductKind;
  attributes: BillingCycleInput["attributes"];
  metrics: BillingCycleInput["metrics"];
  transactions: readonly BillingTransactionInput[];
  truncated: boolean;
  today: string;
}): CardBilling | null {
  if (input.kind !== "credit_card") return null;
  const { transactions, today } = input;

  let latestStatementDate: string | null = null;
  let oldest: string | null = null;
  for (const t of transactions) {
    const statementDate = plainDate(t.statementDate);
    if (statementDate && statementDate > (latestStatementDate ?? "")) {
      latestStatementDate = statementDate;
    }
    const date = t.transactionDate.slice(0, 10);
    if (!oldest || date < oldest) oldest = date;
  }

  const cycle = resolveBillingCycle({
    attributes: input.attributes,
    metrics: input.metrics,
    latestStatementDate,
    today,
  });
  const statement = statementFigures(input.metrics);
  const statuses = transactions.map((t) => effectiveBillingStatus(t, cycle));
  const reversed = reversalPairs(transactions, statuses, cycle, statement);

  let unbilledCharges = 0;
  let unbilledCount = 0;
  let paid = 0;
  statuses.forEach((billing, i) => {
    const t = transactions[i];
    if (reversed.has(i)) return;
    if (billing?.status === "unbilled" && t.amount < 0) {
      unbilledCharges -= t.amount;
      unbilledCount += 1;
    }
    if (!cycle || t.amount <= 0) return;
    const afterClosing =
      t.transactionDate.slice(0, 10) > cycle.lastClosing.date;
    // A credit the bank still lists as unbilled missed the statement, whatever
    // its date, so it pays toward it too.
    const offStatement =
      t.billingStatus === "unbilled" && billing?.status === "unbilled";
    if (afterClosing || offStatement) paid += t.amount;
  });
  const paidSinceClosing = cycle ? roundCents(paid) : null;

  const statementIsLast =
    cycle?.basis === "statement" && !cycle.rolledOver;
  let billedPending: number | null = null;
  if (statementIsLast && statement.billedAmount != null) {
    billedPending = Math.max(
      0,
      roundCents(statement.billedAmount - (paidSinceClosing ?? 0))
    );
  }

  return {
    summary: {
      asOf: today,
      cycle,
      statement,
      paidSinceClosing,
      billedPending,
      billedPendingEstimated:
        billedPending != null &&
        (statement.billedAmount ?? 0) > 0 &&
        (paidSinceClosing ?? 0) > 0,
      hasBilledDebt: billedPending == null ? null : billedPending > 0,
      unbilledCharges: roundCents(unbilledCharges),
      unbilledCount,
      statusCount: statuses.filter((s) => s != null).length,
      complete:
        !input.truncated ||
        (cycle != null && oldest != null && oldest <= cycle.previousClosing),
    },
    statuses,
  };
}

// ---------------------------------------------------------------------------
// Display decisions (the card page)
// ---------------------------------------------------------------------------

/** How the card page labels the billed debt: nothing billed on the last
 *  statement, still owed, paid since, or not known. */
export type BilledDebtState = "none" | "pending" | "paid" | "unknown";

export function billedDebtState(summary: CardBillingSummary): BilledDebtState {
  if (summary.billedPending == null) return "unknown";
  if ((summary.statement.billedAmount ?? 0) <= 0) return "none";
  return summary.billedPending > 0 ? "pending" : "paid";
}

/** The closing date of the statement the metrics figures (monto facturado,
 *  pago mínimo) come from, when it is older than the last closing; else
 *  null (a statement dated after today never sets the cycle). */
export function olderStatementDate(summary: CardBillingSummary): string | null {
  const { cycle, statement } = summary;
  if (!cycle || !statement.date) return null;
  return statement.date < cycle.lastClosing.date ? statement.date : null;
}

/** Billed debt is still owed but the payments since the closing cover the
 *  statement's minimum payment. */
export function isMinimumPaid(summary: CardBillingSummary): boolean {
  const minimum = summary.statement.minimumPayment;
  return (
    summary.hasBilledDebt === true &&
    minimum != null &&
    minimum > 0 &&
    (summary.paidSinceClosing ?? 0) >= minimum
  );
}

/** The due date passed with billed debt still owed and, when the statement
 *  has one, its minimum payment not covered. */
export function isPaymentOverdue(summary: CardBillingSummary): boolean {
  const due = summary.cycle?.dueDate;
  const minimum = summary.statement.minimumPayment;
  return (
    due != null &&
    due.date < summary.asOf &&
    summary.hasBilledDebt === true &&
    (minimum == null || (summary.paidSinceClosing ?? 0) < minimum)
  );
}
