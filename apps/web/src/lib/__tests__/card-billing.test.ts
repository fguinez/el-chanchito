import { describe, expect, it } from "vitest";
import {
  billedDebtState,
  dayInMonth,
  daysBetween,
  effectiveBillingStatus,
  isMinimumPaid,
  isPaymentOverdue,
  olderStatementDate,
  resolveBillingCycle,
  statementFigures,
  summarizeBilling,
  type BillingCycle,
  type BillingTransactionInput,
} from "@/lib/card-billing";
import type { CreditCardMetrics } from "@chanchito/product-model";

// Synthetic fixtures only: invented dates, card days and amounts from the
// repo's fake set ($ 1.000.000, $ 999.999, $ 2.500.000, USD 1.234,56).

function cardMetrics(fields: Partial<CreditCardMetrics> = {}): CreditCardMetrics {
  return { kind: "credit_card", available: 2500000, ...fields };
}

function cardAttributes(statementDay?: number | null, dueDay?: number | null) {
  return {
    kind: "credit_card" as const,
    last4: "1234",
    statement_day: statementDay,
    due_day: dueDay,
  };
}

function txn(
  transactionDate: string,
  amount: number,
  billingStatus: BillingTransactionInput["billingStatus"] = null,
  statementDate: string | null = null
): BillingTransactionInput {
  return { transactionDate, amount, billingStatus, statementDate };
}

/** A cycle closing on the 15th, as of late October 2026. */
const OCTOBER_CYCLE: BillingCycle = {
  lastClosing: { date: "2026-10-15", estimated: false },
  previousClosing: "2026-09-15",
  nextClosing: { date: "2026-11-15", estimated: false },
  dueDate: { date: "2026-11-05", estimated: false },
  basis: "statement",
  rolledOver: false,
};

describe("dayInMonth", () => {
  it("clamps a day past the month's end to its last day", () => {
    expect(dayInMonth(2026, 2, 31)).toBe("2026-02-28");
    expect(dayInMonth(2028, 2, 31)).toBe("2028-02-29");
    expect(dayInMonth(2026, 4, 31)).toBe("2026-04-30");
    expect(dayInMonth(2026, 1, 31)).toBe("2026-01-31");
  });

  it("carries a month offset across years", () => {
    expect(dayInMonth(2026, 13, 5)).toBe("2027-01-05");
    expect(dayInMonth(2026, 0, 15)).toBe("2025-12-15");
    expect(dayInMonth(2026, -1, 31)).toBe("2025-11-30");
  });
});

describe("daysBetween", () => {
  it("counts whole calendar days, across month ends and DST changes", () => {
    expect(daysBetween("2026-10-31", "2026-11-05")).toBe(5);
    expect(daysBetween("2026-09-01", "2026-09-10")).toBe(9);
    expect(daysBetween("2026-04-01", "2026-04-10")).toBe(9);
    expect(daysBetween("2026-10-31", "2026-10-31")).toBe(0);
    expect(daysBetween("2026-10-31", "2026-10-30")).toBe(-1);
  });
});

describe("statementFigures", () => {
  it("reads a credit card's latest statement", () => {
    expect(
      statementFigures(
        cardMetrics({
          statement_date: "2026-10-15",
          billed_amount: 1000000,
          minimum_payment: 999999,
          payment_due_date: "2026-11-05",
        })
      )
    ).toEqual({
      date: "2026-10-15",
      billedAmount: 1000000,
      minimumPayment: 999999,
      paymentDueDate: "2026-11-05",
    });
  });

  it("is empty for another kind's metrics or none at all", () => {
    const empty = {
      date: null,
      billedAmount: null,
      minimumPayment: null,
      paymentDueDate: null,
    };
    expect(
      statementFigures({ kind: "line_of_credit", available: 2500000 })
    ).toEqual(empty);
    expect(statementFigures(null)).toEqual(empty);
    expect(statementFigures({})).toEqual(empty);
  });
});

