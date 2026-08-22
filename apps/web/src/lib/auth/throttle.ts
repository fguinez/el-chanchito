/**
 * Brute-force protection for the single shared login password.
 *
 * Two layers, both process-global because the dashboard has exactly one account:
 *
 * 1. A promise mutex serializing every attempt, which turns the per-request
 *    failure delay into a real global ceiling (~2 attempts/s). Without it the
 *    delay throttles nothing: concurrent attempts just wait in parallel.
 * 2. Progressive lockout after a run of failures, answering `429` with
 *    `Retry-After` and backing off exponentially. A single global counter is the
 *    right shape here: there is one secret to guess, and it sidesteps the
 *    IP-spoofing games a per-client counter invites.
 *
 * A successful login clears both the counter and any active lockout.
 */

export interface ThrottleOptions {
  /** Delay applied to every failed attempt, inside the mutex. */
  failureDelayMs?: number;
  /** Consecutive failures tolerated before lockouts start. */
  lockoutAfterFailures?: number;
  /** First lockout window; doubles per extra failure. */
  baseLockoutMs?: number;
  /** Ceiling for the exponential backoff. */
  maxLockoutMs?: number;
  /** Injectable for tests: no wall-clock dependence in the suite. */
  now?: () => number;
  sleep?: (ms: number) => Promise<void>;
}

export const DEFAULT_FAILURE_DELAY_MS = 500;
export const DEFAULT_LOCKOUT_AFTER_FAILURES = 5;
export const DEFAULT_BASE_LOCKOUT_MS = 5_000;
export const DEFAULT_MAX_LOCKOUT_MS = 5 * 60_000;

/** What the caller should do with the attempt it just handed over. */
export type ThrottleOutcome<T> =
  | { status: "ran"; result: T }
  | { status: "locked"; retryAfterSeconds: number };

function defaultSleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export class LoginThrottle {
  private readonly failureDelayMs: number;
  private readonly lockoutAfterFailures: number;
  private readonly baseLockoutMs: number;
  private readonly maxLockoutMs: number;
  private readonly now: () => number;
  private readonly sleep: (ms: number) => Promise<void>;

  /** Tail of the serialization chain; every attempt appends itself to it. */
  private queue: Promise<unknown> = Promise.resolve();
  private consecutiveFailures = 0;
  private lockedUntilMs = 0;

  constructor(options: ThrottleOptions = {}) {
    this.failureDelayMs = options.failureDelayMs ?? DEFAULT_FAILURE_DELAY_MS;
    this.lockoutAfterFailures =
      options.lockoutAfterFailures ?? DEFAULT_LOCKOUT_AFTER_FAILURES;
    this.baseLockoutMs = options.baseLockoutMs ?? DEFAULT_BASE_LOCKOUT_MS;
    this.maxLockoutMs = options.maxLockoutMs ?? DEFAULT_MAX_LOCKOUT_MS;
    this.now = options.now ?? (() => Date.now());
    this.sleep = options.sleep ?? defaultSleep;
  }

  /**
   * Runs `attempt` with at most one attempt in flight at a time. `attempt`
   * resolves to whether the credentials were valid; a failure is delayed and
   * counted before the mutex is released, so the delay throttles the next
   * caller too.
   */
  async run<T>(
    attempt: () => Promise<{ ok: boolean; result: T }>
  ): Promise<ThrottleOutcome<T>> {
    const previous = this.queue;
    let release!: () => void;
    this.queue = new Promise<void>((resolve) => {
      release = resolve;
    });

    // Wait for our turn. A rejected predecessor must not break the chain.
    await previous.catch(() => undefined);

    try {
      const remainingMs = this.lockedUntilMs - this.now();
      if (remainingMs > 0) {
        return {
          status: "locked",
          retryAfterSeconds: Math.max(1, Math.ceil(remainingMs / 1000)),
        };
      }

      const { ok, result } = await attempt();

      if (ok) {
        this.consecutiveFailures = 0;
        this.lockedUntilMs = 0;
      } else {
        this.consecutiveFailures += 1;
        this.applyLockout();
        await this.sleep(this.failureDelayMs);
      }

      return { status: "ran", result };
    } finally {
      release();
    }
  }

  /** Arms the next lockout window once the failure run crosses the threshold. */
  private applyLockout(): void {
    const over = this.consecutiveFailures - this.lockoutAfterFailures;
    if (over < 0) return;

    const window = Math.min(this.baseLockoutMs * 2 ** over, this.maxLockoutMs);
    this.lockedUntilMs = this.now() + window;
  }

  /** Test seam only: forget all accumulated state. */
  reset(): void {
    this.consecutiveFailures = 0;
    this.lockedUntilMs = 0;
  }
}

/** The process-wide throttle guarding POST /api/auth/login. */
export const loginThrottle = new LoginThrottle();
