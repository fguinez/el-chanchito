import { describe, expect, it } from "vitest";

import { LoginThrottle } from "@/lib/auth/throttle";

// The delay and the clock are injected, so nothing here sleeps for real or
// depends on wall-clock time.

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
}: {
  lockoutAfterFailures?: number;
  baseLockoutMs?: number;
  maxLockoutMs?: number;
  failureDelayMs?: number;
} = {}): Harness {
  let clock = 1_000_000;
  const delays: number[] = [];
  const throttle = new LoginThrottle({
    failureDelayMs,
    lockoutAfterFailures,
    baseLockoutMs,
    maxLockoutMs,
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
      throttle.run(slowAttempt(1)),
      throttle.run(slowAttempt(2)),
      throttle.run(slowAttempt(3)),
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

  it("delays every failure inside the mutex, so the delay is a global ceiling", async () => {
    const { throttle, delays } = harness({ failureDelayMs: 500 });
    await Promise.all([
      throttle.run(attempt(false)),
      throttle.run(attempt(false)),
    ]);
    expect(delays).toEqual([500, 500]);
  });

  it("does not delay a successful attempt", async () => {
    const { throttle, delays } = harness();
    const outcome = await throttle.run(attempt(true));
    expect(outcome).toEqual({ status: "ran", result: true });
    expect(delays).toEqual([]);
  });

  it("keeps serving attempts after one throws", async () => {
    const { throttle } = harness();
    await expect(
      throttle.run(async () => {
        throw new Error("boom");
      })
    ).rejects.toThrow("boom");

    const outcome = await throttle.run(attempt(true));
    expect(outcome).toEqual({ status: "ran", result: true });
  });
});

describe("LoginThrottle progressive lockout", () => {
  it("locks out only after the configured number of failures", async () => {
    const { throttle } = harness({ lockoutAfterFailures: 3, baseLockoutMs: 1_000 });

    for (let i = 0; i < 3; i += 1) {
      const outcome = await throttle.run(attempt(false));
      expect(outcome, `failure ${i + 1}`).toEqual({ status: "ran", result: false });
    }

    // The third failure armed the lockout: the fourth attempt never runs.
    const locked = await throttle.run(attempt(false));
    expect(locked).toEqual({ status: "locked", retryAfterSeconds: 1 });
  });

  it("never runs the attempt while locked out", async () => {
    const { throttle } = harness({ lockoutAfterFailures: 1 });
    await throttle.run(attempt(false));

    let ran = false;
    const outcome = await throttle.run(async () => {
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
      const outcome = await throttle.run(attempt(false));
      if (outcome.status === "locked") throw new Error("unexpected lockout");
      const locked = await throttle.run(attempt(false));
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
    await throttle.run(attempt(false));
    expect((await throttle.run(attempt(false))).status).toBe("locked");

    advance(1_000);
    expect(await throttle.run(attempt(false))).toEqual({
      status: "ran",
      result: false,
    });
  });

  it("clears the counter and the lockout on a successful login", async () => {
    const { throttle, advance } = harness({
      lockoutAfterFailures: 2,
      baseLockoutMs: 1_000,
    });

    await throttle.run(attempt(false));
    await throttle.run(attempt(false));
    expect((await throttle.run(attempt(false))).status).toBe("locked");

    advance(1_000);
    expect(await throttle.run(attempt(true))).toEqual({
      status: "ran",
      result: true,
    });

    // Back to a clean slate: the next two failures do not re-trip the lockout.
    for (let i = 0; i < 2; i += 1) {
      expect((await throttle.run(attempt(false))).status, `failure ${i + 1}`).toBe(
        "ran"
      );
    }
  });

  it("reports at least one second so Retry-After is never 0", async () => {
    const { throttle } = harness({ lockoutAfterFailures: 1, baseLockoutMs: 10 });
    await throttle.run(attempt(false));
    const locked = await throttle.run(attempt(false));
    expect(locked).toEqual({ status: "locked", retryAfterSeconds: 1 });
  });

  it("locks out a burst of concurrent guesses", async () => {
    const { throttle } = harness({ lockoutAfterFailures: 5, baseLockoutMs: 1_000 });

    const outcomes = await Promise.all(
      Array.from({ length: 20 }, () => throttle.run(attempt(false)))
    );

    const ran = outcomes.filter((o) => o.status === "ran").length;
    const locked = outcomes.filter((o) => o.status === "locked").length;

    // Only the first five guesses are ever checked; the rest are turned away.
    expect(ran).toBe(5);
    expect(locked).toBe(15);
  });
});
