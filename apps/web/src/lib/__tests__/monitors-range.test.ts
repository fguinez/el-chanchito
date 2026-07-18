import { describe, it, expect } from "vitest";
import { resolveHistoryRange } from "@/lib/monitors/serialize";

// Noon local avoids any midnight-boundary ambiguity in day math.
const NOW = new Date(2026, 6, 18, 12); // 2026-07-18

describe("resolveHistoryRange", () => {
  it("defaults to a trailing 90-day window ending today", () => {
    const result = resolveHistoryRange({}, NOW);

    expect(result).toEqual({ ok: true, from: "2026-04-20", to: "2026-07-18" });
  });

  it("resolves days=N as a trailing window ending today", () => {
    const result = resolveHistoryRange({ days: "30" }, NOW);

    expect(result).toEqual({ ok: true, from: "2026-06-19", to: "2026-07-18" });
  });

  it("clamps days to [1, 365]", () => {
    const tooSmall = resolveHistoryRange({ days: "0" }, NOW);
    const tooLarge = resolveHistoryRange({ days: "9999" }, NOW);

    expect(tooSmall).toEqual({
      ok: true,
      from: "2026-07-18",
      to: "2026-07-18",
    });
    expect(tooLarge).toEqual({
      ok: true,
      from: "2025-07-19",
      to: "2026-07-18",
    });
  });

  it("rejects a non-numeric days parameter", () => {
    const result = resolveHistoryRange({ days: "abc" }, NOW);

    expect(result).toMatchObject({ ok: false, field: "days" });
  });

  it("uses an explicit from/to pair over days", () => {
    const result = resolveHistoryRange(
      { days: "7", from: "2026-07-01", to: "2026-07-10" },
      NOW
    );

    expect(result).toEqual({ ok: true, from: "2026-07-01", to: "2026-07-10" });
  });

  it("defaults to to today when only from is given", () => {
    const result = resolveHistoryRange({ from: "2026-07-01" }, NOW);

    expect(result).toEqual({ ok: true, from: "2026-07-01", to: "2026-07-18" });
  });

  it("clamps a future to back to today", () => {
    const result = resolveHistoryRange(
      { from: "2026-07-01", to: "2027-01-01" },
      NOW
    );

    expect(result).toEqual({ ok: true, from: "2026-07-01", to: "2026-07-18" });
  });

  it("rejects to without from", () => {
    const result = resolveHistoryRange({ to: "2026-07-10" }, NOW);

    expect(result).toMatchObject({ ok: false, field: "from" });
  });

  it("rejects from after to", () => {
    const result = resolveHistoryRange(
      { from: "2026-07-10", to: "2026-07-01" },
      NOW
    );

    expect(result).toMatchObject({ ok: false, field: "from" });
  });

  it("rejects a fully future range", () => {
    const result = resolveHistoryRange(
      { from: "2026-08-01", to: "2026-08-10" },
      NOW
    );

    expect(result).toMatchObject({ ok: false, field: "from" });
  });

  it("rejects malformed and impossible calendar dates", () => {
    const malformed = resolveHistoryRange({ from: "01-07-2026" }, NOW);
    const impossible = resolveHistoryRange({ from: "2026-02-31" }, NOW);

    expect(malformed).toMatchObject({ ok: false, field: "from" });
    expect(impossible).toMatchObject({ ok: false, field: "from" });
  });

  it("rejects absolute spans over 365 days", () => {
    const result = resolveHistoryRange(
      { from: "2025-07-01", to: "2026-07-10" },
      NOW
    );

    expect(result).toMatchObject({ ok: false, field: "from" });
  });

  it("accepts a single-day range", () => {
    const result = resolveHistoryRange(
      { from: "2026-07-10", to: "2026-07-10" },
      NOW
    );

    expect(result).toEqual({ ok: true, from: "2026-07-10", to: "2026-07-10" });
  });
});
