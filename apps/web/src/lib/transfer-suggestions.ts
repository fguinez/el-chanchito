// Scoring of internal transfer suggestions (issue #95): rows that look like
// money moving between two of the user's own products, ranked by a simple,
// explainable score. Pure (no db, no next imports): lib/transfer-suggestions-db
// finds the candidates and hands them in, and every point a candidate earns
// comes with a Spanish line of evidence the UI shows as is.
//
// Two kinds of candidate:
//   pair       a debit on one product and a credit of exactly the same amount
//              on another, dated within SUGGESTION_WINDOW_DAYS of each other.
//   one_sided  a debit whose description names a product without a
//              transaction feed (e.g. a fund), optionally backed by a rise of
//              about the same size in that product's balance history.

import { matchesTransactionSearch } from "@/lib/transaction-search";
import { shiftDay } from "@/lib/transfers";

// ---------------------------------------------------------------------------
// Tunables
// ---------------------------------------------------------------------------

/** How many days apart the two legs of a pair (or a debit and the balance
 *  rise it explains) may be dated. */
export const SUGGESTION_WINDOW_DAYS = 3;

/** Only rows dated within this many days of today are looked at. */
export const SUGGESTION_LOOKBACK_DAYS = 90;

/** Description keywords typical of a transfer between own products. Matched
 *  as whole words, ignoring case and accents. */
export const TRANSFER_KEYWORDS = [
  "Traspaso",
  "Transferencia",
  "Pago tarjeta",
  "Abono",
  "Pago TC",
] as const;

/** The /transfers anchor of the Sugeridas card (the detail panel links it). */
export const SUGGESTIONS_ANCHOR = "sugeridas";

/** Characters of the normalised description kept as a pattern prefix. */
export const PATTERN_PREFIX_LENGTH = 20;

/** Shortest normalised name a one-sided match may hang on, so a short or
 *  generic name ("BCI", "Caja") does not match half the descriptions. */
export const MIN_MATCH_NAME_LENGTH = 5;

/** How far a balance rise may be from the debit amount and still count as the
 *  same deposit, as a fraction of the amount (funds move with the market). */
export const BALANCE_MATCH_TOLERANCE = 0.02;

/** Points per signal. A pair always earns SCORE_EXACT_AMOUNT (exact amount is
 *  required); the date signal loses SCORE_DAY_PENALTY per day of gap. */
export const SCORE_EXACT_AMOUNT = 40;
export const SCORE_SAME_DAY = 30;
export const SCORE_DAY_PENALTY = 10;
export const SCORE_KEYWORD = 15;
export const SCORE_NAME_MATCH = 25;
/** An institution-name match when that institution holds several feedless
 *  products: the name alone cannot tell which one got the money. */
export const SCORE_SHARED_INSTITUTION_MATCH = 10;
export const SCORE_BALANCE_MATCH = 30;
export const SCORE_PRODUCT_PAIR = 20;
export const SCORE_PATTERN = 50;

/** A pair scoring at least this is linked without asking (see
 *  pickAutoLinks); one-sided suggestions always wait for the user. */
export const AUTO_LINK_MIN_SCORE = 85;

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** A scraped row on one side of a suggestion. */
export interface SuggestionTransactionLeg {
  transactionId: string;
  productId: string;
  /** What the dashboard calls the product (displayProductName). */
  productName: string;
  /** With institutionSlug, the product page: /institutions/<i>/<p>. */
  productSlug: string;
  institutionName: string;
  institutionSlug: string;
  /** YYYY-MM-DD. */
  date: string;
  /** The scraped text: every score and pattern reads this one. */
  description: string;
  /** What to show (custom description or alias, else `description`); for
   *  display and search only. */
  displayDescription: string;
  /** Signed: negative on the source, positive on a pair's target. */
  amount: number;
  currency: string;
}

/** The target of a one-sided suggestion: a product with no row to link. */
export interface SuggestionProductLeg {
  transactionId: null;
  productId: string;
  productName: string;
  productSlug: string;
  institutionName: string;
  institutionSlug: string;
  date: null;
  description: null;
  displayDescription: null;
  amount: null;
  currency: string;
}

export interface PairCandidate {
  kind: "pair";
  source: SuggestionTransactionLeg;
  target: SuggestionTransactionLeg;
}

