/**
 * Signed session tokens for the single-user dashboard.
 *
 * There is exactly one secret: DASHBOARD_PASSWORD. The HMAC key is derived from
 * it with PBKDF2, so changing the password invalidates every outstanding
 * session. Tokens carry their own absolute expiry: `{expiresAtMs}.{hexSignature}`.
 *
 * Only Web Crypto is used, so this works unchanged in route handlers and in
 * proxy.ts. Never log tokens, signatures or the password itself.
 */

const KEY_CONTEXT = "el-chanchito/dashboard-session/v1:";

/**
 * PBKDF2 parameters. The salt is fixed (there is exactly one account, so a
 * random salt would have nowhere to live) and the iteration count is what makes
 * a leaked cookie expensive to attack: the token is known plaintext, so a fast
 * MAC would let an attacker recover the password offline at GH/s and try it
 * elsewhere.
 */
const PBKDF2_ITERATIONS = 210_000;
const PBKDF2_SALT = `${KEY_CONTEXT}pbkdf2-salt/v1`;

/** Derived keys are cached, so this only bounds a pathological key churn. */
const KEY_CACHE_LIMIT = 8;

const encoder = new TextEncoder();

function toHex(bytes: ArrayBuffer): string {
  return Array.from(new Uint8Array(bytes))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

/**
 * Memoized per password: PBKDF2 is deliberately slow, and `deriveKey` runs on
 * every authenticated request. Keying the cache on the password itself means a
 * password change can never be served a stale key (it simply misses the cache);
 * the password is already in the process environment, so the map adds no new
 * exposure.
 */
const keyCache = new Map<string, Promise<CryptoKey>>();

async function derive(password: string): Promise<CryptoKey> {
  const material = await crypto.subtle.importKey(
    "raw",
    encoder.encode(KEY_CONTEXT + password),
    "PBKDF2",
    false,
    ["deriveBits"]
  );
  const bits = await crypto.subtle.deriveBits(
    {
      name: "PBKDF2",
      salt: encoder.encode(PBKDF2_SALT),
      iterations: PBKDF2_ITERATIONS,
      hash: "SHA-256",
    },
    material,
    256
  );
  return crypto.subtle.importKey(
    "raw",
    bits,
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
}

/** PBKDF2-SHA256 over a static context prefix plus the password. */
function deriveKey(password: string): Promise<CryptoKey> {
  const cached = keyCache.get(password);
  if (cached) return cached;

  const pending = derive(password).catch((error) => {
    // Never cache a failure: the next request must be able to retry.
    keyCache.delete(password);
    throw error;
  });

  if (keyCache.size >= KEY_CACHE_LIMIT) keyCache.clear();
  keyCache.set(password, pending);
  return pending;
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
