import { describe, it, expect } from "vitest";
import { METRIC_FIELDS, PRODUCT_KINDS } from "@chanchito/product-model";
import {
  groupAdjustmentsByDay,
  isForeignKeyViolation,
  isUniqueViolation,
  parseMonthParam,
  type MonitorAdjustmentRow,
} from "@/lib/monitors/adjustments";
import {
  affectsMonitor,
  monitorProductIds,
  withEffects,
} from "@/lib/monitors/effects";
import { UNDO_DIRECTIONS, undoDeltas, undoMoves } from "@/lib/monitors/undo";
import type { EvalContext, ProductInfo } from "@/lib/monitors";

// All figures and identifiers below are synthetic (see the repo's personal
// data policy): fake uuids and obviously fake CLP/USD amounts.
const MONITOR_ID = "11111111-1111-4111-8111-111111111111";
const CHECKING_ID = "22222222-2222-4222-8222-222222222222";
const CARD_ID = "33333333-3333-4333-8333-333333333333";
const OTHER_ID = "44444444-4444-4444-8444-444444444444";

describe("UNDO_DIRECTIONS", () => {
  it("covers every kind and only names its currency metric fields", () => {
    expect(Object.keys(UNDO_DIRECTIONS).sort()).toEqual([...PRODUCT_KINDS].sort());
    for (const kind of PRODUCT_KINDS) {
      for (const field of Object.keys(UNDO_DIRECTIONS[kind])) {
        expect(METRIC_FIELDS[kind][field]?.denomination, `${kind}.${field}`).toBe(
          "currency"
        );
      }
    }
  });

  it.each([
    ["checking", "balance", -1],
    ["checking", "accounting_balance", -1],
    ["wallet", "balance", -1],
    ["credit_card", "owed", 1],
    ["credit_card", "available", -1],
    ["line_of_credit", "owed", 1],
    ["loan", "owed", 1],
    ["investment", "nav", -1],
  ] as const)("%s.%s undoes with direction %d", (kind, field, direction) => {
    expect(UNDO_DIRECTIONS[kind][field]).toBe(direction);
  });

  it.each([
    ["credit_card", "limit"],
    ["loan", "installments_paid"],
    ["loan", "next_payment_amount"],
    ["investment", "profit"],
    ["crypto", "units"],
  ] as const)("%s.%s does not move", (kind, field) => {
    expect(undoMoves(kind, field)).toBe(false);
  });
});

describe("undoDeltas", () => {
  const kinds: Record<string, "checking" | "credit_card"> = {
    [CHECKING_ID]: "checking",
    [CARD_ID]: "credit_card",
  };
  const kindOf = (id: string) => kinds[id];
  const txn = (productId: string, amount: number, deleted = false) => ({
    transactionId: `${productId}:${amount}`,
    productId,
    transactionDate: "2026-07-10",
    amount,
    deleted,
  });

  it("sums per product and field, skipping deleted and unknown ones", () => {
    const deltas = undoDeltas(
      [
        txn(CHECKING_ID, -1000000),
        txn(CHECKING_ID, 999999),
        txn(CARD_ID, -2500000),
        txn(CARD_ID, -1000000, true),
        txn(OTHER_ID, -1000000),
      ],
      kindOf
    );
    expect(Object.fromEntries(deltas)).toEqual({
      [CHECKING_ID]: { balance: 1, accounting_balance: 1 },
      [CARD_ID]: { owed: -2500000, available: 2500000 },
    });
  });
});

describe("parseMonthParam", () => {
  it.each([
    [null, { ok: true, value: null }],
    ["2026-07", { ok: true, value: "2026-07" }],
  ])("accepts %j", (raw, expected) => {
    expect(parseMonthParam(raw)).toEqual(expected);
  });

  it.each(["2026-13", "2026-7", "2026-07-01", "julio"])(
    "rejects %j",
    (raw) => {
      const result = parseMonthParam(raw);
      expect(result.ok).toBe(false);
      if (result.ok) return;
      expect(result.field).toBe("month");
    }
  );
});

