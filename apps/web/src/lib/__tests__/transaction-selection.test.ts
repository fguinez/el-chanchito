import { describe, expect, it } from "vitest";
import {
  hrefWithSearchParam,
  transactionDisplayId,
  TXN_PARAM,
} from "@/lib/transaction-selection";

// Synthetic ids only.
const TXN_ID = "00000000-0000-4000-8000-000000000001";

describe("hrefWithSearchParam", () => {
  it("adds the param to a bare path", () => {
    expect(hrefWithSearchParam("/expenses", "", TXN_PARAM, TXN_ID)).toBe(
      `/expenses?txn=${TXN_ID}`
    );
  });

  it("replaces an existing value and keeps other params", () => {
    expect(
      hrefWithSearchParam("/expenses", "range=30d&txn=old", TXN_PARAM, TXN_ID)
    ).toBe(`/expenses?range=30d&txn=${TXN_ID}`);
  });

  it("removes the param on null, keeping the rest", () => {
    expect(
      hrefWithSearchParam("/expenses", `range=30d&txn=${TXN_ID}`, TXN_PARAM, null)
    ).toBe("/expenses?range=30d");
  });

  it("drops the question mark when nothing is left", () => {
    expect(
      hrefWithSearchParam("/expenses", `txn=${TXN_ID}`, TXN_PARAM, null)
    ).toBe("/expenses");
  });
});

describe("transactionDisplayId", () => {
  it("prefers the institution's operation id", () => {
    expect(
      transactionDisplayId({ id: TXN_ID, externalId: "SYNTH-0001" })
    ).toEqual({ value: "SYNTH-0001", external: true });
  });

  it("falls back to the internal id when there is no external one", () => {
    expect(transactionDisplayId({ id: TXN_ID, externalId: null })).toEqual({
      value: TXN_ID,
      external: false,
    });
  });

  it("treats an empty external id as missing", () => {
    expect(transactionDisplayId({ id: TXN_ID, externalId: "" })).toEqual({
      value: TXN_ID,
      external: false,
    });
  });
});