describe("resolveBillingCycle", () => {
  it("is null when nothing tells the closing dates", () => {
    expect(
      resolveBillingCycle({ attributes: {}, metrics: null, today: "2026-10-31" })
    ).toBeNull();
    expect(
      resolveBillingCycle({
        attributes: cardAttributes(null, 5),
        metrics: cardMetrics({ billed_amount: 1000000 }),
        today: "2026-10-31",
      })
    ).toBeNull();
  });

  it("takes every date from the latest statement while it is current", () => {
    expect(
      resolveBillingCycle({
        attributes: cardAttributes(15, 5),
        metrics: cardMetrics({
          statement_date: "2026-10-15",
          next_statement_date: "2026-11-14",
          payment_due_date: "2026-11-05",
        }),
        today: "2026-10-31",
      })
    ).toEqual({
      lastClosing: { date: "2026-10-15", estimated: false },
      previousClosing: "2026-09-15",
      nextClosing: { date: "2026-11-14", estimated: false },
      dueDate: { date: "2026-11-05", estimated: false },
      basis: "statement",
      rolledOver: false,
    });
  });

  it("estimates every date from the statement day alone", () => {
    expect(
      resolveBillingCycle({
        attributes: cardAttributes(15, 5),
        metrics: null,
        today: "2026-10-31",
      })
    ).toEqual({
      lastClosing: { date: "2026-10-15", estimated: true },
      previousClosing: "2026-09-15",
      nextClosing: { date: "2026-11-15", estimated: true },
      dueDate: { date: "2026-11-05", estimated: true },
      basis: "statement_day",
      rolledOver: false,
    });
  });

  it("closes a day-31 card on the last day of February and 30-day months", () => {
    const cycle = (today: string) =>
      resolveBillingCycle({
        attributes: cardAttributes(31),
        metrics: null,
        today,
      });
    expect(cycle("2026-03-10")).toMatchObject({
      lastClosing: { date: "2026-02-28" },
      previousClosing: "2026-01-31",
      nextClosing: { date: "2026-03-31" },
    });
    expect(cycle("2028-03-10")).toMatchObject({
      lastClosing: { date: "2028-02-29" },
    });
    expect(cycle("2026-05-01")).toMatchObject({
      lastClosing: { date: "2026-04-30" },
      previousClosing: "2026-03-31",
      nextClosing: { date: "2026-05-31" },
    });
    expect(cycle("2026-02-27")).toMatchObject({
      lastClosing: { date: "2026-01-31" },
      nextClosing: { date: "2026-02-28" },
    });
  });

  it("keeps a closing that falls today as the next one", () => {
    expect(
      resolveBillingCycle({
        attributes: cardAttributes(15),
        metrics: null,
        today: "2026-10-15",
      })
    ).toMatchObject({
      lastClosing: { date: "2026-09-15" },
      nextClosing: { date: "2026-10-15" },
    });
    expect(
      resolveBillingCycle({
        attributes: cardAttributes(15),
        metrics: cardMetrics({
          statement_date: "2026-09-15",
          next_statement_date: "2026-10-15",
        }),
        today: "2026-10-15",
      })
    ).toMatchObject({
      lastClosing: { date: "2026-09-15", estimated: false },
      nextClosing: { date: "2026-10-15", estimated: false },
      rolledOver: false,
    });
  });

  it("rolls over once the bank's next closing has passed", () => {
    const cycle = resolveBillingCycle({
      attributes: cardAttributes(15, 5),
      metrics: cardMetrics({
        statement_date: "2026-09-15",
        next_statement_date: "2026-10-14",
        payment_due_date: "2026-10-05",
      }),
      today: "2026-10-20",
    });
    expect(cycle).toEqual({
      lastClosing: { date: "2026-10-14", estimated: true },
      previousClosing: "2026-09-15",
      nextClosing: { date: "2026-11-15", estimated: true },
      // The statement's due date is the September one's: the due day decides.
      dueDate: { date: "2026-11-05", estimated: true },
      basis: "statement",
      rolledOver: true,
    });
  });

  it("drops the statement's due date after a rollover without a due day", () => {
    expect(
      resolveBillingCycle({
        attributes: cardAttributes(15),
        metrics: cardMetrics({
          statement_date: "2026-09-15",
          payment_due_date: "2026-10-05",
        }),
        today: "2026-10-20",
      })?.dueDate
    ).toBeNull();
  });

  it("rolls a stale statement forward by whole cycles", () => {
    expect(
      resolveBillingCycle({
        attributes: {},
        metrics: cardMetrics({ statement_date: "2026-06-30" }),
        today: "2026-10-31",
      })
    ).toMatchObject({
      // Without a statement day the statement's own day (30) is the cycle's.
      lastClosing: { date: "2026-10-30", estimated: true },
      previousClosing: "2026-09-30",
      nextClosing: { date: "2026-11-30", estimated: true },
      rolledOver: true,
    });
  });

  it("keeps a cycle's length when the bank moved a closing across a month", () => {
    // Day 30, but the September statement closed on October 1st.
    expect(
      resolveBillingCycle({
        attributes: cardAttributes(30),
        metrics: cardMetrics({ statement_date: "2026-10-01" }),
        today: "2026-10-20",
      })
    ).toMatchObject({
      lastClosing: { date: "2026-10-01", estimated: false },
      previousClosing: "2026-08-30",
      nextClosing: { date: "2026-10-30", estimated: true },
      rolledOver: false,
    });
    // Day 1, but the October statement closed on September 30th.
    expect(
      resolveBillingCycle({
        attributes: cardAttributes(1),
        metrics: cardMetrics({ statement_date: "2026-09-30" }),
        today: "2026-10-05",
      })
    ).toMatchObject({
      lastClosing: { date: "2026-09-30", estimated: false },
      previousClosing: "2026-09-01",
      nextClosing: { date: "2026-11-01", estimated: true },
      rolledOver: false,
    });
  });

  it("estimates the next closing a month on when the bank gave none", () => {
    expect(
      resolveBillingCycle({
        attributes: {},
        metrics: cardMetrics({ statement_date: "2026-10-15" }),
        today: "2026-10-31",
      })
    ).toMatchObject({
      lastClosing: { date: "2026-10-15", estimated: false },
      nextClosing: { date: "2026-11-15", estimated: true },
      dueDate: null,
    });
  });

  it("puts the due date on the first due day after the closing", () => {
    const due = (statementDay: number, dueDay: number, today: string) =>
      resolveBillingCycle({
        attributes: cardAttributes(statementDay, dueDay),
        metrics: null,
        today,
      })?.dueDate?.date;
    // Across the year end.
    expect(due(25, 10, "2026-12-28")).toBe("2027-01-10");
    // Later the same month, clamped to a 30-day month.
    expect(due(20, 31, "2026-11-25")).toBe("2026-11-30");
    // Due on the closing day itself means the next month's.
    expect(due(15, 15, "2026-10-20")).toBe("2026-11-15");
  });

  it("falls back to the newest statement date on the movements", () => {
    expect(
      resolveBillingCycle({
        attributes: cardAttributes(null, 5),
        metrics: cardMetrics({ payment_due_date: "2026-11-09" }),
        latestStatementDate: "2026-10-15",
        today: "2026-10-20",
      })
    ).toMatchObject({
      lastClosing: { date: "2026-10-15", estimated: false },
      basis: "transactions",
      // The statement's due date is not tied to this closing.
      dueDate: { date: "2026-11-05", estimated: true },
    });
  });

  it("prefers a movement billed on a newer statement than the metrics", () => {
    expect(
      resolveBillingCycle({
        attributes: cardAttributes(15),
        metrics: cardMetrics({ statement_date: "2026-09-15" }),
        latestStatementDate: "2026-10-15",
        today: "2026-10-20",
      })
    ).toMatchObject({
      lastClosing: { date: "2026-10-15", estimated: false },
      basis: "transactions",
      rolledOver: false,
    });
  });

  it("works back from the bank's next closing alone", () => {
    expect(
      resolveBillingCycle({
        attributes: {},
        metrics: cardMetrics({ next_statement_date: "2026-11-14" }),
        today: "2026-10-31",
      })
    ).toMatchObject({
      lastClosing: { date: "2026-10-14", estimated: true },
      previousClosing: "2026-09-14",
      nextClosing: { date: "2026-11-14", estimated: false },
      basis: "next_statement",
    });
  });

  it("keeps a closing worked back from a far next closing in the past", () => {
    expect(
      resolveBillingCycle({
        attributes: {},
        metrics: cardMetrics({ next_statement_date: "2026-12-15" }),
        today: "2026-10-31",
      })
    ).toMatchObject({
      lastClosing: { date: "2026-10-15", estimated: true },
      previousClosing: "2026-09-15",
      // A closing falls between today and the bank's: that one is next.
      nextClosing: { date: "2026-11-15", estimated: true },
      basis: "next_statement",
    });
  });

  it("keeps a closing worked back to today as the next one", () => {
    expect(
      resolveBillingCycle({
        attributes: {},
        metrics: cardMetrics({ next_statement_date: "2026-11-20" }),
        today: "2026-10-20",
      })
    ).toMatchObject({
      lastClosing: { date: "2026-09-20", estimated: true },
      nextClosing: { date: "2026-10-20", estimated: true },
    });
  });

  it("ignores future, malformed and out-of-range values", () => {
    expect(
      resolveBillingCycle({
        attributes: cardAttributes(15),
        metrics: cardMetrics({ statement_date: "2026-11-15" }),
        today: "2026-10-31",
      })
    ).toMatchObject({ basis: "statement_day" });
    expect(
      resolveBillingCycle({
        attributes: cardAttributes(15),
        metrics: cardMetrics({ statement_date: "15/10/2026" }),
        today: "2026-10-31",
      })
    ).toMatchObject({ basis: "statement_day" });
    for (const day of [0, 32, 1.5]) {
      expect(
        resolveBillingCycle({
          attributes: cardAttributes(day),
          metrics: null,
          today: "2026-10-31",
        })
      ).toBeNull();
    }
  });
});

