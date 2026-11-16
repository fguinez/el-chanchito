// The credential scheme the dashboard seals with, as plain strings. Client
// safe (no key material, no crypto): the Credenciales page and the save
// blockers use it next to seal.ts, which does the sealing server side.

// Must match apps/scrapers/scrapers/credentials.py. The algorithm is what
// the scraper service publishes next to its key; the scheme, stored on every
// row, names the whole construction sealed to that key.
export const CREDENTIAL_SCHEME_V2 = "hpke-mlkem768x25519-sha256-aes256gcm-v2";
export const CREDENTIAL_ALGORITHM_V2 = "HPKE-Base(MLKEM768-X25519, HKDF-SHA256, AES-256-GCM)";

const SCHEME_BY_ALGORITHM = new Map([[CREDENTIAL_ALGORITHM_V2, CREDENTIAL_SCHEME_V2]]);

/** The scheme rows sealed to a key with this published algorithm carry, or
 *  null for an algorithm the dashboard cannot seal to (an older install's v1
 *  key, until the scraper service rotates it). */
export function schemeForAlgorithm(algorithm: string): string | null {
  return SCHEME_BY_ALGORITHM.get(algorithm) ?? null;
}

// An older install's v1 key: never sealed to here, but its rows still open
// until the scraper service rotates it, so they still count as stored.
const ROW_SCHEME_BY_ALGORITHM = new Map([
  ...SCHEME_BY_ALGORITHM,
  ["HPKE-Base(DHKEM-X25519-HKDF-SHA256, HKDF-SHA256, AES-256-GCM)", "hpke-x25519-sha256-aes256gcm-v1"],
]);

/** The scheme a stored row under a key with this algorithm must carry to
 *  open, v1 included; null for an algorithm neither side knows. */
export function rowSchemeForAlgorithm(algorithm: string): string | null {
  return ROW_SCHEME_BY_ALGORITHM.get(algorithm) ?? null;
}
