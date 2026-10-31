import { describe, it, expect } from "vitest";
import { createTransferSchema, transferLinksSchema } from "@/lib/api/schemas";
import {
  candidateWindow,
  checkTransferEndpoints,
  checkTransferLink,
  findLinkClash,
  transferIsAccountedFor,
  transferMatchesSearch,
} from "@/lib/transfers";
import { searchTokens } from "@/lib/transaction-search";

// All figures and identifiers below are synthetic (see the repo's personal
// data policy): fake uuids, fake CLP amounts.
const CHECKING_ID = "11111111-1111-4111-8111-11111111111a";
const FUND_ID = "22222222-2222-4222-8222-22222222222b";
const USD_ID = "33333333-3333-4333-8333-33333333333c";
const TRANSFER_ID = "44444444-4444-4444-8444-44444444444d";
const OTHER_TRANSFER_ID = "55555555-5555-4555-8555-55555555555e";
const DEBIT_ID = "66666666-6666-4666-8666-66666666666f";
const CREDIT_ID = "77777777-7777-4777-8777-777777777770";

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

describe("createTransferSchema", () => {
  it("accepts a complete body and normalizes it", () => {
    const result = createTransferSchema.safeParse(
      body({
        description: "  Aporte al fondo ",
        amount: 999999.6,
        fromProductId: CHECKING_ID.toUpperCase(),
        notes: " ",
      })
    );

    expect(result).toEqual({
      success: true,
      data: {
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
    const result = createTransferSchema.safeParse(
      body({
        fromProductId: undefined,
        toProductId: undefined,
        fromAccountId: CHECKING_ID,
        toAccountId: FUND_ID,
      })
    );

    expect(result).toMatchObject({
      success: true,
      data: { fromProductId: CHECKING_ID, toProductId: FUND_ID },
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
    const result = createTransferSchema.safeParse(body(overrides));

    expect(result.success).toBe(false);
    expect(result.error?.issues[0].path).toEqual([field]);
  });

  it("rejects a body that is not an object", () => {
    expect(createTransferSchema.safeParse(null).success).toBe(false);
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

describe("transferLinksSchema", () => {
  it("lowercases ids and keeps null and omitted apart", () => {
    expect(
      transferLinksSchema.parse({ fromTransactionId: DEBIT_ID.toUpperCase(), toTransactionId: null })
    ).toEqual({ fromTransactionId: DEBIT_ID, toTransactionId: null });
    expect(transferLinksSchema.parse({ toTransactionId: CREDIT_ID })).toEqual({
      toTransactionId: CREDIT_ID,
    });
  });

  it("needs at least one side", () => {
    expect(transferLinksSchema.safeParse({}).success).toBe(false);
  });
});

describe("checkTransferLink", () => {
  const transfer = {
    id: TRANSFER_ID,
    amount: 1000000,
    fromProductId: CHECKING_ID,
    toProductId: FUND_ID,
    fromTransactionId: null,
    toTransactionId: null,
  };
  const debit = { id: DEBIT_ID, productId: CHECKING_ID, amount: -1000000 };
  const credit = { id: CREDIT_ID, productId: FUND_ID, amount: 1000000 };

  it("accepts the outgoing source row and the incoming target row", () => {
    expect(checkTransferLink(transfer, "from", debit).ok).toBe(true);
    expect(checkTransferLink(transfer, "to", credit).ok).toBe(true);
  });

  it.each([
    ["an unknown row", "from", undefined],
    ["a row on the other product", "from", { ...debit, productId: FUND_ID }],
    ["an incoming source row", "from", { ...debit, amount: 1000000 }],
    ["an outgoing target row", "to", { ...credit, amount: -1000000 }],
    ["a row off by a fee", "from", { ...debit, amount: -1000300 }],
    ["a smaller row", "to", { ...credit, amount: 999999 }],
  ] as const)("refuses %s", (_, side, txn) => {
    const result = checkTransferLink(transfer, side, txn);

    expect(result).toMatchObject({
      ok: false,
      field: side === "from" ? "fromTransactionId" : "toTransactionId",
    });
  });

  it("explains a mismatch in Spanish, for the Vincular dialog", () => {
    expect(
      checkTransferLink(transfer, "from", { ...debit, amount: -1000300 })
    ).toMatchObject({
      error: "El monto de la transacción no coincide exactamente con el del movimiento",
    });
  });

  it("refuses a side the transfer has no product for", () => {
    const legacy = { ...transfer, toProductId: null };

    expect(checkTransferLink(legacy, "to", credit)).toMatchObject({
      ok: false,
      field: "toTransactionId",
    });
  });
});

describe("findLinkClash", () => {
  it("names the side whose row another transfer holds", () => {
    const clash = findLinkClash(TRANSFER_ID, { from: DEBIT_ID, to: CREDIT_ID }, [
      { id: OTHER_TRANSFER_ID, fromTransactionId: null, toTransactionId: CREDIT_ID },
    ]);

    expect(clash).toMatchObject({ field: "toTransactionId" });
  });

  it("ignores the transfer's own links", () => {
    const clash = findLinkClash(TRANSFER_ID, { from: DEBIT_ID }, [
      { id: TRANSFER_ID, fromTransactionId: DEBIT_ID, toTransactionId: null },
    ]);

    expect(clash).toBeNull();
  });
});

describe("transferIsAccountedFor", () => {
  const sides = { fromProductId: CHECKING_ID, toProductId: FUND_ID };

  it("needs every side with a feed linked", () => {
    const feeds = new Set([CHECKING_ID, FUND_ID]);

    expect(
      transferIsAccountedFor(
        { ...sides, fromTransactionId: DEBIT_ID, toTransactionId: null },
        feeds
      )
    ).toBe(false);
    expect(
      transferIsAccountedFor(
        { ...sides, fromTransactionId: DEBIT_ID, toTransactionId: CREDIT_ID },
        feeds
      )
    ).toBe(true);
  });

  it("lets a feedless side stay unlinked", () => {
    expect(
      transferIsAccountedFor(
        { ...sides, fromTransactionId: DEBIT_ID, toTransactionId: null },
        new Set([CHECKING_ID])
      )
    ).toBe(true);
  });

  it("never resolves a transfer with nothing linked", () => {
    expect(
      transferIsAccountedFor(
        { ...sides, fromTransactionId: null, toTransactionId: null },
        new Set()
      )
    ).toBe(false);
  });
});

describe("candidateWindow", () => {
  it("spans five days around the transfer, across month ends", () => {
    expect(candidateWindow("2026-03-02")).toEqual({
      from: "2026-02-25",
      to: "2026-03-07",
    });
  });
});

describe("transferMatchesSearch", () => {
  const transfer = {
    description: "Aporte al fondo",
    fromTransaction: {
      description: "TRASPASO A FONDO 00-000-00000-01",
      displayDescription: "Ahorro de prueba",
    },
    toTransaction: null,
  };
  const matches = (q: string) => transferMatchesSearch(searchTokens(q), transfer);

  it("searches its description and a linked row's raw and display text", () => {
    expect(matches("aporte")).toBe(true);
    expect(matches("traspaso")).toBe(true);
    expect(matches("ahorro")).toBe(true);
    // Words may come from different fields.
    expect(matches("APORTE traspaso ahorro")).toBe(true);
  });

  it("drops a transfer missing a word everywhere", () => {
    expect(matches("aporte supermercado")).toBe(false);
  });

  it("keeps every transfer for a blank search", () => {
    expect(matches("  ")).toBe(true);
  });
});