describe("effectiveBillingStatus", () => {
  it("keeps the bank's billed status and statement date", () => {
    expect(
      effectiveBillingStatus(
        txn("2026-10-20", -999999, "billed", "2026-10-15"),
        OCTOBER_CYCLE
      )
    ).toEqual({ status: "billed", estimated: false, statementDate: "2026-10-15" });
    expect(
      effectiveBillingStatus(txn("2026-10-20", -999999, "billed"), null)
    ).toEqual({ status: "billed", estimated: false, statementDate: null });
  });

  it("keeps the bank's unbilled status within the last two cycles", () => {
    expect(
      effectiveBillingStatus(txn("2026-09-16", -999999, "unbilled"), OCTOBER_CYCLE)
    ).toEqual({ status: "unbilled", estimated: false, statementDate: null });
    expect(
      effectiveBillingStatus(txn("2026-09-01", -999999, "unbilled"), null)
    ).toEqual({ status: "unbilled", estimated: false, statementDate: null });
  });

  it("takes a charge still unbilled since the previous closing as billed", () => {
    expect(
      effectiveBillingStatus(txn("2026-09-15", -999999, "unbilled"), OCTOBER_CYCLE)
    ).toEqual({ status: "billed", estimated: true, statementDate: null });
  });

  it("takes an unbilled charge as billed once a later closing has passed", () => {
    const rolled: BillingCycle = {
      ...OCTOBER_CYCLE,
      lastClosing: { date: "2026-10-15", estimated: true },
      dueDate: null,
      rolledOver: true,
    };
    // Reported unbilled before the October closing the scraper never read.
    expect(
      effectiveBillingStatus(txn("2026-10-15", -999999, "unbilled"), rolled)
    ).toEqual({ status: "billed", estimated: true, statementDate: null });
    expect(
      effectiveBillingStatus(txn("2026-10-16", -999999, "unbilled"), rolled)
    ).toEqual({ status: "unbilled", estimated: false, statementDate: null });
    // Without a rollover the bank's unbilled still holds after the closing.
    expect(
      effectiveBillingStatus(txn("2026-10-15", -999999, "unbilled"), OCTOBER_CYCLE)
    ).toEqual({ status: "unbilled", estimated: false, statementDate: null });
  });

  it("derives a status from the last closing, the closing day billed", () => {
    expect(
      effectiveBillingStatus(txn("2026-10-15", -999999), OCTOBER_CYCLE)
    ).toEqual({ status: "billed", estimated: true, statementDate: null });
    expect(
      effectiveBillingStatus(txn("2026-10-16", -999999), OCTOBER_CYCLE)
    ).toEqual({ status: "unbilled", estimated: true, statementDate: null });
  });

  it("is null with neither a reported status nor a cycle", () => {
    expect(effectiveBillingStatus(txn("2026-10-16", -999999), null)).toBeNull();
  });
});

