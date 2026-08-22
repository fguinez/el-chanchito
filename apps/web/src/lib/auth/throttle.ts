/**
 * Brute-force protection for the single shared login password.
 *
 * The dashboard has exactly one account and exactly one way in, so availability
 * matters as much as guessing resistance: nothing a remote party does may leave
 * the owner unable to log in. Three layers, in order of how much an attacker can
 * influence them:
 *
 * 1. A **global** promise mutex serializing every attempt, which turns the
 *    per-request failure delay into a real ceiling (~2 attempts/s). Without it
 *    the delay throttles nothing: concurrent attempts just wait in parallel.
 *    Nobody can spoof their way past this, and it applies to distributed
 *    attacks too, which is why it (and not a global lockout) is the backstop.
 * 2. A **global** queue bound, so a flood cannot park the owner's request behind
 *    an arbitrarily long chain of 500 ms failures, nor grow memory without end.
 * 3. A **per-client** progressive lockout answering `429` with `Retry-After`.
 *    Scoped per client so a lockout an attacker triggers falls on the identity
 *    it used, not on the owner. Client identity is only as good as the network
 *    tells us (see `loginClientKey`), which is exactly why the un-spoofable
 *    ceiling above is layer 1 and the lockout is layer 3.
 *
 * The counter decays after a quiet period, and a client that has been
 * continuously locked for longer than `maxLockedStreakMs` stops being refused
 * before its password is checked: from then on a correct password gets in while
 * wrong ones still get `429`. Together those two bound how long any single
 * client can be kept out, so "one wrong guess per window, forever" can no longer
 * turn into a permanent denial of the owner's only entrance.
 *
 * A successful login clears that client's state entirely.
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
  /** Quiet period after which a client's failure counter is forgotten. */
  failureDecayMs?: number;
  /**
   * How long a client may be kept locked before the lockout stops short
   * circuiting password verification. Bounds a targeted denial of service.
   */
  maxLockedStreakMs?: number;
  /** Attempts allowed to wait for the mutex before new ones are turned away. */
  maxQueueDepth?: number;
  /** Upper bound on tracked client identities. */
  maxTrackedClients?: number;
  /** Injectable for tests: no wall-clock dependence in the suite. */
  now?: () => number;
  sleep?: (ms: number) => Promise<void>;
}

export const DEFAULT_FAILURE_DELAY_MS = 500;
export const DEFAULT_LOCKOUT_AFTER_FAILURES = 5;
export const DEFAULT_BASE_LOCKOUT_MS = 5_000;
export const DEFAULT_MAX_LOCKOUT_MS = 5 * 60_000;
export const DEFAULT_FAILURE_DECAY_MS = 15 * 60_000;
export const DEFAULT_MAX_LOCKED_STREAK_MS = 30 * 60_000;
export const DEFAULT_MAX_QUEUE_DEPTH = 8;
export const DEFAULT_MAX_TRACKED_CLIENTS = 1_000;

/** What the caller should do with the attempt it just handed over. */
export type ThrottleOutcome<T> =
  | { status: "ran"; result: T }
  | {
      status: "locked";
      retryAfterSeconds: number;
      /** `queue` means the process is saturated, not that this client misbehaved. */
      reason: "lockout" | "queue";
    };

function defaultSleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * The identity a lockout is scoped to.
 *
 * With a trusted proxy in front, the client-most entry of `X-Forwarded-For` is
 * the client address the proxy observed. Without one the header is not
 * trustworthy, but it is still the only view of the peer a route handler gets:
 * `next start` fills it from the connection's remote address when the client
 * sent none (`NextRequest.ip` does not exist since Next 15), so unforged
 * requests key on the real connection IP, and a forged value simply buys the
 * attacker its own bucket. That is the whole point of scoping per client: a
 * spoofer locks out the identity it invented, never the owner.
 */
