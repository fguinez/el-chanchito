import { describe, it, expect } from "vitest";
import {
  LEGACY_COMPOSITE_INSTITUTION,
  scraperRunLabel,
} from "@/lib/scraper-run-label";

describe("scraperRunLabel", () => {
  it.each([
    ["banchile", "Banco de Chile", "Banco de Chile"],
    ["new_fintech", null, "new_fintech"],
    [LEGACY_COMPOSITE_INSTITUTION, null, "Email (legacy)"],
    ["banchile", "", "banchile"],
  ])("%s with name %j -> %s", (institution, institution_name, expected) => {
    expect(scraperRunLabel({ institution, institution_name })).toBe(expected);
  });
});