describe("summarizeBilling", () => {
  const TODAY = "2026-10-31";
  const STATEMENT = cardMetrics({
    statement_date: "2026-10-15",
    next_statement_date: "2026-11-14",
    payment_due_date: "2026-11-05",
    billed_amount: 1000000,
    minimum_payment: 999999,
  });

  function summarize(
    transactions: BillingTransactionInput[],
    {
      metrics = STATEMENT as CreditCardMetrics | null,
      attributes = cardAttributes(15, 5) as ReturnType<typeof cardAttributes> | Record<string, never>,
      truncated = false,
      today = TODAY,
    } = {}
  ) {
    return summarizeBilling({
      kind: "credit_card",
      attributes,
      metrics,
      transactions,
      truncated,
      today,
    });
  }

  it("is null for every kind but a credit card", () => {
    for (const kind of ["checking", "line_of_credit"] as const) {
      expect(
        summarizeBilling({
          kind,
          attributes: {},
          metrics: null,
          transactions: [txn("2026-10-20", -999999, "unbilled")],
          truncated: false,
          today: TODAY,
        })
      ).toBeNull();
    }
  });

  it("subtracts the payments since the closing from the billed amount", () => {
    const billing = summarize([
      txn("2026-10-10", -1000000, "billed", "2026-10-15"),
      // On the closing day: already on the statement, not a later payment.
      txn("2026-10-15", 2500000, "billed", "2026-10-15"),
      txn("2026-10-20", 999999, "unbilled"),
    ]);
    expect(billing?.summary).toMatchObject({
      asOf: TODAY,
      statement: { date: "2026-10-15", billedAmount: 1000000, minimumPayment: 999999 },
      paidSinceClosing: 999999,
      billedPending: 1,
      billedPendingEstimated: true,
      hasBilledDebt: true,
    });
    expect(billedDebtState(billing!.summary)).toBe("pending");
  });

  it("floors the billed debt at zero once paid in full or more", () => {
    for (const payment of [1000000, 2500000]) {
      const { summary } = summarize([txn("2026-10-20", payment, "unbilled")])!;
      expect(summary.billedPending).toBe(0);
      expect(summary.hasBilledDebt).toBe(false);
      expect(billedDebtState(summary)).toBe("paid");
    }
  });

  it("counts a refund after the closing as paid", () => {
    const { summary } = summarize([txn("2026-10-25", 999999)])!;
    expect(summary.paidSinceClosing).toBe(999999);
    expect(summary.billedPending).toBe(1);
  });

  it("drops a reversed unbilled charge and its reversal from both totals", () => {
    const { summary } = summarize([
      txn("2026-10-20", -2500000, "unbilled"),
      txn("2026-10-22", 2500000, "unbilled"),
      txn("2026-10-23", -999999, "unbilled"),
    ])!;
    expect(summary).toMatchObject({
      unbilledCharges: 999999,
      unbilledCount: 1,
      paidSinceClosing: 0,
      billedPending: 1000000,
      hasBilledDebt: true,
    });
    expect(billedDebtState(summary)).toBe("pending");
  });

  it("keeps a linked transfer or a statement figure as a payment", () => {
    const transfer = {
      ...txn("2026-10-22", 2500000, "unbilled"),
      isInternalTransfer: true,
    };
    expect(
      summarize([txn("2026-10-20", -2500000, "unbilled"), transfer])!.summary
    ).toMatchObject({ unbilledCharges: 2500000, paidSinceClosing: 2500000 });
    // Paying exactly the billed amount, which an unbilled charge also costs.
    expect(
      summarize([
        txn("2026-10-20", -1000000, "unbilled"),
        txn("2026-10-22", 1000000, "unbilled"),
      ])!.summary
    ).toMatchObject({
      unbilledCharges: 1000000,
      paidSinceClosing: 1000000,
      billedPending: 0,
    });
  });

  it("counts a credit the bank still lists as unbilled as paid, whatever its date", () => {
    const { summary } = summarize([
      // Paid on the closing day, posted after it: not on the statement.
      txn("2026-10-15", 999999, "unbilled"),
      // On the statement already.
      txn("2026-10-14", 2500000, "billed", "2026-10-15"),
      txn("2026-10-14", 2500000),
    ])!;
    expect(summary.paidSinceClosing).toBe(999999);
    expect(summary.billedPending).toBe(1);
  });

  it("reports no billed debt on a statement that billed nothing", () => {
    const { summary } = summarize([], {
      metrics: cardMetrics({ statement_date: "2026-10-15", billed_amount: 0 }),
    })!;
    expect(summary).toMatchObject({
      billedPending: 0,
      billedPendingEstimated: false,
      hasBilledDebt: false,
    });
    expect(billedDebtState(summary)).toBe("none");
  });

  it("totals unbilled charges without netting unbilled payments", () => {
    const billing = summarize([
      txn("2026-10-16", -999999, "unbilled"),
      txn("2026-10-18", 2500000, "unbilled"),
      txn("2026-10-20", -1000000),
      // Billed by the bank, so not part of the unbilled total.
      txn("2026-10-21", -999999, "billed", "2026-10-15"),
    ])!;
    expect(billing.summary).toMatchObject({
      unbilledCharges: 1999999,
      unbilledCount: 2,
      statusCount: 4,
    });
    expect(billing.statuses.map((s) => s?.status)).toEqual([
      "unbilled",
      "unbilled",
      "unbilled",
      "billed",
    ]);
  });

  it("rounds USD totals to cents", () => {
    const { summary } = summarize(
      [
        txn("2026-10-16", -0.1, "unbilled"),
        txn("2026-10-17", -0.2, "unbilled"),
        txn("2026-10-20", 1000.1),
        txn("2026-10-22", 234.46),
      ],
      {
        metrics: cardMetrics({
          statement_date: "2026-10-15",
          billed_amount: 1234.56,
          minimum_payment: null,
        }),
      }
    )!;
    expect(summary.unbilledCharges).toBe(0.3);
    expect(summary.paidSinceClosing).toBe(1234.56);
    expect(summary.billedPending).toBe(0);
    expect(summary.hasBilledDebt).toBe(false);
    expect(summary.statement.minimumPayment).toBeNull();
  });

  it("leaves the billed debt unknown once a newer closing passed", () => {
    const { summary } = summarize([txn("2026-10-20", 999999)], {
      metrics: cardMetrics({
        statement_date: "2026-09-15",
        next_statement_date: "2026-10-15",
        billed_amount: 1000000,
      }),
    })!;
    expect(summary.cycle?.rolledOver).toBe(true);
    expect(summary.statement.billedAmount).toBe(1000000);
    expect(summary).toMatchObject({
      billedPending: null,
      billedPendingEstimated: false,
      hasBilledDebt: null,
    });
    expect(billedDebtState(summary)).toBe("unknown");
  });

  it("leaves the billed debt unknown without a statement date", () => {
    const { summary } = summarize([], {
      metrics: cardMetrics({ billed_amount: 1000000 }),
    })!;
    expect(summary.cycle?.basis).toBe("statement_day");
    expect(summary.billedPending).toBeNull();
  });

  it("derives the cycle from the movements' statement dates", () => {
    const billing = summarize(
      [
        txn("2026-10-10", -999999, "billed", "2026-10-15"),
        txn("2026-10-12", -1000000),
        txn("2026-10-20", -999999),
      ],
      { metrics: null, attributes: {} }
    )!;
    expect(billing.summary.cycle).toMatchObject({
      basis: "transactions",
      lastClosing: { date: "2026-10-15", estimated: false },
    });
    expect(billing.statuses).toEqual([
      { status: "billed", estimated: false, statementDate: "2026-10-15" },
      { status: "billed", estimated: true, statementDate: null },
      { status: "unbilled", estimated: true, statementDate: null },
    ]);
  });

  it("knows nothing about a card that reports nothing", () => {
    const billing = summarize([txn("2026-10-20", -999999)], {
      metrics: null,
      attributes: {},
    })!;
    expect(billing.statuses).toEqual([null]);
    expect(billing.summary).toEqual({
      asOf: TODAY,
      cycle: null,
      statement: {
        date: null,
        billedAmount: null,
        minimumPayment: null,
        paymentDueDate: null,
      },
      paidSinceClosing: null,
      billedPending: null,
      billedPendingEstimated: false,
      hasBilledDebt: null,
      unbilledCharges: 0,
      unbilledCount: 0,
      statusCount: 0,
      complete: true,
    });
  });

  it("totals bank-reported unbilled charges without any cycle", () => {
    const { summary } = summarize(
      [txn("2026-10-20", -999999, "unbilled"), txn("2026-10-21", -1000000)],
      { metrics: null, attributes: {} }
    )!;
    expect(summary).toMatchObject({
      cycle: null,
      unbilledCharges: 999999,
      unbilledCount: 1,
      statusCount: 1,
    });
  });

  it("flags totals from a cut list that may miss the open cycles", () => {
    const rows = (oldest: string) => [
      txn(oldest, -999999),
      txn("2026-10-20", -1000000),
    ];
    expect(summarize(rows("2026-09-16"), { truncated: true })!.summary.complete).toBe(
      false
    );
    expect(summarize(rows("2026-09-15"), { truncated: true })!.summary.complete).toBe(
      true
    );
    expect(
      summarize(rows("2026-09-16"), {
        truncated: true,
        metrics: null,
        attributes: {},
      })!.summary.complete
    ).toBe(false);
    expect(summarize(rows("2026-09-16"))!.summary.complete).toBe(true);
  });
});

