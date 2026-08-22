import { describe, expect, it } from "vitest";

import { LoginThrottle, loginClientKey } from "@/lib/auth/throttle";

// The delay and the clock are injected, so nothing here sleeps for real or
// depends on wall-clock time.

/** Two obviously synthetic client identities. */
const OWNER = "203.0.113.10";
const ATTACKER = "198.51.100.7";

interface Harness {
  throttle: LoginThrottle;
  /** Virtual clock, advanced explicitly by the tests. */
  advance: (ms: number) => void;
  /** Every delay the throttle asked for, in order. */
  delays: number[];
}

function harness({
  lockoutAfterFailures = 3,
  baseLockoutMs = 1_000,
  maxLockoutMs = 8_000,
  failureDelayMs = 500,
  failureDecayMs = 900_000,
  maxLockedStreakMs = 1_800_000,
  maxQueueDepth = 100,
  maxTrackedClients = 1_000,
}: {
  lockoutAfterFailures?: number;
  baseLockoutMs?: number;
  maxLockoutMs?: number;
  failureDelayMs?: number;
  failureDecayMs?: number;
  maxLockedStreakMs?: number;
  maxQueueDepth?: number;
  maxTrackedClients?: number;
} = {}): Harness {
  let clock = 1_000_000;
  const delays: number[] = [];
  const throttle = new LoginThrottle({
    failureDelayMs,
    lockoutAfterFailures,
    baseLockoutMs,
    maxLockoutMs,
    failureDecayMs,
    maxLockedStreakMs,
    maxQueueDepth,
    maxTrackedClients,
    now: () => clock,
    // Record the delay instead of serving it; the mutex ordering is what matters.
    sleep: async (ms) => {
      delays.push(ms);
    },
  });
  return {
    throttle,
    advance: (ms: number) => {
      clock += ms;
    },
    delays,
  };
}

/** One attempt with a fixed verdict. */
function attempt(ok: boolean) {
  return async () => ({ ok, result: ok });
}

describe("loginClientKey", () => {
  const headers = (values: Record<string, string>) => new Headers(values);

  it("takes the client-most forwarded entry when a proxy is trusted", () => {
    expect(
      loginClientKey(
        headers({ "x-forwarded-for": `${OWNER}, 10.0.0.1, 10.0.0.2` }),
        true
      )
    ).toBe(OWNER);
  });

  it("keys on the whole untrusted header when no proxy is declared", () => {
    // next start fills the header from the connection when the client sent none,
    // so this is the peer address unless the caller forged it, in which case the
    // forgery only buys the caller its own bucket.
    expect(loginClientKey(headers({ "x-forwarded-for": OWNER }), false)).toBe(OWNER);
    expect(
      loginClientKey(headers({ "x-forwarded-for": `${OWNER}, 10.0.0.1` }), false)
    ).not.toBe(OWNER);
  });

  it("falls back to a constant identity and bounds the key length", () => {
    expect(loginClientKey(headers({}), false)).toBe("unknown");
    expect(loginClientKey(headers({ "x-forwarded-for": "   " }), true)).toBe(
      "unknown"
    );
    const long = loginClientKey(
      headers({ "x-forwarded-for": "a".repeat(500) }),
      false
    );
    expect(long).toHaveLength(100);
  });
});

