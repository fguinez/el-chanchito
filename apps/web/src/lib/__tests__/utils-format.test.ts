import { describe, it, expect } from "vitest";
import {
  formatAxisValue,
  formatDateTimeEs,
  formatPriceAxisValue,
} from "@/lib/utils";

// vitest.config.ts pins TZ=America/Santiago (UTC-4 in July, UTC-3 in January).
describe("formatDateTimeEs", () => {
  it.each([
    ["a winter afternoon", "2026-07-09T18:00:00.000Z", "09-07-2026, 14:00"],
    ["a summer morning", "2026-01-15T12:30:00.000Z", "15-01-2026, 09:30"],
    ["midnight, 24h clock", "2026-07-09T04:00:00.000Z", "09-07-2026, 00:00"],
    ["the local day, not UTC", "2026-07-10T02:30:00.000Z", "09-07-2026, 22:30"],
  ])("formats %s", (_label, iso, expected) => {
    expect(formatDateTimeEs(iso)).toBe(expected);
  });
});

// Synthetic prices only.
describe("formatPriceAxisValue", () => {
  it.each([
    ["a price under a thousand", 250.5, "250.5"],
    ["four significant digits", 1234.56, "1235"],
    ["cents under a hundred", 99.99, "99.99"],
    ["float noise from a dragged domain", 100.0000001, "100"],
    ["a quote under a dollar", 0.123456, "0.1235"],
    ["a negative bound", -12.345, "-12.35"],
    ["thousands up to 100K", 99_990, "99990"],
    ["100K and up", 123_456, "123.5K"],
    ["millions", 2_500_000, "2.5M"],
    ["zero", 0, "0"],
    ["float dust", 1e-9, "0"],
  ])("formats %s", (_label, value, expected) => {
    expect(formatPriceAxisValue(value)).toBe(expected);
  });

  it("keeps ticks apart where formatAxisValue collapses them", () => {
    expect([1000, 1250, 1500].map(formatAxisValue)).toEqual(["1K", "1K", "2K"]);
    expect([1000, 1250, 1500].map(formatPriceAxisValue)).toEqual([
      "1000",
      "1250",
      "1500",
    ]);
  });
});
