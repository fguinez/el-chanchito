import { describe, it, expect } from "vitest";
import { checkTransferEndpoints, parseNewTransfer } from "@/lib/transfers";

// All figures and identifiers below are synthetic (see the repo's personal
// data policy): fake uuids, fake CLP amounts.
const CHECKING_ID = "11111111-1111-4111-8111-11111111111a";
const FUND_ID = "22222222-2222-4222-8222-22222222222b";
const USD_ID = "33333333-3333-4333-8333-33333333333c";

/** A valid create body; tests override single fields. */
function body(overrides: Record<string, unknown> = {}) {
  return {
    description: "Aporte al fondo",
    amount: 1000000,
    fromProductId: CHECKING_ID,
    toProductId: FUND_ID,
    transferDate: "2026-09-15",
    ...overrides,
  };
}

describe("parseNewTransfer", () => {
  it("accepts a complete body and normalizes it", () => {
    const result = parseNewTransfer(
      body({
        description: "  Aporte al fondo ",
        amount: 999999.6,
        fromProductId: CHECKING_ID.toUpperCase(),
        notes: " ",
      })
    );

    expect(result).toEqual({
      ok: true,
      value: {
        description: "Aporte al fondo",
        amount: 1000000,
        fromProductId: CHECKING_ID,
        toProductId: FUND_ID,
        transferDate: "2026-09-15",
        notes: null,
      },
    });
  });

  it("accepts the legacy fromAccountId/toAccountId aliases", () => {
    const result = parseNewTransfer(
      body({
        fromProductId: undefined,
        toProductId: undefined,
        fromAccountId: CHECKING_ID,
        toAccountId: FUND_ID,
      })
    );

    expect(result).toMatchObject({
      ok: true,
      value: { fromProductId: CHECKING_ID, toProductId: FUND_ID },
    });
  });

  it.each([
    ["a missing source product", { fromProductId: undefined }, "fromProductId"],
    ["a missing target product", { toProductId: null }, "toProductId"],
    ["a non-uuid product id", { toProductId: "banchile" }, "toProductId"],
    ["the same product on both ends", { toProductId: CHECKING_ID }, "toProductId"],
    [
      "the same product in another letter case",
      { toProductId: CHECKING_ID.toUpperCase() },
      "toProductId",
    ],
    ["a blank description", { description: "   " }, "description"],
    ["a zero amount", { amount: 0 }, "amount"],
    ["an amount that rounds to zero", { amount: 0.4 }, "amount"],
    ["a negative amount", { amount: -2500000 }, "amount"],
    ["an amount past the integer column", { amount: 2_147_483_648 }, "amount"],
    ["a string amount", { amount: "1000000" }, "amount"],
    ["an impossible date", { transferDate: "2026-02-30" }, "transferDate"],
    ["a non-ISO date", { transferDate: "15-09-2026" }, "transferDate"],
    ["non-string notes", { notes: 42 }, "notes"],
  ])("rejects %s", (_, overrides, field) => {
    const result = parseNewTransfer(body(overrides));

    expect(result).toMatchObject({ ok: false, status: 400, field });
  });

  it("rejects a body that is not an object", () => {
    expect(parseNewTransfer(null)).toMatchObject({ ok: false, status: 400 });
  });
});

describe("checkTransferEndpoints", () => {
  const input = { fromProductId: CHECKING_ID, toProductId: FUND_ID };

  it("passes when both products exist and hold CLP", () => {
    const result = checkTransferEndpoints(input, [
      { id: CHECKING_ID, currency: "CLP" },
      { id: FUND_ID, currency: "CLP" },
    ]);

    expect(result.ok).toBe(true);
  });

  it("names the endpoint that does not exist", () => {
    const result = checkTransferEndpoints(input, [
      { id: CHECKING_ID, currency: "CLP" },
    ]);

    expect(result).toMatchObject({ ok: false, field: "toProductId" });
  });

  it("rejects a product in another currency", () => {
    const result = checkTransferEndpoints(
      { fromProductId: USD_ID, toProductId: FUND_ID },
      [
        { id: USD_ID, currency: "USD" },
        { id: FUND_ID, currency: "CLP" },
      ]
    );

    expect(result).toMatchObject({ ok: false, field: "fromProductId" });
  });
});
