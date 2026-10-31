// Text search over transaction lists. The client-side filters and the SQL
// condition the API routes build (`descriptionMatches` in lib/db/transactions)
// follow these same rules, so a query finds the same rows either way: a row
// matches when every word of the query appears, ignoring case and accents, in
// its raw scraped description or in its display description (the custom
// override or alias); different words may match different fields. Matching
// is literal substring matching: `%` and `_` are plain characters.

/** Longest search text the API accepts (see `searchQuerySchema`). */
export const SEARCH_QUERY_MAX_LENGTH = 200;

/** Words of a query past this many are ignored. */
export const MAX_SEARCH_TOKENS = 8;

/** The combining diacritics NFD splits off accented Latin letters. The SQL
 *  fold (`foldForSearch`) strips the same range, so both sides agree. */
const COMBINING_MARKS = /[\u0300-\u036f]/g;

/** NFD, without combining accents, lowercase: the form a query and the text
 *  it searches are compared in. Unlike transfer-suggestions' normalizeText,
 *  punctuation and spacing stay, so "s.a." only matches "s.a.". */
export function normalizeSearchText(text: string): string {
  return text.normalize("NFD").replace(COMBINING_MARKS, "").toLowerCase();
}

/** The normalized, distinct words of a query (at most MAX_SEARCH_TOKENS);
 *  empty for a blank query, which filters nothing. */
export function searchTokens(query: string | null | undefined): string[] {
  if (!query) return [];
  const words = normalizeSearchText(query).split(/\s+/).filter(Boolean);
  return [...new Set(words)].slice(0, MAX_SEARCH_TOKENS);
}

/** Whether every word of `query` (the raw text, or its searchTokens when a
 *  list filters many rows) appears in at least one of `fields`. A blank
 *  query matches every row; null fields are skipped. */
export function matchesTransactionSearch(
  query: string | readonly string[],
  ...fields: (string | null | undefined)[]
): boolean {
  const tokens = typeof query === "string" ? searchTokens(query) : query;
  if (tokens.length === 0) return true;
  const haystacks = fields
    .filter((field): field is string => !!field)
    .map(normalizeSearchText);
  return tokens.every((token) => haystacks.some((text) => text.includes(token)));
}

/** A tooltip (`title`) for a row shown by its `shown` description: the row's
 *  `other` description too, labelled, when it differs, so a search that matched
 *  the text off screen still reads as a match. Rows shown by their display
 *  description pass the raw one (the default label); rows shown raw pass the
 *  display one with their own label. */
export function descriptionTitle(
  shown: string,
  other: string | null | undefined,
  otherLabel = "Descripción original"
): string {
  return other && other !== shown ? `${shown}\n${otherLabel}: ${other}` : shown;
}