describe("LoginThrottle serialization", () => {
  it("runs concurrent attempts one at a time", async () => {
    const { throttle } = harness();
    const events: string[] = [];

    const slowAttempt = (id: number) => async () => {
      events.push(`start:${id}`);
      // Yield repeatedly: a non-serialized implementation would interleave here.
      for (let i = 0; i < 5; i += 1) await Promise.resolve();
      events.push(`end:${id}`);
      return { ok: false, result: false };
    };

    await Promise.all([
      throttle.run(OWNER, slowAttempt(1)),
      throttle.run(OWNER, slowAttempt(2)),
      throttle.run(OWNER, slowAttempt(3)),
    ]);

    expect(events).toEqual([
      "start:1",
      "end:1",
      "start:2",
      "end:2",
      "start:3",
      "end:3",
    ]);
  });

  it("serializes across clients too, so the ceiling is global", async () => {
    // The mutex is the one layer nobody can spoof their way past: rotating the
    // client identity must not buy any extra parallelism.
    const { throttle } = harness();
    let inFlight = 0;
    let maxInFlight = 0;

    const slowAttempt = async () => {
      inFlight += 1;
      maxInFlight = Math.max(maxInFlight, inFlight);
      for (let i = 0; i < 5; i += 1) await Promise.resolve();
      inFlight -= 1;
      return { ok: false, result: false };
    };

    await Promise.all(
      Array.from({ length: 10 }, (_, i) =>
        throttle.run(`10.0.0.${i}`, slowAttempt)
      )
    );

    expect(maxInFlight).toBe(1);
  });

  it("delays every failure inside the mutex, so the delay is a global ceiling", async () => {
    const { throttle, delays } = harness({ failureDelayMs: 500 });
    await Promise.all([
      throttle.run(OWNER, attempt(false)),
      throttle.run(ATTACKER, attempt(false)),
    ]);
    expect(delays).toEqual([500, 500]);
  });

  it("does not delay a successful attempt", async () => {
    const { throttle, delays } = harness();
    const outcome = await throttle.run(OWNER, attempt(true));
    expect(outcome).toEqual({ status: "ran", result: true });
    expect(delays).toEqual([]);
  });

  it("keeps serving attempts after one throws", async () => {
    const { throttle } = harness();
    await expect(
      throttle.run(OWNER, async () => {
        throw new Error("boom");
      })
    ).rejects.toThrow("boom");

    const outcome = await throttle.run(OWNER, attempt(true));
    expect(outcome).toEqual({ status: "ran", result: true });
  });
});

describe("LoginThrottle queue bound", () => {
  it("turns attempts away instead of queueing them without limit", async () => {
    const { throttle } = harness({ maxQueueDepth: 3 });
    let releaseFirst!: () => void;
    const gate = new Promise<void>((resolve) => {
      releaseFirst = resolve;
    });

    // One attempt holds the mutex; the queue can hold two more.
    const held = throttle.run(OWNER, async () => {
      await gate;
      return { ok: false, result: false };
    });
    const queued = [
      throttle.run(ATTACKER, attempt(false)),
      throttle.run(ATTACKER, attempt(false)),
    ];

    const rejected = await throttle.run(OWNER, attempt(true));
    expect(rejected).toEqual({
      status: "locked",
      retryAfterSeconds: 1,
      reason: "queue",
    });

    releaseFirst();
    await Promise.all([held, ...queued]);

    // The bound is transient: once the queue drains the owner gets in.
    expect(await throttle.run(OWNER, attempt(true))).toEqual({
      status: "ran",
      result: true,
    });
  });

  it("never runs the attempt for a queue rejection", async () => {
    const { throttle } = harness({ maxQueueDepth: 1 });
    let releaseFirst!: () => void;
    const gate = new Promise<void>((resolve) => {
      releaseFirst = resolve;
    });
    const held = throttle.run(OWNER, async () => {
      await gate;
      return { ok: false, result: false };
    });

    let ran = false;
    await throttle.run(ATTACKER, async () => {
      ran = true;
      return { ok: false, result: false };
    });
    expect(ran).toBe(false);

    releaseFirst();
    await held;
  });
});

