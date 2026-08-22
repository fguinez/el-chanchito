/**
 * Signed session tokens for the single-user dashboard.
 *
 * There is exactly one secret: DASHBOARD_PASSWORD. The HMAC key is derived from
 * it, so changing the password invalidates every outstanding session. Tokens
 * carry their own absolute expiry: `{expiresAtMs}.{hexSignature}`.
 *
 * Only Web Crypto is used, so this works unchanged in route handlers and in
 * proxy.ts. Never log tokens, signatures or the password itself.
 */

const KEY_CONTEXT = "el-chanchito/dashboard-session/v1:";

const encoder = new TextEncoder();

function toHex(bytes: ArrayBuffer): string {
  return Array.from(new Uint8Array(bytes))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

/** SHA-256 of a static context prefix plus the password. */
async function deriveKey(password: string): Promise<CryptoKey> {
  const material = await crypto.subtle.digest(
    "SHA-256",
    encoder.encode(KEY_CONTEXT + password)
  );
  return crypto.subtle.importKey(
    "raw",
    material,
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
}

async function sign(password: string, payload: string): Promise<string> {
  const key = await deriveKey(password);
  return toHex(await crypto.subtle.sign("HMAC", key, encoder.encode(payload)));
}

/** Length-independent, content-constant-time string comparison. */
export function timingSafeEqual(a: string, b: string): boolean {
  const aBytes = encoder.encode(a);
  const bBytes = encoder.encode(b);
  // Compare over a fixed length so only the (public) lengths can leak.
  const length = Math.max(aBytes.length, bBytes.length);
  let diff = aBytes.length ^ bBytes.length;
  for (let i = 0; i < length; i += 1) {
    diff |= (aBytes[i] ?? 0) ^ (bBytes[i] ?? 0);
  }
  return diff === 0;
}

/**
 * Checks a submitted password against the configured one. Both sides are hashed
 * first so the comparison time never depends on the shared prefix length.
 */
export async function verifyPassword(
  candidate: string,
  expected: string
): Promise<boolean> {
  const [candidateHash, expectedHash] = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(candidate)),
    crypto.subtle.digest("SHA-256", encoder.encode(expected)),
  ]);
  return timingSafeEqual(toHex(candidateHash), toHex(expectedHash));
}

/** Mints a token valid for `lifetimeSeconds` from `now`. */
export async function createSessionToken(
  password: string,
  lifetimeSeconds: number,
  now: number = Date.now()
): Promise<string> {
  const expiresAtMs = now + lifetimeSeconds * 1000;
  const payload = String(expiresAtMs);
  return `${payload}.${await sign(password, payload)}`;
}

/** True when the token is well formed, correctly signed and not expired. */
export async function verifySessionToken(
  token: string | undefined | null,
  password: string,
  now: number = Date.now()
): Promise<boolean> {
  if (!token) return false;

  const separator = token.indexOf(".");
  if (separator <= 0) return false;

  const payload = token.slice(0, separator);
  const signature = token.slice(separator + 1);
  if (!/^\d+$/.test(payload) || signature.length === 0) return false;

  const expected = await sign(password, payload);
  if (!timingSafeEqual(signature, expected)) return false;

  return Number(payload) > now;
}