/** A rise in a feedless product's balance history near the debit date. */
export interface BalanceMatch {
  /** YYYY-MM-DD of the snapshot that shows the rise. */
  date: string;
  /** The rise, in the product's currency. */
  delta: number;
}

export interface OneSidedCandidate {
  kind: "one_sided";
  source: SuggestionTransactionLeg;
  target: SuggestionProductLeg;
  /** The name the source description mentions (as the dashboard shows it), or
   *  null when only a confirmed pattern brought the candidate in. */
  matchedName: string | null;
  /** What `matchedName` is: the target product's own name, or its
   *  institution's (null without a name match). */
  matchedBy: "product" | "institution" | null;
  /** Other active CLP products without a feed at the target's institution;
   *  an institution-name match scores lower when there are any. */
  institutionSiblings: number;
  balanceMatch: BalanceMatch | null;
}

export type SuggestionCandidate = PairCandidate | OneSidedCandidate;

/** A confirmed suggestion's shape (confirmed_transfer_patterns). */
export interface ConfirmedPattern {
  sourceProductId: string;
  targetProductId: string;
  descriptionPrefix: string;
}

export interface SuggestionScore {
  score: number;
  /** Spanish, one item per signal, each readable on its own. */
  evidence: string[];
}

export type TransferSuggestion = SuggestionCandidate &
  SuggestionScore & {
    /** Stable key: kind, source row and target row (or target product). */
    id: string;
    /** The money moved, positive. */
    amount: number;
  };

// ---------------------------------------------------------------------------
// Text
// ---------------------------------------------------------------------------

