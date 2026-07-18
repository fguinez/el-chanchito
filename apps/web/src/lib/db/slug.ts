/**
 * Institution-unique product slugs.
 *
 * Pure string helpers, no db imports. The scraper writer implements the same
 * canonical spec in Python (apps/scrapers/db/slug.py) so both product
 * creation paths mint identical slugs; keep the two in sync.
 */

/**
 * URL-safe slug for a product name.
 *
 * NFKD-normalize and drop non-ASCII (folding accents, e.g. "crédito" ->
 * "credito"), lowercase, then collapse every run outside [a-z0-9] into a
 * single hyphen. A name with nothing to keep (e.g. symbols only) falls back
 * to the kind with underscores hyphenated, which is never empty.
 */
export function slugify(name: string, kind: string): string {
  const asciiName = name.normalize("NFKD").replace(/[^\x00-\x7F]/g, "");
  const slug = asciiName
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return slug || kind.replace(/_/g, "-");
}

/** `base` if free, else the first free `base-n` for n = 2, 3, 4, ... */
export function uniqueSlug(base: string, taken: Set<string>): string {
  if (!taken.has(base)) {
    return base;
  }
  let n = 2;
  while (taken.has(`${base}-${n}`)) {
    n += 1;
  }
  return `${base}-${n}`;
}