describe("LoginThrottle progressive lockout", () => {
  it("locks out only after the configured number of failures", async () => {
    const { throttle } = harness({ lockoutAfterFailures: 3, baseLockoutMs: 1_000 });

    for (let i = 0; i < 3; i += 1) {
      const outcome = await throttle.run(OWNER, attempt(false));
      expect(outcome, `failure ${i + 1}`).toEqual({ status: "ran", result: false });
    }

    // The third failure armed the lockout: the fourth attempt never runs.
    const locked = await throttle.run(OWNER, attempt(false));
    expect(locked).toEqual({
      status: "locked",
      retryAfterSeconds: 1,
      reason: "lockout",
    });
  });

  it("never runs the attempt while locked out", async () => {
    const { throttle } = harness({ lockoutAfterFailures: 1 });
    await throttle.run(OWNER, attempt(false));

    let ran = false;
    const outcome = await throttle.run(OWNER, async () => {
      ran = true;
      // Even the correct password must not be checked while locked.
      return { ok: true, result: true };
    });

    expect(ran).toBe(false);
    expect(outcome.status).toBe("locked");
  });

  it("backs off exponentially and reports Retry-After in seconds", async () => {
    const { throttle, advance } = harness({
      lockoutAfterFailures: 1,
      baseLockoutMs: 1_000,
      maxLockoutMs: 8_000,
    });

    const windows: number[] = [];
    for (let i = 0; i < 5; i += 1) {
      const outcome = await throttle.run(OWNER, attempt(false));
      if (outcome.status === "locked") throw new Error("unexpected lockout");
      const locked = await throttle.run(OWNER, attempt(false));
      if (locked.status !== "locked") throw new Error("expected a lockout");
      windows.push(locked.retryAfterSeconds);
      // Wait the window out so the next failure can be recorded.
      advance(locked.retryAfterSeconds * 1000);
    }

    // 1s, 2s, 4s, then capped at 8s.
    expect(windows).toEqual([1, 2, 4, 8, 8]);
  });

  it("recovers once the window has passed", async () => {
    const { throttle, advance } = harness({
      lockoutAfterFailures: 1,
      baseLockoutMs: 1_000,
    });
    await throttle.run(OWNER, attempt(false));
    expect((await throttle.run(OWNER, attempt(false))).status).toBe("locked");

    advance(1_000);
    expect(await throttle.run(OWNER, attempt(false))).toEqual({
      status: "ran",
      result: false,
    });
  });

  it("clears the counter and the lockout on a successful login", async () => {
    const { throttle, advance } = harness({
      lockoutAfterFailures: 2,
      baseLockoutMs: 1_000,
    });

    await throttle.run(OWNER, attempt(false));
    await throttle.run(OWNER, attempt(false));
    expect((await throttle.run(OWNER, attempt(false))).status).toBe("locked");

    advance(1_000);
    expect(await throttle.run(OWNER, attempt(true))).toEqual({
      status: "ran",
      result: true,
    });

    // Back to a clean slate: the next two failures do not re-trip the lockout.
    for (let i = 0; i < 2; i += 1) {
      expect(
        (await throttle.run(OWNER, attempt(false))).status,
        `failure ${i + 1}`
      ).toBe("ran");
    }
  });

  it("reports at least one second so Retry-After is never 0", async () => {
    const { throttle } = harness({ lockoutAfterFailures: 1, baseLockoutMs: 10 });
    await throttle.run(OWNER, attempt(false));
    const locked = await throttle.run(OWNER, attempt(false));
    expect(locked).toEqual({
      status: "locked",
      retryAfterSeconds: 1,
      reason: "lockout",
    });
  });

  it("locks out a burst of concurrent guesses from one client", async () => {
    const { throttle } = harness({ lockoutAfterFailures: 5, baseLockoutMs: 1_000 });

    const outcomes = await Promise.all(
      Array.from({ length: 20 }, () => throttle.run(ATTACKER, attempt(false)))
    );

    const ran = outcomes.filter((o) => o.status === "ran").length;
    const locked = outcomes.filter((o) => o.status === "locked").length;

    // Only the first five guesses are ever checked; the rest are turned away.
    expect(ran).toBe(5);
    expect(locked).toBe(15);
  });

  it("does not extend its own window with an attempt made while locked", async () => {
    const { throttle, advance } = harness({
      lockoutAfterFailures: 1,
      baseLockoutMs: 4_000,
    });
    await throttle.run(ATTACKER, attempt(false));

    advance(1_000);
    const locked = await throttle.run(ATTACKER, attempt(false));
    if (locked.status !== "locked") throw new Error("expected a lockout");
    // 3s of the original 4s window left, not a fresh 4s.
    expect(locked.retryAfterSeconds).toBe(3);
  });
});

