import { describe, it, expect } from "vitest";
import { formatDateTimeEs } from "@/lib/utils";

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
