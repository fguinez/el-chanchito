import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { wealthSnapshots } from "@/lib/db/schema";

// POST /api/wealth wiring: the cutoff comes from the latest legacy date and
// the product observations after it, the body goes through
// validateLegacySnapshot, and a duplicate date is a conflict rather than an
// overwrite. The db is a stub that answers the queries POST issues:
// `select ... from` (max(snapshot_date), or every snapshot's asOf/source)
// and `insert ... onConflictDoNothing ... returning`.

type SnapshotRow = { asOf: Date; source: string };

const lastLegacyDate = vi.fn<() => string | null>();
const snapshotRows = vi.fn<() => SnapshotRow[]>();
const insertValues = vi.fn();
const insertReturning = vi.fn<() => unknown[]>();

vi.mock("@/lib/db", () => ({
  db: {
    select: () => ({
      from: async (table: unknown) =>
        table === wealthSnapshots
          ? [{ lastLegacyDate: lastLegacyDate() }]
          : snapshotRows(),
    }),
    insert: () => ({
      values: (values: unknown) => {
        insertValues(values);
        return {
          onConflictDoNothing: () => ({
            returning: async () => insertReturning(),
          }),
        };
      },
    }),
  },
}));

const { POST } = await import("@/app/api/wealth/route");

function postRequest(body: string) {
  return new NextRequest(new URL("http://localhost:3000/api/wealth"), {
    method: "POST",
    headers: { "content-type": "application/json" },
    body,
  });
}

const post = (body: unknown) => POST(postRequest(JSON.stringify(body)));

beforeEach(() => {
  lastLegacyDate.mockReset();
  snapshotRows.mockReset();
  insertValues.mockReset();
  insertReturning.mockReset();
  // Legacy history ends on 2026-03-01; real scraping after it starts on
  // 2026-04-10. The earlier rows must not move the cutoff: a pre-migration
  // scraper row and a backfill row left behind after the legacy date.
  lastLegacyDate.mockReturnValue("2026-03-01");
  snapshotRows.mockReturnValue([
    { asOf: new Date(2026, 0, 20, 10), source: "scraper" },
    { asOf: new Date(Date.UTC(2026, 3, 1)), source: "wealth_snapshot" },
    { asOf: new Date(2026, 3, 10, 10), source: "scraper" },
  ]);
  insertReturning.mockReturnValue([{ id: "legacy-row-id" }]);
  // The cutoff is also capped at today, so pin the clock.
  vi.useFakeTimers();
  vi.setSystemTime(new Date(2026, 5, 15, 12));
});

afterEach(() => {
  vi.useRealTimers();
});

describe("POST /api/wealth", () => {
  it("answers 400 for invalid JSON or a malformed body", async () => {
    const invalid = await POST(postRequest("{"));
    expect(invalid.status).toBe(400);

    const malformed = await post({ snapshotDate: "2026-02-30", patrimonio: 1 });
    expect(malformed.status).toBe(400);
    expect(insertValues).not.toHaveBeenCalled();
  });

  it("accepts the day before the derived series starts", async () => {
    const response = await post({
      snapshotDate: "2026-04-09",
      patrimonio: 2500000,
    });
    expect(response.status).toBe(201);
  });

  it("answers 409 from the derived series' first day on", async () => {
    const response = await post({
      snapshotDate: "2026-04-10",
      patrimonio: 2500000,
    });

    expect(response.status).toBe(409);
    await expect(response.json()).resolves.toMatchObject({
      error: expect.stringContaining("before 2026-04-10"),
    });
    expect(insertValues).not.toHaveBeenCalled();
  });

  it("accepts dates inside legacy history", async () => {
    // After the pre-migration scraper row, which must not bound it.
    const response = await post({
      snapshotDate: "2026-02-15",
      patrimonio: 2500000,
    });
    expect(response.status).toBe(201);
  });

  it("answers 409 when the date already has a legacy snapshot", async () => {
    insertReturning.mockReturnValue([]);

    const response = await post({
      snapshotDate: "2026-01-10",
      patrimonio: 2500000,
    });

    expect(response.status).toBe(409);
    await expect(response.json()).resolves.toEqual({
      error: "A legacy snapshot already exists for 2026-01-10",
    });
  });

  it("inserts the rounded values and answers 201 with the row", async () => {
    const row = { id: "legacy-row-id", snapshotDate: "2026-01-10" };
    insertReturning.mockReturnValue([row]);

    const response = await post({
      snapshotDate: "2026-01-10",
      patrimonio: 2500000.4,
      deuda: 999999.2,
      fintualBalance: 1000000,
    });

    expect(response.status).toBe(201);
    await expect(response.json()).resolves.toEqual(row);
    expect(insertValues).toHaveBeenCalledWith({
      snapshotDate: "2026-01-10",
      patrimonio: 2500000,
      deuda: 999999,
      fintualBalance: 1000000,
      mercadopagoBalance: null,
      banchileSavings: null,
      notes: null,
    });
  });

  describe("with no product observations yet", () => {
    beforeEach(() => {
      lastLegacyDate.mockReturnValue(null);
      snapshotRows.mockReturnValue([]);
    });

    it("answers 409 for today", async () => {
      const response = await post({
        snapshotDate: "2026-06-15",
        patrimonio: 2500000,
      });
      expect(response.status).toBe(409);
    });

    it("accepts yesterday", async () => {
      const response = await post({
        snapshotDate: "2026-06-14",
        patrimonio: 2500000,
      });
      expect(response.status).toBe(201);
    });
  });
});
