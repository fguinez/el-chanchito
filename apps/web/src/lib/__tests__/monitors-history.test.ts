import { describe, it, expect } from "vitest";
import {
  replayHistory,
  type MonitorDefinition,
  type ProductInfo,
  type SnapshotRow,
} from "@/lib/monitors";
import type { ClpRates } from "@/lib/rates";

// Synthetic uuids and figures only.
const CHECKING_ID = "11111111-1111-4111-8111-111111111111";
const CARD_ID = "22222222-2222-4222-8222-222222222222";

const rates: ClpRates = { CLP: 1, USD: 900 };

// Current product rows carry deliberately different values than any snapshot,
// so a leak of "today" into the past would show up in the assertions.
const products = new Map<string, ProductInfo>([
  [
    CHECKING_ID,
    {
      id: CHECKING_ID,
      kind: "checking",
      currency: "CLP",
      isActive: true,
      metrics: { kind: "checking", balance: 999999 },
      balanceAsOf: new Date("2026-07-10T12:00:00Z"),
      slug: "cuenta_corriente",
      institutionSlug: "banchile",
      name: "Cuenta corriente",
    },
  ],
  [
    CARD_ID,
    {
      id: CARD_ID,
      kind: "credit_card",
      currency: "CLP",
      isActive: true,
      metrics: { kind: "credit_card", available: 999999, owed: 999999 },
      balanceAsOf: new Date("2026-07-10T12:00:00Z"),
      slug: "tarjeta",
      institutionSlug: "banchile",
      name: "Tarjeta",
    },
  ],
]);

// The card only appears mid-range (2026-07-03), and 2026-07-02/04 have no
// snapshots at all, exercising both the null-before-first-observation rule
// and the carry-forward.
const snapshots: SnapshotRow[] = [
  {
    productId: CHECKING_ID,
    metrics: { kind: "checking", balance: 2000000 },
    asOf: new Date("2026-07-01T12:00:00Z"),
  },
  {
    productId: CHECKING_ID,
    metrics: { kind: "checking", balance: 1500000 },
    asOf: new Date("2026-07-03T12:00:00Z"),
  },
  {
    productId: CARD_ID,
    metrics: { kind: "credit_card", available: 500000, owed: 500000 },
    asOf: new Date("2026-07-03T15:00:00Z"),
  },
  {
    productId: CARD_ID,
    metrics: { kind: "credit_card", available: 300000, owed: 700000 },
    asOf: new Date("2026-07-05T12:00:00Z"),
  },
];

const def: MonitorDefinition = {
  currency: "CLP",
  expression: `@{${CHECKING_ID}:balance} - @{${CARD_ID}:owed}`,
  thresholds: [
    {
      severity: "alert",
      comparator: "<",
      expression: "1000000 - 30000 * (DAY_OF_MONTH() - 1)",
    },
  ],
};

describe("replayHistory", () => {
  it("replays one point per day with carry-forward and per-day thresholds", () => {
    const points = replayHistory(def, {
      snapshots,
      products,
      rates,
      from: "2026-07-01",
      to: "2026-07-05",
    });

    expect(points.map((p) => p.date)).toEqual([
      "2026-07-01",
      "2026-07-02",
      "2026-07-03",
      "2026-07-04",
      "2026-07-05",
    ]);

    // Before the card's first snapshot the value is null (no_data), but the
    // ramp threshold is still recomputed with that day's date.
    expect(points[0].value).toBeNull();
    expect(points[0].status).toBe("no_data");
    expect(points[0].thresholds[0].value).toBe(1000000);
    expect(points[1].value).toBeNull();
    expect(points[1].thresholds[0].value).toBe(970000);

    // 07-03: both products observed; 1500000 - 500000 = 1000000 vs 940000.
    expect(points[2].value).toBe(1000000);
    expect(points[2].thresholds[0].value).toBe(940000);
    expect(points[2].status).toBe("ok");
    expect(points[2].margin).toBe(60000);

    // 07-04 has no snapshots: values carry forward, threshold keeps ramping.
    expect(points[3].value).toBe(1000000);
    expect(points[3].thresholds[0].value).toBe(910000);
    expect(points[3].status).toBe("ok");
    expect(points[3].margin).toBe(90000);

    // 07-05: new card observation; 1500000 - 700000 = 800000 < 880000.
    expect(points[4].value).toBe(800000);
    expect(points[4].thresholds[0].value).toBe(880000);
    expect(points[4].status).toBe("breached");
    expect(points[4].margin).toBe(-80000);
  });

  it("defaults the range to the first..last snapshot dates", () => {
    const points = replayHistory(def, { snapshots, products, rates });
    expect(points).toHaveLength(5);
    expect(points[0].date).toBe("2026-07-01");
    expect(points[4].date).toBe("2026-07-05");
  });

  it("warms the carry map with snapshots before the window", () => {
    const points = replayHistory(def, {
      snapshots,
      products,
      rates,
      from: "2026-07-04",
      to: "2026-07-04",
    });
    expect(points).toHaveLength(1);
    expect(points[0].value).toBe(1000000);
    expect(points[0].status).toBe("ok");
  });

  it("treats legacy empty-object metrics as null (no_data, not a crash)", () => {
    const legacy: SnapshotRow[] = [
      {
        productId: CHECKING_ID,
        metrics: {},
        asOf: new Date("2026-07-01T12:00:00Z"),
      },
      {
        productId: CARD_ID,
        metrics: {},
        asOf: new Date("2026-07-01T13:00:00Z"),
      },
    ];
    const points = replayHistory(def, { snapshots: legacy, products, rates });
    // Both the checking balance and the card owed live in metrics, which the
    // legacy row lacks, so the day is no-data (not a crash).
    expect(points).toHaveLength(1);
    expect(points[0].value).toBeNull();
    expect(points[0].status).toBe("no_data");
  });

  it("returns an empty series when there is nothing to replay", () => {
    expect(replayHistory(def, { snapshots: [], products, rates })).toEqual([]);
  });

  it("steps day by day across a DST boundary without dropping a day", () => {
    // Chile's 2026 spring-forward skips local midnight of Sunday 2026-09-06
    // (clocks jump from Sat 24:00 to Sun 01:00). Day stepping is UTC-based
    // and evaluation dates are noon-local, so in America/Santiago the series
    // must still hit every calendar day with that day's own DAY_OF_MONTH.
    const septSnapshots: SnapshotRow[] = [
      {
        productId: CHECKING_ID,
        metrics: { kind: "checking", balance: 2000000 },
        asOf: new Date("2026-09-04T12:00:00Z"),
      },
      {
        productId: CARD_ID,
        metrics: { kind: "credit_card", available: 500000, owed: 500000 },
        asOf: new Date("2026-09-04T12:00:00Z"),
      },
    ];
    const points = replayHistory(def, {
      snapshots: septSnapshots,
      products,
      rates,
      from: "2026-09-05",
      to: "2026-09-07",
    });
    expect(points.map((p) => p.date)).toEqual([
      "2026-09-05",
      "2026-09-06",
      "2026-09-07",
    ]);
    // Ramp threshold per day: 1000000 - 30000 * (DAY_OF_MONTH() - 1).
    expect(points.map((p) => p.thresholds[0].value)).toEqual([
      880000, 850000, 820000,
    ]);
    // Carried value everywhere: 2000000 - 500000.
    expect(points.map((p) => p.value)).toEqual([1500000, 1500000, 1500000]);
  });
});
