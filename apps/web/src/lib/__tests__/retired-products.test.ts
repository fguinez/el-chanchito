import { describe, it, expect } from "vitest";
import { isRetiredGhost } from "@/lib/retired-products";

describe("isRetiredGhost", () => {
  it("hides a retired roll-up: inactive with its balance stripped", () => {
    expect(isRetiredGhost({ isActive: false, currentBalance: null })).toBe(true);
  });

  it("keeps an inactive product that still has a balance", () => {
    // The UI shows these with an "Inactivo" badge; they are not ghosts.
    expect(isRetiredGhost({ isActive: false, currentBalance: 1000000 })).toBe(
      false
    );
  });

  it("keeps an inactive product with a zero balance", () => {
    // 0 is data, not the absence of data.
    expect(isRetiredGhost({ isActive: false, currentBalance: 0 })).toBe(false);
  });

  it("keeps an active product that has not been scraped yet", () => {
    expect(isRetiredGhost({ isActive: true, currentBalance: null })).toBe(false);
  });

  it("keeps an ordinary active product with a balance", () => {
    expect(isRetiredGhost({ isActive: true, currentBalance: 2500000 })).toBe(
      false
    );
  });

  it("handles the string balances postgres-js returns for numerics", () => {
    expect(isRetiredGhost({ isActive: false, currentBalance: "999999" })).toBe(
      false
    );
    expect(isRetiredGhost({ isActive: false, currentBalance: "0" })).toBe(false);
  });
});
