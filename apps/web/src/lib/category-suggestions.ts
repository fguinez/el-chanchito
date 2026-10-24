// Category suggestions for uncategorized transactions, learned from the ones
// already categorized (by hand or by a rule). Pure scoring and wording; the
// queries live in lib/category-suggestions-db. Nothing here applies a
// category: a suggestion is only shown, and the user applies or ignores it.
//
// Matching keys on the raw scraped `transactions.description`, never on the
// displayed override or alias.

/** Two descriptions with this similarity or less do not vote at all. */
export const MIN_SIMILARITY = 0.5;

/** A suggestion is shown only at this confidence or above (see
 *  `suggestCategory`). */
export const MIN_CONFIDENCE = 0.6;

/** Tokens shorter than this are noise ("sa", "de", "cl"). */
export const MIN_TOKEN_LENGTH = 3;

/** Words common to descriptions of every kind, so sharing them says nothing
 *  about the category: Spanish connectors, company suffixes and movement
 *  verbs. */
const STOPWORDS = new Set([
  "abono",
  "cargo",
  "cia",
  "com",
  "compra",
  "con",
  "del",
  "eirl",
  "las",
  "los",
  "ltda",
  "pago",
  "para",
  "por",
  "spa",
  "www",
]);

/**
 * The description reduced to the words that identify a merchant: lowercase,
 * accents dropped, every token holding a digit removed (dates, amounts,
 * reference and card numbers), punctuation turned into spaces and whitespace
 * collapsed. "COMERCIO EJEMPLO SPA 1234" and "Comercio Ejemplo SpA #5678"
 * both become "comercio ejemplo spa".
 */
export function normalizeDescription(text: string): string {
  return text
    .normalize("NFD")
    .replace(/\p{M}/gu, "")
    .toLowerCase()
    .split(/\s+/)
    .filter((token) => !/[0-9]/.test(token))
    .join(" ")
    .replace(/[^a-z]+/g, " ")
    .trim();
}

/** The distinct meaningful tokens of a normalised description: at least
 *  MIN_TOKEN_LENGTH letters and not a stopword. */
export function descriptionTokens(normalized: string): string[] {
  const tokens = normalized
    .split(" ")
    .filter((t) => t.length >= MIN_TOKEN_LENGTH && !STOPWORDS.has(t));
  return [...new Set(tokens)];
}

/**
 * How alike two token sets are, 0 to 1: the mean of the Jaccard index
 * (shared / all tokens) and the overlap coefficient (shared / tokens of the
 * shorter one), so a description that extends another ("supermercado
 * ficticio sucursal centro" over "supermercado ficticio") still scores well,
 * while two that share one word out of several do not.
 */
export function tokenSimilarity(a: readonly string[], b: readonly string[]): number {
  if (a.length === 0 || b.length === 0) return 0;
  const setB = new Set(b);
  const shared = a.filter((t) => setB.has(t)).length;
  if (shared === 0) return 0;
  const jaccard = shared / (a.length + b.length - shared);
  const overlap = shared / Math.min(a.length, b.length);
  return (jaccard + overlap) / 2;
}

/** Categorized rows with one description (as stored, any case) and one
 *  category. */
export interface CategorizedGroup {
  description: string;
  categoryId: string;
  count: number;
}

interface IndexedEntry {
  normalized: string;
  tokens: string[];
  categoryId: string;
  count: number;
}

/** The categorized history, merged by normalised description and category,
 *  with a token lookup so scoring only compares descriptions that share a
 *  word. */
export interface HistoryIndex {
  byNormalized: Map<string, IndexedEntry[]>;
  byToken: Map<string, IndexedEntry[]>;
}

export function buildHistoryIndex(groups: readonly CategorizedGroup[]): HistoryIndex {
  const merged = new Map<string, IndexedEntry>();
  for (const group of groups) {
    const normalized = normalizeDescription(group.description);
    const tokens = descriptionTokens(normalized);
    if (tokens.length === 0 || group.count <= 0) continue;
    const key = `${normalized}\u0000${group.categoryId}`;
    const entry = merged.get(key);
    if (entry) entry.count += group.count;
    else merged.set(key, { normalized, tokens, categoryId: group.categoryId, count: group.count });
  }
  const byNormalized = new Map<string, IndexedEntry[]>();
  const byToken = new Map<string, IndexedEntry[]>();
  for (const entry of merged.values()) {
    push(byNormalized, entry.normalized, entry);
    for (const token of entry.tokens) push(byToken, token, entry);
  }
  return { byNormalized, byToken };
}

