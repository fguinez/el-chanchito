import { describe, expect, it } from "vitest";
import { billingDaysPatch, storedBillingDays } from "@/lib/billing-days";

// Synthetic card attributes only (see the repo's personal data policy).
describe("storedBillingDays", () => {
  it("reads a credit card's days, null when one is missing", () => {
    expect(
      storedBillingDays({ kind: "credit_card", last4: "1234", due_day: 5 })
    ).toEqual({ statementDay: null, dueDay: 5 });
  });

  it("reads no days from empty attributes", () => {
    expect(storedBillingDays({})).toEqual({ statementDay: null, dueDay: null });
  });
});

describe("billingDaysPatch", () => {
  const stored = { statementDay: 20, dueDay: 5 };

  it("sends nothing while both inputs hold the stored days", () => {
    expect(billingDaysPatch({ statementDay: "20", dueDay: " 5 " }, stored)).toEqual({
      ok: true,
      patch: {},
    });
  });

  it("sends only the day that changed", () => {
    expect(billingDaysPatch({ statementDay: "25", dueDay: "5" }, stored)).toEqual({
      ok: true,
      patch: { statementDay: 25 },
    });
  });

  it("sends an emptied input as null", () => {
    expect(billingDaysPatch({ statementDay: "", dueDay: "5" }, stored)).toEqual({
      ok: true,
      patch: { statementDay: null },
    });
  });

  it("sends nothing for an empty input with no stored day", () => {
    expect(
      billingDaysPatch(
        { statementDay: "", dueDay: "" },
        { statementDay: null, dueDay: null }
      )
    ).toEqual({ ok: true, patch: {} });
  });

  it.each(["0", "32", "-1", "10.5", "abc", "5,", "1e1", "0x1F", "+5"])(
    "rejects a changed statement day of %j",
    (input) => {
      expect(
        billingDaysPatch({ statementDay: input, dueDay: "5" }, stored)
      ).toEqual({ ok: false, field: "statementDay" });
    }
  );

  it("names the due day when it is the invalid one", () => {
    expect(billingDaysPatch({ statementDay: "20", dueDay: "40" }, stored)).toEqual({
      ok: false,
      field: "dueDay",
    });
  });

  it("lets an out-of-range stored day through while it is left untouched", () => {
    const odd = { statementDay: 45, dueDay: 5 };
    expect(billingDaysPatch({ statementDay: "45", dueDay: "6" }, odd)).toEqual({
      ok: true,
      patch: { dueDay: 6 },
    });
  });

  it("still checks an out-of-range stored day once it is edited", () => {
    const odd = { statementDay: 45, dueDay: 5 };
    expect(billingDaysPatch({ statementDay: "46", dueDay: "5" }, odd)).toEqual({
      ok: false,
      field: "statementDay",
    });
  });

  it("clears an out-of-range stored day when its input is emptied", () => {
    const odd = { statementDay: 45, dueDay: 5 };
    expect(billingDaysPatch({ statementDay: "", dueDay: "5" }, odd)).toEqual({
      ok: true,
      patch: { statementDay: null },
    });
  });
});
