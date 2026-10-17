import { describe, it, expect } from "vitest";
import { buildWaterfallData, type MovementLike } from "../product-movements";

// Synthetic figures only (see the repo's personal data policy): USD amounts
// built from 1.234,56 and round fake values.

function txn(id: string, transactionDate: string, amount: number): MovementLike {
  return { id, description: `Movimiento ${id}`, amount, transactionDate };
}

describe("buildWaterfallData", () => {
  it("walks backwards from the current balance", () => {
    const { rows, truncated } = buildWaterfallData(
      [txn("a", "2026-01-05", -1234.56), txn("b", "2026-01-06", 1000)],
      5000
    );
    expect(truncated).toBe(false);
    expect(rows.map((r) => r.key)).toEqual(["a", "b"]);
    expect(rows[0].before).toBeCloseTo(5234.56, 2);
    expect(rows[0].after).toBe(4000);
    expect(rows[1].before).toBe(4000);
    expect(rows[1].after).toBe(5000);
  });

  it("stops at the first step outside the bounds and flags truncation", () => {
    // Cupo disponible 1.000 of a 2.000 limit: undoing the latest charge gives
    // 2.000 (fine), undoing the one before would exceed the limit.
    const { rows, truncated } = buildWaterfallData(
      [txn("old", "2026-01-01", -1234.56), txn("new", "2026-01-02", -1000)],
      1000,
      { min: 0, max: 2000 }
    );
    expect(truncated).toBe(true);
    expect(rows.map((r) => r.key)).toEqual(["new"]);
  });

  it("can keep fewer than two bars, so the card shows its note instead", () => {
    const { rows } = buildWaterfallData(
      [txn("a", "2026-01-01", -1234.56), txn("b", "2026-01-02", 1234.56)],
      1000,
      { min: 0, max: 1000 }
    );
    expect(rows.length).toBeLessThan(2);
  });
});
