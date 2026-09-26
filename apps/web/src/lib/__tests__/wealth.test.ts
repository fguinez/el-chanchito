import { describe, it, expect } from "vitest";
import {
  buildWealthSeries,
  type LegacyWealthRow,
  type WealthSnapshotRow,
} from "@/lib/wealth";
import type { ClpRates } from "@/lib/rates";

// Synthetic uuids and figures only. Timestamps use local-time constructors:
// the series groups snapshots by local day and the suite runs in
// America/Santiago (see vitest.config.ts).
const GOAL_ID = "11111111-1111-4111-8111-111111111111";
const CHECKING_ID = "22222222-2222-4222-8222-222222222222";
const CARD_ID = "33333333-3333-4333-8333-333333333333";

const rates: ClpRates = { CLP: 1 };

const legacyRow = (
  snapshotDate: string,
  patrimonio: number,
  deuda: number,
  extra: Partial<LegacyWealthRow> = {}
): LegacyWealthRow => ({
  id: `legacy-${snapshotDate}`,
  snapshotDate,
  patrimonio,
  deuda,
  fintualBalance: null,
  mercadopagoBalance: null,
  banchileSavings: null,
  notes: null,
  ...extra,
});

const goal = (
  asOf: Date,
  nav: number,
  source = "scraper"
): WealthSnapshotRow => ({
  productId: GOAL_ID,
  // numeric column, returned as a string by drizzle
  balance: nav.toFixed(8),
  metrics: { kind: "investment", nav },
  asOf,
  source,
  kind: "investment",
  currency: "CLP",
  slug: "fintual",
});

const checking = (asOf: Date, balance: number): WealthSnapshotRow => ({
  productId: CHECKING_ID,
  balance,
  metrics: { kind: "checking", balance },
  asOf,
  source: "scraper",
  kind: "checking",
  currency: "CLP",
  slug: "banchile",
});

const card = (
  asOf: Date,
  available: number,
  owed: number
): WealthSnapshotRow => ({
  productId: CARD_ID,
  balance: available,
  metrics: { kind: "credit_card", available, limit: 2000000, owed },
  asOf,
  source: "scraper",
  kind: "credit_card",
  currency: "CLP",
  slug: "banchile",
});

describe("buildWealthSeries", () => {
  it("keeps legacy totals up to the last legacy date, then computes", () => {
    const series = buildWealthSeries(
      [
        legacyRow("2026-01-10", 2000000, 1000000),
        legacyRow("2026-02-10", 2500000, 999999),
      ],
      [
        // Observed on the boundary day: hidden there, carried forward after.
        goal(new Date(2026, 1, 10, 9), 1000000),
        checking(new Date(2026, 2, 5, 10), 1500000),
      ],
      rates
    );

    expect(series.map((p) => [p.snapshotDate, p.source])).toEqual([
      ["2026-01-10", "manual"],
      ["2026-02-10", "manual"],
      ["2026-03-05", "computed"],
    ]);
    expect(series[1]).toMatchObject({ patrimonio: 2500000, deuda: 999999 });
    expect(series[2]).toMatchObject({
      id: "computed-2026-03-05",
      patrimonio: 2500000,
      deuda: 0,
      fintualBalance: 1000000,
      mercadopagoBalance: null,
      banchileSavings: null,
    });
  });

  it("ignores backfill rows dated on legacy days", () => {
    const series = buildWealthSeries(
      [
        legacyRow("2026-01-10", 2000000, 0, { fintualBalance: 1000000 }),
        legacyRow("2026-02-10", 2500000, 0, { fintualBalance: 1000000 }),
      ],
      [
        goal(new Date(2026, 0, 10, 0), 1000000, "wealth_snapshot"),
        goal(new Date(2026, 1, 10, 0), 1000000, "wealth_snapshot"),
      ],
      rates
    );

    expect(series.map((p) => p.id)).toEqual([
      "legacy-2026-01-10",
      "legacy-2026-02-10",
    ]);
    expect(series.map((p) => p.patrimonio)).toEqual([2000000, 2500000]);
  });

  it("derives deuda from a card's owed metric with no legacy rows", () => {
    const series = buildWealthSeries(
      [],
      [
        checking(new Date(2026, 2, 5, 10), 2500000),
        card(new Date(2026, 2, 5, 11), 1000000, 999999),
      ],
      rates
    );

    expect(series).toHaveLength(1);
    // The card's available cupo is not an asset; its reported owed is debt.
    expect(series[0]).toMatchObject({
      source: "computed",
      patrimonio: 2500000,
      deuda: 999999,
      ahorro: 1500001,
    });
  });

  it("enriches each point against the previous one", () => {
    const series = buildWealthSeries(
      [legacyRow("2026-01-10", 2000000, 1000000)],
      [
        checking(new Date(2026, 2, 10, 10), 2500000),
        card(new Date(2026, 2, 10, 11), 1000000, 1000000),
      ],
      rates
    );

    expect(series[0]).toMatchObject({
      ahorro: 1000000,
      periodSavings: null,
      monthsBetween: null,
      monthlyRate: null,
    });
    expect(series[1]).toMatchObject({
      source: "computed",
      ahorro: 1500000,
      periodSavings: 500000,
      monthsBetween: 2,
      monthlyRate: 250000,
    });
  });
});
