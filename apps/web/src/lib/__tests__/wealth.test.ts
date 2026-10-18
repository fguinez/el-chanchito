import { describe, it, expect } from "vitest";
import {
  BACKFILL_SOURCE,
  buildWealthSeries,
  derivedSeriesStart,
  legacyDateError,
  legacyEntryCutoff,
  type LegacyWealthRow,
  type WealthSnapshotRow,
} from "@/lib/wealth";
import type { RateBook } from "@/lib/rates";

// Synthetic uuids and figures only. Timestamps use local-time constructors:
// the series groups snapshots by local day and the suite runs in
// America/Santiago (see vitest.config.ts).
const GOAL_ID = "11111111-1111-4111-8111-111111111111";
const CHECKING_ID = "22222222-2222-4222-8222-222222222222";
const CARD_ID = "33333333-3333-4333-8333-333333333333";
const STOCK_ID = "44444444-4444-4444-8444-444444444444";

const book: RateBook = { provider: { CLP: 1 }, overrides: [] };

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

/** A Fintual stock position, in USD. */
const stock = (asOf: Date, nav: number): WealthSnapshotRow => ({
  productId: STOCK_ID,
  balance: nav.toFixed(8),
  metrics: { kind: "stock", nav },
  asOf,
  source: "scraper",
  kind: "stock",
  currency: "USD",
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
      book
    );

    expect(series.map((p) => [p.snapshotDate, p.source])).toEqual([
      ["2026-01-10", "legacy"],
      ["2026-02-10", "legacy"],
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
        // V009 wrote them at UTC midnight of each legacy date.
        goal(new Date(Date.UTC(2026, 0, 10)), 1000000, BACKFILL_SOURCE),
        goal(new Date(Date.UTC(2026, 1, 10)), 1000000, BACKFILL_SOURCE),
      ],
      book
    );

    expect(series.map((p) => p.id)).toEqual([
      "legacy-2026-01-10",
      "legacy-2026-02-10",
    ]);
    expect(series.map((p) => p.patrimonio)).toEqual([2000000, 2500000]);
  });

  it("emits no point for a day with only backfill rows after the legacy history", () => {
    const series = buildWealthSeries(
      [legacyRow("2026-01-10", 2000000, 0, { fintualBalance: 1000000 })],
      [
        // Left behind when the 2026-02-10 legacy row was deleted.
        goal(new Date(Date.UTC(2026, 1, 10)), 1000000, BACKFILL_SOURCE),
        checking(new Date(2026, 2, 5, 10), 1500000),
      ],
      book
    );

    expect(series.map((p) => p.id)).toEqual([
      "legacy-2026-01-10",
      "computed-2026-03-05",
    ]);
    expect(series[1].patrimonio).toBe(2500000);
  });

  it("keeps an evening scrape on the boundary day hidden", () => {
    const series = buildWealthSeries(
      [legacyRow("2026-03-01", 2000000, 0)],
      [
        // 23:00 local is already 2026-03-02 in UTC.
        checking(new Date(2026, 2, 1, 23), 1000000),
        goal(new Date(2026, 2, 5, 10), 1500000),
      ],
      book
    );

    expect(series.map((p) => [p.snapshotDate, p.source])).toEqual([
      ["2026-03-01", "legacy"],
      ["2026-03-05", "computed"],
    ]);
    expect(series[1].patrimonio).toBe(2500000);
  });

  it("keeps Fintual's USD stock positions out of the CLP Fintual column", () => {
    const usdBook: RateBook = { provider: { CLP: 1, USD: 1000 }, overrides: [] };

    const series = buildWealthSeries(
      [],
      [
        goal(new Date(2026, 2, 5, 9), 1000000),
        stock(new Date(2026, 2, 5, 9), 1234.56),
      ],
      usdBook
    );

    expect(series[0]).toMatchObject({
      patrimonio: 2234560,
      fintualBalance: 1000000,
    });
  });

  it("derives deuda from a card's owed metric with no legacy rows", () => {
    const series = buildWealthSeries(
      [],
      [
        checking(new Date(2026, 2, 5, 10), 2500000),
        card(new Date(2026, 2, 5, 11), 1000000, 999999),
      ],
      book
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
      book
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

  it("counts months between local calendar dates", () => {
    // Parsed as UTC midnight, 2026-03-01 would read as February here.
    const series = buildWealthSeries(
      [legacyRow("2026-03-01", 2000000, 1000000)],
      [checking(new Date(2026, 3, 15, 10), 2500000)],
      book
    );

    expect(series[1]).toMatchObject({
      snapshotDate: "2026-04-15",
      monthsBetween: 1,
      monthlyRate: 1500000,
    });
  });
});

describe("derivedSeriesStart", () => {
  const scraped = (asOf: Date) => ({ asOf, source: "scraper" });

  it("ignores observations on the latest legacy date", () => {
    expect(
      derivedSeriesStart(
        [scraped(new Date(2026, 2, 1, 10)), scraped(new Date(2026, 2, 2, 9))],
        "2026-03-01"
      )
    ).toBe("2026-03-02");
  });

  it("ignores scraper rows that predate the latest legacy date", () => {
    // Pre-migration account_balances rows became 'scraper' snapshots.
    expect(
      derivedSeriesStart(
        [
          scraped(new Date(2026, 0, 20, 10)),
          scraped(new Date(2026, 3, 10, 10)),
        ],
        "2026-03-01"
      )
    ).toBe("2026-04-10");
  });

  it("ignores backfill rows after the latest legacy date", () => {
    // The 2026-03-01 legacy row was deleted; its backfill row remains, and
    // re-posting that date must stay possible.
    expect(
      derivedSeriesStart(
        [
          { asOf: new Date(Date.UTC(2026, 2, 1)), source: BACKFILL_SOURCE },
          scraped(new Date(2026, 3, 10, 10)),
        ],
        "2026-02-01"
      )
    ).toBe("2026-04-10");
  });

  it("keeps an evening scrape on its local day", () => {
    // Both are the next day in UTC.
    expect(
      derivedSeriesStart(
        [
          scraped(new Date(2026, 2, 1, 23)),
          { asOf: new Date(2026, 2, 5, 23), source: "manual" },
        ],
        "2026-03-01"
      )
    ).toBe("2026-03-05");
  });

  it("has no lower bound without legacy rows", () => {
    const rows = [scraped(new Date(2026, 0, 20, 10))];
    expect(derivedSeriesStart(rows, null)).toBe("2026-01-20");
    expect(derivedSeriesStart(rows, "")).toBe("2026-01-20");
  });

  it("is null when nothing real follows the legacy history", () => {
    expect(
      derivedSeriesStart(
        [
          { asOf: new Date(Date.UTC(2026, 0, 10)), source: BACKFILL_SOURCE },
          scraped(new Date(2026, 0, 20, 10)),
        ],
        "2026-03-01"
      )
    ).toBeNull();
  });
});

describe("legacyEntryCutoff", () => {
  const now = new Date(2026, 5, 15, 12);

  it("is the derived series' start when it predates today", () => {
    expect(legacyEntryCutoff("2026-03-05", now)).toBe("2026-03-05");
  });

  it("is today when nothing has been observed yet", () => {
    expect(legacyEntryCutoff(null, now)).toBe("2026-06-15");
  });
});

describe("legacyDateError", () => {
  const cutoff = "2026-03-05";

  it("accepts dates before the cutoff", () => {
    expect(legacyDateError("2026-03-04", cutoff)).toBeNull();
  });

  it.each(["2026-03-05", "2026-03-06"])(
    "rejects %s, on or after the cutoff",
    (snapshotDate) => {
      expect(legacyDateError(snapshotDate, cutoff)).toContain(
        "before 2026-03-05"
      );
    }
  );
});