function push<K, V>(map: Map<K, V[]>, key: K, value: V) {
  const list = map.get(key);
  if (list) list.push(value);
  else map.set(key, [value]);
}

export interface CategorySuggestion {
  categoryId: string;
  categoryName: string;
  /** 0 to 1: the winning category's share of the weighted votes times its
   *  best similarity. */
  confidence: number;
  /** Categorized rows behind the winning category. */
  matchCount: number;
  /** Whether every one of them has the same normalised description. */
  exact: boolean;
  /** Short Spanish explanation, e.g. "3 transacciones similares están en
   *  Supermercado". */
  reason: string;
}

/**
 * The category to suggest for `description`, or null when nothing is
 * confident enough. Every categorized description with a similarity above
 * MIN_SIMILARITY (1 for the same normalised description) votes for its
 * category with weight similarity x row count. The winner's confidence is its
 * share of the votes times its best similarity; below MIN_CONFIDENCE, or for
 * a category missing from `categoryNames`, there is no suggestion. Ties go to
 * the category with more rows, then the lower id, so the result is stable.
 */
export function suggestCategory(
  description: string,
  index: HistoryIndex,
  categoryNames: ReadonlyMap<string, string>
): CategorySuggestion | null {
  const normalized = normalizeDescription(description);
  const tokens = descriptionTokens(normalized);
  if (tokens.length === 0) return null;

  const candidates = new Set<IndexedEntry>();
  for (const token of tokens) {
    for (const entry of index.byToken.get(token) ?? []) candidates.add(entry);
  }

  const votes = new Map<
    string,
    { weight: number; count: number; exactCount: number; best: number }
  >();
  let total = 0;
  for (const entry of candidates) {
    const exact = entry.normalized === normalized;
    const similarity = exact ? 1 : tokenSimilarity(tokens, entry.tokens);
    if (similarity <= MIN_SIMILARITY) continue;
    const weight = similarity * entry.count;
    total += weight;
    const vote = votes.get(entry.categoryId) ?? { weight: 0, count: 0, exactCount: 0, best: 0 };
    vote.weight += weight;
    vote.count += entry.count;
    if (exact) vote.exactCount += entry.count;
    vote.best = Math.max(vote.best, similarity);
    votes.set(entry.categoryId, vote);
  }

  let winner: [string, { weight: number; count: number; exactCount: number; best: number }] | null =
    null;
  for (const item of votes) {
    if (
      !winner ||
      item[1].weight > winner[1].weight ||
      (item[1].weight === winner[1].weight &&
        (item[1].count > winner[1].count ||
          (item[1].count === winner[1].count && item[0] < winner[0])))
    ) {
      winner = item;
    }
  }
  if (!winner) return null;
  const [categoryId, vote] = winner;
  const categoryName = categoryNames.get(categoryId);
  if (categoryName === undefined) return null;
  const confidence = (vote.weight / total) * vote.best;
  if (confidence < MIN_CONFIDENCE) return null;
  const exact = vote.exactCount === vote.count;
  return {
    categoryId,
    categoryName,
    confidence: Math.round(confidence * 100) / 100,
    matchCount: vote.count,
    exact,
    reason: suggestionReason(vote.count, exact, categoryName),
  };
}

/** "1 transacción con la misma descripción está en X" / "3 transacciones
 *  similares están en X". */
export function suggestionReason(count: number, exact: boolean, categoryName: string): string {
  const n = count.toLocaleString("es-CL");
  if (exact) {
    return count === 1
      ? `1 transacción con la misma descripción está en ${categoryName}`
      : `${n} transacciones con la misma descripción están en ${categoryName}`;
  }
  return count === 1
    ? `1 transacción similar está en ${categoryName}`
    : `${n} transacciones similares están en ${categoryName}`;
}

/** The key a dismissal is stored and looked up under (V042). */
export function dismissalKey(normalizedDescription: string, categoryId: string): string {
  return `${normalizedDescription}\u0000${categoryId.toLowerCase()}`;
}
