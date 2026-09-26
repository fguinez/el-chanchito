import { describe, expect, it } from "vitest";
import { parseNumberInput } from "@/lib/utils";

describe("parseNumberInput", () => {
  it.each([
    ["0", 0],
    ["999999", 999_999],
    ["-2500", -2500],
    ["12.5", 12.5],
    ["1e3", 1000],
    [" 42 ", 42],
  ])("parses %j as %d", (input, expected) => {
    expect(parseNumberInput(input)).toBe(expected);
  });

  it.each(["", "   ", "abc", "1,5", "Infinity"])(
    "returns null for %j",
    (input) => {
      expect(parseNumberInput(input)).toBeNull();
    }
  );
});
