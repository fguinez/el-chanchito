import { describe, it, expect } from "vitest";
import {
  hasRunDetails,
  runStatusStyle,
  truncateRunMessage,
} from "@/lib/scraper-runs";

describe("runStatusStyle", () => {
  it("gives partial runs their own label and color", () => {
    const partial = runStatusStyle("partial");

    expect(partial.label).toBe("parcial");
    expect(partial.badgeClass).not.toBe(runStatusStyle("success").badgeClass);
    expect(partial.badgeClass).not.toBe(runStatusStyle("error").badgeClass);
  });

  it("shows an unknown status as-is, unstyled", () => {
    expect(runStatusStyle("constructor")).toEqual({
      label: "constructor",
      badgeClass: "",
    });
  });
});

describe("hasRunDetails", () => {
  it.each([
    ["error", "products: login failed", true],
    ["partial", "transactions: 1 of 2 not written", true],
    ["partial", null, false],
    ["success", null, false],
    ["running", null, false],
  ])("%s with message %j -> %s", (status, error_message, expected) => {
    expect(hasRunDetails({ status, error_message })).toBe(expected);
  });
});

describe("truncateRunMessage", () => {
  it("keeps a short message whole and cuts a long one", () => {
    expect(truncateRunMessage("ok", 5)).toBe("ok");
    expect(truncateRunMessage("abcdefgh", 5)).toBe("abcde...");
  });
});