/** Lowercase, without accents, every run of non-letters/digits as one space. */
export function normalizeText(text: string): string {
  return text
    .normalize("NFD")
    .replace(/\p{M}/gu, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

/** `text` normalised and without digits, so account numbers, dates and
 *  amounts do not make every description unique. */
function normalizeWords(text: string): string {
  return normalizeText(text.replace(/[0-9]+/g, " "))
    .replace(/\s+/g, " ")
    .trim();
}

/** The pattern key of a description: normalised, digits dropped, cut to
 *  PATTERN_PREFIX_LENGTH characters (without a trailing space). */
export function descriptionPrefix(description: string): string {
  return normalizeWords(description).slice(0, PATTERN_PREFIX_LENGTH).trim();
}

/** Whether normalised `haystack` holds normalised `needle` as whole words. */
export function containsWords(haystack: string, needle: string): boolean {
  const words = normalizeText(needle);
  if (!words) return false;
  return ` ${normalizeText(haystack)} `.includes(` ${words} `);
}

/** The first TRANSFER_KEYWORDS entry `description` contains, or null. */
export function findTransferKeyword(description: string): string | null {
  return TRANSFER_KEYWORDS.find((kw) => containsWords(description, kw)) ?? null;
}

/** Whether `name` is long enough to match descriptions on (see
 *  MIN_MATCH_NAME_LENGTH); a transfer keyword never is. */
export function isMatchableName(name: string): boolean {
  const normalized = normalizeText(name);
  return (
    normalized.length >= MIN_MATCH_NAME_LENGTH &&
    !TRANSFER_KEYWORDS.some((kw) => normalizeText(kw) === normalized)
  );
}

// ---------------------------------------------------------------------------
// Dates
// ---------------------------------------------------------------------------

/** Whole days between two YYYY-MM-DD days, always >= 0. */
export function daysBetween(a: string, b: string): number {
  const ms = Date.parse(`${a}T00:00:00Z`) - Date.parse(`${b}T00:00:00Z`);
  return Math.abs(Math.round(ms / 86_400_000));
}

/** "Mismo día", "1 día de diferencia", "N días de diferencia". */
export function dayGapLabel(days: number): string {
  if (days === 0) return "Mismo día";
  return `${days} ${days === 1 ? "día" : "días"} de diferencia`;
}

/** The first day rows are looked at, for a lookback ending on `today`. */
export function lookbackStart(
  today: string,
  days: number = SUGGESTION_LOOKBACK_DAYS
): string {
  return shiftDay(today, -days);
}

// ---------------------------------------------------------------------------
// Scoring
// ---------------------------------------------------------------------------

const QUOTE_LENGTH = 20;

/** `glosa 'Traspaso a...'`: the description's start, as the bank wrote it. */
function quoteDescription(description: string): string {
  const trimmed = description.trim().replace(/\s+/g, " ");
  const quoted =
    trimmed.length > QUOTE_LENGTH
      ? `${trimmed.slice(0, QUOTE_LENGTH).trimEnd()}...`
      : trimmed;
  return `Glosa '${quoted}'`;
}

function dateScore(days: number): number {
  return Math.max(0, SCORE_SAME_DAY - SCORE_DAY_PENALTY * days);
}

/**
 * Score a candidate and explain it. Signals (additive):
 *   - pair: exact amount (SCORE_EXACT_AMOUNT) and date gap (SCORE_SAME_DAY,
 *     minus SCORE_DAY_PENALTY per day)
 *   - one-sided: the description names the target (SCORE_NAME_MATCH; only
 *     SCORE_SHARED_INSTITUTION_MATCH for an institution name shared by
 *     several feedless products), and a
 *     balance rise of about the same size (SCORE_BALANCE_MATCH, plus the date
 *     signal for its gap)
 *   - a transfer keyword in either description (SCORE_KEYWORD, once)
 *   - a confirmed pattern with the same products and description prefix
 *     (SCORE_PATTERN), or else one with the same products (SCORE_PRODUCT_PAIR)
 */
export function scoreSuggestion(
  candidate: SuggestionCandidate,
  patterns: readonly ConfirmedPattern[] = []
): SuggestionScore {
  let score = 0;
  const evidence: string[] = [];
  const { source, target } = candidate;

  if (candidate.kind === "pair") {
    score += SCORE_EXACT_AMOUNT;
    evidence.push("Mismo monto");
    const gap = daysBetween(source.date, candidate.target.date);
    score += dateScore(gap);
    evidence.push(dayGapLabel(gap));
  } else {
    if (candidate.matchedName) {
      const shared =
        candidate.matchedBy === "institution" &&
        candidate.institutionSiblings > 0;
      score += shared ? SCORE_SHARED_INSTITUTION_MATCH : SCORE_NAME_MATCH;
      evidence.push(`La glosa menciona ${candidate.matchedName}`);
    }
    if (candidate.balanceMatch) {
      score += SCORE_BALANCE_MATCH;
      evidence.push(`Depósito similar en ${target.productName}`);
      const gap = daysBetween(source.date, candidate.balanceMatch.date);
      score += dateScore(gap);
      evidence.push(dayGapLabel(gap));
    }
  }

  const described = [source.description, target.description].filter(
    (d): d is string => d !== null
  );
  const withKeyword = described.find((d) => findTransferKeyword(d) !== null);
  if (withKeyword) {
    score += SCORE_KEYWORD;
    evidence.push(quoteDescription(withKeyword));
  }

  const samePair = patterns.filter(
    (p) =>
      p.sourceProductId === source.productId &&
      p.targetProductId === target.productId
  );
  const prefix = descriptionPrefix(source.description);
  if (samePair.some((p) => p.descriptionPrefix === prefix)) {
    score += SCORE_PATTERN;
    evidence.push("Patrón confirmado antes");
  } else if (samePair.length > 0) {
    score += SCORE_PRODUCT_PAIR;
    evidence.push("Mismos productos que una transferencia confirmada");
  }

  return { score, evidence };
}

/** Whether a confirmed pattern matches `source` exactly (same products and
 *  description prefix): such a debit is a one-sided candidate even when its
 *  description names no product. */
export function matchesPattern(
  source: Pick<SuggestionTransactionLeg, "productId" | "description">,
  targetProductId: string,
  patterns: readonly ConfirmedPattern[]
): boolean {
  const prefix = descriptionPrefix(source.description);
  return patterns.some(
    (p) =>
      p.sourceProductId === source.productId &&
      p.targetProductId === targetProductId &&
      p.descriptionPrefix === prefix
  );
}

/** The suggestion's stable key, as the UI keys its rows. */
export function suggestionId(candidate: SuggestionCandidate): string {
  const target =
    candidate.kind === "pair"
      ? candidate.target.transactionId
      : candidate.target.productId;
  return `${candidate.kind}:${candidate.source.transactionId}:${target}`;
}

/** Score every candidate and attach its key and amount. */
export function buildSuggestions(
  candidates: readonly SuggestionCandidate[],
  patterns: readonly ConfirmedPattern[] = []
): TransferSuggestion[] {
  return sortSuggestions(
    candidates.map((candidate) => ({
      ...candidate,
      ...scoreSuggestion(candidate, patterns),
      id: suggestionId(candidate),
      amount: Math.abs(candidate.source.amount),
    }))
  );
}

/** The suggestions that use `transactionId` on either side. */
export function suggestionsForTransaction<
  T extends {
    source: { transactionId: string };
    target: { transactionId: string | null };
  },
>(suggestions: readonly T[], transactionId: string): T[] {
  return suggestions.filter(
    (s) =>
      s.source.transactionId === transactionId ||
      s.target.transactionId === transactionId
  );
}

/**
 * The list once `settled` was confirmed or dismissed, before the refetch
 * lands, so a stale row cannot be clicked into a 409:
 *   - confirm: every suggestion using either of its rows (they are linked now)
 *   - dismiss a pair: that pair only (its siblings are still valid)
 *   - dismiss a one-sided suggestion: every one-sided one for that source
 */
export function withoutSettled<T extends TransferSuggestion>(
  suggestions: readonly T[],
  settled: TransferSuggestion,
  action: "confirm" | "dismiss"
): T[] {
  if (action === "confirm") {
    const used = new Set(
      [settled.source.transactionId, settled.target.transactionId].filter(
        (id): id is string => id !== null
      )
    );
    return suggestions.filter(
      (s) =>
        !used.has(s.source.transactionId) &&
        !(s.target.transactionId !== null && used.has(s.target.transactionId))
    );
  }
  if (settled.kind === "pair") {
    return suggestions.filter((s) => s.id !== settled.id);
  }
  return suggestions.filter(
    (s) =>
      !(
        s.kind === "one_sided" &&
        s.source.transactionId === settled.source.transactionId
      )
  );
}

/** Whether a suggestion is kept by a search's words (see
 *  lib/transaction-search): each in the raw or display description of one of
 *  its rows. */
export function suggestionMatchesSearch(
  tokens: readonly string[],
  suggestion: {
    source: Pick<SuggestionTransactionLeg, "description" | "displayDescription">;
    target: Pick<
      SuggestionTransactionLeg | SuggestionProductLeg,
      "description" | "displayDescription"
    >;
  }
): boolean {
  const { source, target } = suggestion;
  return matchesTransactionSearch(
    tokens,
    source.description,
    source.displayDescription,
    target.description,
    target.displayDescription
  );
}

/** Whether `suggestion` is confident enough to link on its own: a pair (two
 *  real rows) scoring at least AUTO_LINK_MIN_SCORE. */
export function isAutoLinkable(
  suggestion: Pick<TransferSuggestion, "kind" | "score">
): boolean {
  return suggestion.kind === "pair" && suggestion.score >= AUTO_LINK_MIN_SCORE;
}

/** The suggestions to link without asking: auto-linkable ones that share
 *  neither row with any other suggestion, since a row with two candidate
 *  partners is the user's call. They never share rows with each other. */
export function pickAutoLinks<T extends TransferSuggestion>(
  suggestions: readonly T[]
): T[] {
  const rowsOf = (s: T) =>
    [s.source.transactionId, s.target.transactionId].filter(
      (id): id is string => id !== null
    );
  const uses = new Map<string, number>();
  for (const s of suggestions) {
    for (const id of rowsOf(s)) uses.set(id, (uses.get(id) ?? 0) + 1);
  }
  return suggestions.filter(
    (s) => isAutoLinkable(s) && rowsOf(s).every((id) => uses.get(id) === 1)
  );
}

/** Highest score first, then the most recent source row, then by key so the
 *  order is stable. Returns a new array. */
export function sortSuggestions<
  T extends { score: number; id: string; source: { date: string } },
>(suggestions: readonly T[]): T[] {
  return [...suggestions].sort(
    (a, b) =>
      b.score - a.score ||
      b.source.date.localeCompare(a.source.date) ||
      a.id.localeCompare(b.id)
  );
}