export function loginClientKey(
  headers: Pick<Headers, "get">,
  trustProxy: boolean
): string {
  const raw = headers.get("x-forwarded-for") ?? "";
  const candidate = trustProxy ? (raw.split(",")[0] ?? "") : raw;
  // Bounded so a long header cannot bloat the map's keys.
  return candidate.trim().toLowerCase().slice(0, 100) || "unknown";
}

interface ClientState {
  consecutiveFailures: number;
  lockedUntilMs: number;
  lastFailureAtMs: number;
  /** When the current unbroken run of lockouts began; 0 when there is none. */
  lockedStreakStartedAtMs: number;
}

export class LoginThrottle {
  private readonly failureDelayMs: number;
  private readonly lockoutAfterFailures: number;
  private readonly baseLockoutMs: number;
  private readonly maxLockoutMs: number;
  private readonly failureDecayMs: number;
  private readonly maxLockedStreakMs: number;
  private readonly maxQueueDepth: number;
  private readonly maxTrackedClients: number;
  private readonly now: () => number;
  private readonly sleep: (ms: number) => Promise<void>;

  /** Tail of the serialization chain; every attempt appends itself to it. */
  private queue: Promise<unknown> = Promise.resolve();
  private queueDepth = 0;
  private readonly clients = new Map<string, ClientState>();

  constructor(options: ThrottleOptions = {}) {
    this.failureDelayMs = options.failureDelayMs ?? DEFAULT_FAILURE_DELAY_MS;
    this.lockoutAfterFailures =
      options.lockoutAfterFailures ?? DEFAULT_LOCKOUT_AFTER_FAILURES;
    this.baseLockoutMs = options.baseLockoutMs ?? DEFAULT_BASE_LOCKOUT_MS;
    this.maxLockoutMs = options.maxLockoutMs ?? DEFAULT_MAX_LOCKOUT_MS;
    this.failureDecayMs = options.failureDecayMs ?? DEFAULT_FAILURE_DECAY_MS;
    this.maxLockedStreakMs =
      options.maxLockedStreakMs ?? DEFAULT_MAX_LOCKED_STREAK_MS;
    this.maxQueueDepth = options.maxQueueDepth ?? DEFAULT_MAX_QUEUE_DEPTH;
    this.maxTrackedClients =
      options.maxTrackedClients ?? DEFAULT_MAX_TRACKED_CLIENTS;
    this.now = options.now ?? (() => Date.now());
    this.sleep = options.sleep ?? defaultSleep;
  }

  /**
   * Runs `attempt` for `clientKey` with at most one attempt in flight at a time.
   * `attempt` resolves to whether the credentials were valid; a failure is
   * delayed and counted before the mutex is released, so the delay throttles the
   * next caller too.
   */
  async run<T>(
    clientKey: string,
    attempt: () => Promise<{ ok: boolean; result: T }>
  ): Promise<ThrottleOutcome<T>> {
    if (this.queueDepth >= this.maxQueueDepth) {
      // Saturated: turning this away immediately is what keeps the owner's own
      // attempt from queueing behind a flood of 500 ms failures.
      return { status: "locked", retryAfterSeconds: 1, reason: "queue" };
    }

    this.queueDepth += 1;
    const previous = this.queue;
    let release!: () => void;
    this.queue = new Promise<void>((resolve) => {
      release = resolve;
    });

    // Wait for our turn. A rejected predecessor must not break the chain.
    await previous.catch(() => undefined);

    try {
      const state = this.stateFor(clientKey);
      const remainingMs = state.lockedUntilMs - this.now();
      const locked = remainingMs > 0;
      const retryAfterSeconds = Math.max(1, Math.ceil(remainingMs / 1000));

      // While locked the password is normally not even looked at. Once this
      // client has been locked for longer than the streak bound the check runs
      // anyway, so a correct password always eventually gets in.
      if (locked && !this.streakExceeded(state)) {
        return { status: "locked", retryAfterSeconds, reason: "lockout" };
      }

      const { ok, result } = await attempt();

      if (ok) {
        this.clients.delete(clientKey);
        return { status: "ran", result };
      }

      if (locked) {
        // A wrong guess arriving while locked is still refused, and (as before)
        // does not extend the window it is already serving.
        await this.sleep(this.failureDelayMs);
        return { status: "locked", retryAfterSeconds, reason: "lockout" };
      }

      state.consecutiveFailures += 1;
      state.lastFailureAtMs = this.now();
      this.applyLockout(state);
      await this.sleep(this.failureDelayMs);

      return { status: "ran", result };
    } finally {
      this.queueDepth -= 1;
      release();
    }
  }

