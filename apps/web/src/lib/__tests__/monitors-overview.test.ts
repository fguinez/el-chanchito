import { describe, it, expect } from "vitest";
import {
  STALE_AFTER_HOURS,
  groupForInicio,
  isStale,
  type MonitorStatus,
} from "@/lib/monitors";

// Synthetic timestamps only; `now` is fixed, never the wall clock.
const NOW = new Date("2026-07-15T12:00:00Z");
const FRESH = "2026-07-15T08:00:00.000Z"; // 4 hours before NOW
const STALE = "2026-07-12T12:00:00.000Z"; // 72 hours before NOW

function monitor(
  id: string,
  status: MonitorStatus,
  {
    staleAsOf = FRESH,
    isActive = true,
  }: { staleAsOf?: string | null; isActive?: boolean } = {}
) {
  return { id, isActive, evaluation: { status, staleAsOf } };
}

function ids(list: { id: string }[]): string[] {
  return list.map((m) => m.id);
}

describe("isStale", () => {
  it("treats the threshold as two days", () => {
    expect(STALE_AFTER_HOURS).toBe(48);
  });

  it("is false when there is no observation", () => {
    expect(isStale(null, NOW)).toBe(false);
  });

  it.each([
    ["4 hours old", FRESH, false],
    ["one minute under 48 hours", "2026-07-13T12:01:00.000Z", false],
    ["exactly 48 hours old", "2026-07-13T12:00:00.000Z", false],
    ["one minute over 48 hours", "2026-07-13T11:59:00.000Z", true],
    ["72 hours old", STALE, true],
    ["in the future (clock skew)", "2026-07-15T13:00:00.000Z", false],
  ])("%s (%s) -> %s", (_label, staleAsOf, expected) => {
    expect(isStale(staleAsOf, NOW)).toBe(expected);
  });
});

describe("groupForInicio", () => {
  it("puts breached and warning monitors in attention, in input order", () => {
    const { attention, noData, staleOk } = groupForInicio(
      [
        monitor("w1", "warning"),
        monitor("ok", "ok"),
        monitor("b1", "breached"),
        monitor("w2", "warning"),
      ],
      NOW
    );
    expect(ids(attention)).toEqual(["w1", "b1", "w2"]);
    expect(noData).toEqual([]);
    expect(staleOk).toEqual([]);
  });

  it("puts no_data monitors in noData", () => {
    const { attention, noData } = groupForInicio(
      [
        monitor("n1", "no_data"),
        monitor("b1", "breached"),
        monitor("n2", "no_data"),
      ],
      NOW
    );
    expect(ids(noData)).toEqual(["n1", "n2"]);
    expect(ids(attention)).toEqual(["b1"]);
  });

  it("lists only the stale ok monitors in staleOk", () => {
    const groups = groupForInicio(
      [
        monitor("stale", "ok", { staleAsOf: STALE }),
        monitor("fresh", "ok"),
        monitor("unobserved", "ok", { staleAsOf: null }),
      ],
      NOW
    );
    expect(groups).toEqual({
      attention: [],
      noData: [],
      staleOk: [monitor("stale", "ok", { staleAsOf: STALE })],
    });
  });

  it("counts stale data only for ok monitors", () => {
    const { attention, noData, staleOk } = groupForInicio(
      [
        monitor("b1", "breached", { staleAsOf: STALE }),
        monitor("n1", "no_data", { staleAsOf: STALE }),
      ],
      NOW
    );
    expect(ids(attention)).toEqual(["b1"]);
    expect(ids(noData)).toEqual(["n1"]);
    expect(staleOk).toEqual([]);
  });

  it.each<MonitorStatus>(["breached", "warning", "no_data", "ok"])(
    "excludes an inactive %s monitor from every group",
    (status) => {
      expect(
        groupForInicio(
          [monitor("inactive", status, { staleAsOf: STALE, isActive: false })],
          NOW
        )
      ).toEqual({ attention: [], noData: [], staleOk: [] });
    }
  );
});