describe("LoginThrottle owner availability", () => {
  it("does not lock out one client because another was locked out", async () => {
    const { throttle } = harness({ lockoutAfterFailures: 2, baseLockoutMs: 60_000 });

    await throttle.run(ATTACKER, attempt(false));
    await throttle.run(ATTACKER, attempt(false));
    expect((await throttle.run(ATTACKER, attempt(false))).status).toBe("locked");

    // The owner has a clean slate and gets straight in.
    expect(await throttle.run(OWNER, attempt(true))).toEqual({
      status: "ran",
      result: true,
    });
  });

  it("keeps letting the owner in while an attacker guesses in a loop", async () => {
    // The exact review scenario: one wrong guess per window, forever. Before the
    // per-client scoping this left the owner permanently locked out.
    const { throttle, advance } = harness({
      lockoutAfterFailures: 5,
      baseLockoutMs: 5_000,
      maxLockoutMs: 300_000,
      failureDecayMs: 900_000,
    });

    // 24 hours of one guess every 5 minutes.
    for (let i = 0; i < 288; i += 1) {
      await throttle.run(ATTACKER, attempt(false));
      advance(300_000);
      // The owner's correct password is accepted at every single step.
      expect(await throttle.run(OWNER, attempt(true)), `hour ${i / 12}`).toEqual({
        status: "ran",
        result: true,
      });
    }
  });

  it("decays the failure counter after a quiet window", async () => {
    const { throttle, advance } = harness({
      lockoutAfterFailures: 2,
      baseLockoutMs: 1_000,
      failureDecayMs: 900_000,
    });

    await throttle.run(OWNER, attempt(false));
    await throttle.run(OWNER, attempt(false));
    expect((await throttle.run(OWNER, attempt(false))).status).toBe("locked");

    advance(900_000);

    // Counter forgotten: the backoff starts over instead of ratcheting up.
    for (let i = 0; i < 2; i += 1) {
      expect(
        (await throttle.run(OWNER, attempt(false))).status,
        `failure ${i + 1}`
      ).toBe("ran");
    }
    const locked = await throttle.run(OWNER, attempt(false));
    if (locked.status !== "locked") throw new Error("expected a lockout");
    expect(locked.retryAfterSeconds).toBe(1);
  });

  it("checks the password again once one client has been locked long enough", async () => {
    // Worst case: an attacker that knows the owner's identity and re-arms the
    // lock forever. The streak bound makes the lockout stop short circuiting
    // verification, so a correct password gets in and a wrong one still does not.
    const { throttle, advance } = harness({
      lockoutAfterFailures: 1,
      baseLockoutMs: 300_000,
      maxLockoutMs: 300_000,
      maxLockedStreakMs: 1_800_000,
    });

    await throttle.run(OWNER, attempt(false));
    expect((await throttle.run(OWNER, attempt(true))).status).toBe("locked");

    // Six 5-minute windows, each re-armed by one wrong guess: 30 minutes locked.
    for (let i = 0; i < 6; i += 1) {
      advance(300_000);
      await throttle.run(OWNER, attempt(false));
    }

    expect(await throttle.run(OWNER, attempt(true))).toEqual({
      status: "ran",
      result: true,
    });
  });

  it("still refuses a wrong password after the streak bound", async () => {
    const { throttle, advance } = harness({
      lockoutAfterFailures: 1,
      baseLockoutMs: 300_000,
      maxLockoutMs: 300_000,
      maxLockedStreakMs: 600_000,
    });

    await throttle.run(ATTACKER, attempt(false));
    advance(300_000);
    await throttle.run(ATTACKER, attempt(false));
    advance(300_000);
    await throttle.run(ATTACKER, attempt(false));

    const outcome = await throttle.run(ATTACKER, attempt(false));
    expect(outcome.status).toBe("locked");
  });
});

describe("LoginThrottle client map", () => {
  it("stays bounded while an attacker rotates identities", async () => {
    const { throttle } = harness({
      lockoutAfterFailures: 1,
      baseLockoutMs: 1_000,
      maxTrackedClients: 10,
    });

    for (let i = 0; i < 200; i += 1) {
      await throttle.run(`192.0.2.${i}`, attempt(false));
    }

    // No direct view of the map, so assert the observable consequence: the
    // owner's own slate is still clean and nothing blew up on the way.
    expect(await throttle.run(OWNER, attempt(true))).toEqual({
      status: "ran",
      result: true,
    });
  });

  it("forgets a client's state on reset", async () => {
    const { throttle } = harness({ lockoutAfterFailures: 1, baseLockoutMs: 60_000 });
    await throttle.run(ATTACKER, attempt(false));
    expect((await throttle.run(ATTACKER, attempt(false))).status).toBe("locked");

    throttle.reset(ATTACKER);
    expect((await throttle.run(ATTACKER, attempt(false))).status).toBe("ran");

    await throttle.run(ATTACKER, attempt(false));
    throttle.reset();
    expect((await throttle.run(ATTACKER, attempt(false))).status).toBe("ran");
  });
});