describe("olderStatementDate", () => {
  const summary = (
    metrics: Partial<CreditCardMetrics>,
    transactions: BillingTransactionInput[] = []
  ) =>
    summarizeBilling({
      kind: "credit_card",
      attributes: cardAttributes(15, 5),
      metrics: cardMetrics(metrics),
      transactions,
      truncated: false,
      today: "2026-10-31",
    })!.summary;

  it("names the statement the figures belong to when it is not the last one", () => {
    // A movement was billed on a newer statement than the metrics hold.
    expect(
      olderStatementDate(
        summary({ statement_date: "2026-09-15", next_statement_date: "2026-11-15" }, [
          txn("2026-10-10", -999999, "billed", "2026-10-15"),
        ])
      )
    ).toBe("2026-09-15");
    // A closing passed since the statement was read.
    expect(
      olderStatementDate(
        summary({ statement_date: "2026-09-15", next_statement_date: "2026-10-15" })
      )
    ).toBe("2026-09-15");
  });

  it("is null while the statement is the last closing's, or unknown", () => {
    expect(olderStatementDate(summary({ statement_date: "2026-10-15" }))).toBeNull();
    expect(olderStatementDate(summary({ billed_amount: 1000000 }))).toBeNull();
  });
});

describe("isPaymentOverdue", () => {
  const metrics = cardMetrics({
    statement_date: "2026-10-15",
    payment_due_date: "2026-11-05",
    billed_amount: 1000000,
  });
  const summary = (today: string, payment: number) =>
    summarizeBilling({
      kind: "credit_card",
      attributes: cardAttributes(15, 5),
      metrics: { ...metrics, next_statement_date: "2026-11-15" },
      transactions: [txn("2026-10-20", payment)],
      truncated: false,
      today,
    })!.summary;

  it("is true only past the due date with billed debt still owed", () => {
    expect(isPaymentOverdue(summary("2026-11-06", 999999))).toBe(true);
    expect(isPaymentOverdue(summary("2026-11-05", 999999))).toBe(false);
    expect(isPaymentOverdue(summary("2026-11-06", 1000000))).toBe(false);
  });

  it("is false once the minimum payment was paid", () => {
    const withMinimum = (payment: number) =>
      summarizeBilling({
        kind: "credit_card",
        attributes: cardAttributes(15, 5),
        metrics: {
          ...metrics,
          next_statement_date: "2026-11-15",
          billed_amount: 2500000,
          minimum_payment: 1000000,
        },
        transactions: [txn("2026-10-20", payment)],
        truncated: false,
        today: "2026-11-06",
      })!.summary;
    expect(isPaymentOverdue(withMinimum(999999))).toBe(true);
    expect(isMinimumPaid(withMinimum(999999))).toBe(false);
    expect(isPaymentOverdue(withMinimum(1000000))).toBe(false);
    expect(isMinimumPaid(withMinimum(1000000))).toBe(true);
    // Paid in full: no debt left, so neither applies.
    expect(isMinimumPaid(withMinimum(2500000))).toBe(false);
  });
});