describe("monitorProductIds and affectsMonitor", () => {
  const monitor = (expression: string, ...thresholds: string[]) => ({
    expression,
    thresholds: thresholds.map((t) => ({ severity: "alert", expression: t })),
  });
  const full = monitor(
    `@{${CHECKING_ID}:balance} - @{${CARD_ID}:owed} * 0.7 + @{${CARD_ID}:limit}`,
    `@{${OTHER_ID}:balance} * 0.5`
  );

  it("lists each product of the left side and the thresholds once", () => {
    expect(monitorProductIds(full)).toEqual([CHECKING_ID, CARD_ID, OTHER_ID]);
    expect(monitorProductIds(monitor("DAY_OF_MONTH() * 1000", "1"))).toEqual([]);
    expect(monitorProductIds(monitor("1 +", "2 *"))).toEqual([]);
  });

  it("is true only when a read field moves, on either side", () => {
    expect(affectsMonitor(full, CARD_ID, "credit_card")).toBe(true);
    expect(affectsMonitor(full, OTHER_ID, "checking")).toBe(true);
    expect(
      affectsMonitor(monitor(`@{${CARD_ID}:limit}`), CARD_ID, "credit_card")
    ).toBe(false);
    expect(affectsMonitor(monitor("1", "2"), OTHER_ID, "checking")).toBe(false);
    expect(affectsMonitor(full, CARD_ID, undefined)).toBe(false);
  });
});

describe("withEffects", () => {
  const products = new Map<string, ProductInfo>([
    [
      CHECKING_ID,
      {
        id: CHECKING_ID,
        kind: "checking",
        currency: "CLP",
        isActive: true,
        metrics: { kind: "checking", balance: 2500000 },
        balanceAsOf: null,
        slug: "cuenta_corriente",
        institutionSlug: "banco_demo",
        name: "Cuenta corriente",
      },
    ],
    [
      CARD_ID,
      {
        id: CARD_ID,
        kind: "credit_card",
        currency: "USD",
        isActive: true,
        metrics: { kind: "credit_card", available: 1000, owed: 100 },
        balanceAsOf: null,
        slug: "tarjeta_usd",
        institutionSlug: "banco_demo",
        name: "Tarjeta USD",
      },
    ],
  ]);
  const ctx: EvalContext = {
    date: new Date(2026, 6, 20),
    products,
    rates: { provider: { CLP: 1, USD: 900 }, overrides: [] },
    currency: "CLP",
  };

  function row(
    id: string,
    productId: string,
    transactionDate: string,
    amount: number,
    deleted = false
  ): MonitorAdjustmentRow {
    return {
      id,
      monitorId: MONITOR_ID,
      transactionId: `${id}-txn`,
      productId,
      transactionDate,
      amount,
      deleted,
      createdAt: "2026-07-20T12:00:00.000Z",
      updatedAt: "2026-07-20T12:00:00.000Z",
      description: "Compra de prueba",
      productName: "Producto de prueba",
      productCurrency: products.get(productId)!.currency,
      institutionSlug: "banco_demo",
      institutionName: "Banco Demo",
    };
  }

  const monitor = (expression: string, threshold = "1000000") => ({
    expression,
    thresholds: [
      { severity: "warning", expression: "999999999" },
      { severity: "alert", expression: threshold },
    ],
  });
  const expression = monitor(`@{${CHECKING_ID}:balance} - @{${CARD_ID}:owed}`);

  it("gives each row its own effect and a month-to-date total", () => {
    const rows = [
      row("a", CHECKING_ID, "2026-07-03", -1000000),
      row("b", CARD_ID, "2026-07-15", -1234.56),
      row("c", CHECKING_ID, "2026-07-15", 999999),
      row("d", CHECKING_ID, "2026-07-16", -2500000, true),
      row("e", CHECKING_ID, "2026-08-01", -1000000),
    ];
    const result = withEffects(rows, expression, ctx);
    expect(
      result.map((r) => [r.id, r.affects, r.effect, r.monthToDateEffect])
    ).toEqual([
      ["a", true, -1000000, -1000000],
      // 1234.56 USD at 900 CLP.
      ["b", true, -1111104, -1111105],
      ["c", true, 999999, -1111105],
      ["d", true, null, null],
      // Another month: its own total starts over.
      ["e", true, -1000000, -1000000],
    ]);
  });

  it("is 0 for a product the expression no longer reads", () => {
    const [result] = withEffects(
      [row("a", CARD_ID, "2026-07-03", -1000000)],
      monitor(`@{${CHECKING_ID}:balance}`),
      ctx
    );
    expect(result.affects).toBe(false);
    expect(result.effect).toBe(0);
  });

  it("is null when the expression has no data or does not parse", () => {
    const rows = [row("a", CHECKING_ID, "2026-07-03", -1000000)];
    expect(
      withEffects(
        rows,
        monitor(`@{${CHECKING_ID}:balance} + @{${OTHER_ID}:balance}`),
        ctx
      )[0].effect
    ).toBeNull();
    expect(withEffects(rows, monitor("1 +"), ctx)[0].effect).toBeNull();
    expect(
      withEffects(rows, monitor(`@{${CHECKING_ID}:balance}`, "2 *"), ctx)[0]
        .effect
    ).toBeNull();
  });

  it("reports the alert threshold, including its own refs", () => {
    // The alert threshold reads `- owed` like the left side: undoing a
    // -1234.56 USD purchase moves both by the same CLP amount, so they
    // cancel (the warning threshold, a constant, would not).
    const [result] = withEffects(
      [row("b", CARD_ID, "2026-07-15", -1234.56)],
      monitor(
        `@{${CHECKING_ID}:balance} - @{${CARD_ID}:owed}`,
        `- @{${CARD_ID}:owed}`
      ),
      ctx
    );
    expect(result.effect).toBe(0);
    expect(result.affects).toBe(true);
  });
});