  /**
   * This client's state, decayed and with the map kept bounded. A client whose
   * last failure is older than the decay window starts from zero, so the backoff
   * can never ratchet up permanently.
   */
  private stateFor(clientKey: string): ClientState {
    const now = this.now();
    this.evict(now);

    const existing = this.clients.get(clientKey);
    if (existing) {
      if (this.isDecayed(existing, now)) {
        existing.consecutiveFailures = 0;
        existing.lockedUntilMs = 0;
        existing.lastFailureAtMs = 0;
        existing.lockedStreakStartedAtMs = 0;
      }
      return existing;
    }

    const fresh: ClientState = {
      consecutiveFailures: 0,
      lockedUntilMs: 0,
      lastFailureAtMs: 0,
      lockedStreakStartedAtMs: 0,
    };
    this.clients.set(clientKey, fresh);
    return fresh;
  }

  /** Nothing left worth remembering: no live lock and no recent failure. */
  private isDecayed(state: ClientState, now: number): boolean {
    if (state.lockedUntilMs > now) return false;
    return now - state.lastFailureAtMs >= this.failureDecayMs;
  }

  private streakExceeded(state: ClientState): boolean {
    if (state.lockedStreakStartedAtMs === 0) return false;
    return this.now() - state.lockedStreakStartedAtMs >= this.maxLockedStreakMs;
  }

  /** Drops decayed entries, then makes room if the map is still at its bound. */
  private evict(now: number): void {
    for (const [key, state] of this.clients) {
      if (this.isDecayed(state, now)) this.clients.delete(key);
    }
    while (this.clients.size >= this.maxTrackedClients) {
      // Give up the lock that expires soonest: the least useful thing to keep,
      // and an attacker gains nothing by it (a fresh key was always free).
      let oldestKey: string | undefined;
      let oldestUntil = Infinity;
      for (const [key, state] of this.clients) {
        if (state.lockedUntilMs < oldestUntil) {
          oldestUntil = state.lockedUntilMs;
          oldestKey = key;
        }
      }
      if (oldestKey === undefined) return;
      this.clients.delete(oldestKey);
    }
  }

  /** Arms the next lockout window once the failure run crosses the threshold. */
  private applyLockout(state: ClientState): void {
    const over = state.consecutiveFailures - this.lockoutAfterFailures;
    if (over < 0) return;

    const window = Math.min(this.baseLockoutMs * 2 ** over, this.maxLockoutMs);
    const now = this.now();
    state.lockedUntilMs = now + window;
    if (state.lockedStreakStartedAtMs === 0) state.lockedStreakStartedAtMs = now;
  }

  /**
   * Forget accumulated state, for one client or all of them.
   *
   * Used as a test seam, and as the documented operational unlock: there is no
   * endpoint for it because authenticating an unlock request would need the very
   * password being throttled, so an operator unlocks by restarting the service.
   * Neither is a recovery path an attacker can take away any more: per-client
   * scoping, the decay window and the locked-streak bound are.
   */
  reset(clientKey?: string): void {
    if (clientKey === undefined) this.clients.clear();
    else this.clients.delete(clientKey);
  }
}

/** The process-wide throttle guarding POST /api/auth/login. */
export const loginThrottle = new LoginThrottle();