describe("groupAdjustmentsByDay", () => {
  const adjustments = [
    { id: "a", transactionDate: "2026-07-15", deleted: false, monthToDateEffect: -10000 },
    { id: "b", transactionDate: "2026-07-03", deleted: false, monthToDateEffect: 30000 },
    { id: "c", transactionDate: "2026-07-15", deleted: false, monthToDateEffect: -10000 },
    { id: "d", transactionDate: "2026-07-20", deleted: true, monthToDateEffect: null },
    { id: "e", transactionDate: "2026-08-01", deleted: false, monthToDateEffect: 999999 },
  ];

  it("groups per day with day and month-to-date totals from the effects", () => {
    const days = groupAdjustmentsByDay(adjustments, "2026-07");
    expect(
      days.map((d) => ({
        date: d.transactionDate,
        ids: d.entries.map((e) => e.id),
        dayTotal: d.dayTotal,
        runningTotal: d.runningTotal,
      }))
    ).toEqual([
      { date: "2026-07-03", ids: ["b"], dayTotal: 30000, runningTotal: 30000 },
      {
        date: "2026-07-15",
        ids: ["a", "c"],
        dayTotal: -40000,
        runningTotal: -10000,
      },
      // Only a deleted one: nothing changes that day.
      { date: "2026-07-20", ids: ["d"], dayTotal: 0, runningTotal: -10000 },
    ]);
  });

  it("propagates a missing total as null", () => {
    const days = groupAdjustmentsByDay(
      [
        { transactionDate: "2026-07-01", deleted: false, monthToDateEffect: null },
        { transactionDate: "2026-07-02", deleted: false, monthToDateEffect: 0.3 },
      ],
      "2026-07"
    );
    expect(days.map((d) => [d.dayTotal, d.runningTotal])).toEqual([
      [null, null],
      [null, 0.3],
    ]);
  });

  it("keeps only the requested month", () => {
    expect(groupAdjustmentsByDay(adjustments, "2026-08")).toHaveLength(1);
    expect(groupAdjustmentsByDay(adjustments, "2026-09")).toEqual([]);
  });
});

describe("postgres error codes", () => {
  it.each([
    [{ code: "23503" }, true, false],
    [{ message: "Failed query", cause: { code: "23503" } }, true, false],
    [{ cause: { code: "23505" } }, false, true],
    [new Error("boom"), false, false],
    [null, false, false],
  ])("%j", (error, foreignKey, unique) => {
    expect(isForeignKeyViolation(error)).toBe(foreignKey);
    expect(isUniqueViolation(error)).toBe(unique);
  });
});
