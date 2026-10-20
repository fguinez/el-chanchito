import { describe, it, expect } from "vitest";
import {
  adjustmentOnDate,
  adjustmentsInEffect,
  thresholdShift,
  bindExpression,
  comparatorHolds,
  evaluateExpression,
  evaluateMonitor,
  parseExpression,
  serializeExpression,
  type EvalContext,
  type MonitorAdjustment,
  type MonitorDefinition,
  type ProductCatalog,
  type ProductInfo,
} from "@/lib/monitors";
import type { RateBook } from "@/lib/rates";

// All figures and identifiers below are synthetic (see the repo's personal
// data policy): fake uuids, fake CLP/USD amounts, fake slugs.
const CHECKING_ID = "11111111-1111-4111-8111-111111111111";
const CARD_CLP_ID = "22222222-2222-4222-8222-222222222222";
const CARD_USD_ID = "33333333-3333-4333-8333-333333333333";
const CARD_LIDER_ID = "44444444-4444-4444-8444-444444444444";
const FUND_ID = "55555555-5555-4555-8555-555555555555";
const LOAN_ID = "66666666-6666-4666-8666-666666666666";
const EUR_WALLET_ID = "77777777-7777-4777-8777-777777777777";
const CLOSED_ID = "88888888-8888-4888-8888-888888888888";
const UNKNOWN_ID = "99999999-9999-4999-8999-999999999999";
const STOCK_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";

const rates: RateBook = { provider: { CLP: 1, USD: 900 }, overrides: [] };

type Overrides = Partial<ProductInfo> &
  Pick<ProductInfo, "id" | "kind" | "slug" | "institutionSlug">;

function productInfo(overrides: Overrides): ProductInfo {
  return {
    currency: "CLP",
    isActive: true,
    metrics: null,
    balanceAsOf: null,
    name: overrides.slug,
    ...overrides,
  };
}

/** Products for the issue #41 example plus edge-case fixtures. */
function buildProducts(checkingBalance: number): Map<string, ProductInfo> {
  const list: ProductInfo[] = [
    productInfo({
      id: CHECKING_ID,
      kind: "checking",
      slug: "cuenta_corriente",
      institutionSlug: "banchile",
      metrics: { kind: "checking", balance: checkingBalance },
      balanceAsOf: new Date("2026-07-15T12:00:00Z"),
    }),
    productInfo({
      id: CARD_CLP_ID,
      kind: "credit_card",
      slug: "tarjeta_clp",
      institutionSlug: "banchile",
      metrics: { kind: "credit_card", available: 1000000, owed: 300000 },
      balanceAsOf: new Date("2026-07-14T12:00:00Z"),
    }),
    productInfo({
      id: CARD_USD_ID,
      kind: "credit_card",
      slug: "tarjeta_usd",
      institutionSlug: "banchile",
      currency: "USD",
      metrics: { kind: "credit_card", available: 1000, owed: 100 },
      balanceAsOf: new Date("2026-07-15T08:00:00Z"),
    }),
    productInfo({
      id: CARD_LIDER_ID,
      kind: "credit_card",
      slug: "tarjeta",
      institutionSlug: "bci_lider",
      metrics: { kind: "credit_card", available: 500000, owed: 200000 },
      balanceAsOf: new Date("2026-07-13T12:00:00Z"),
    }),
    productInfo({
      id: FUND_ID,
      kind: "investment",
      slug: "fondo_a",
      institutionSlug: "fintual",
      currency: "USD",
      metrics: { kind: "investment", nav: 1000, var_daily_pct: 2.5 },
    }),
    productInfo({
      id: STOCK_ID,
      kind: "stock",
      slug: "acme",
      institutionSlug: "fintual",
      currency: "USD",
      metrics: { kind: "stock", nav: 250, units: 2.5, unit_price: 100 },
    }),
    productInfo({
      id: LOAN_ID,
      kind: "loan",
      slug: "credito",
      institutionSlug: "banchile",
      currency: "USD",
      metrics: { kind: "loan", owed: 1000, installments_paid: 12 },
    }),
    productInfo({
      id: EUR_WALLET_ID,
      kind: "wallet",
      slug: "billetera_eur",
      institutionSlug: "banchile",
      currency: "EUR",
      metrics: { kind: "wallet", balance: 100000 },
    }),
    productInfo({
      id: CLOSED_ID,
      kind: "credit_card",
      slug: "tarjeta_cerrada",
      institutionSlug: "banchile",
      isActive: false,
      metrics: { kind: "credit_card", available: 100000, owed: 100000 },
    }),
  ];
  return new Map(list.map((p) => [p.id, p]));
}

const products = buildProducts(2500000);

const catalog: ProductCatalog = {
  byId: products,
  bySlug: new Map(
    [...products.values()].map((p) => [`${p.institutionSlug}:${p.slug}`, p])
  ),
};

function ctx(overrides: Partial<EvalContext> = {}): EvalContext {
  return {
    date: new Date(2026, 6, 15), // 2026-07-15, local
    products,
    rates,
    currency: "CLP",
    ...overrides,
  };
}

/** Parse (display or stored form), bind, evaluate. */
function run(source: string, context: EvalContext = ctx()) {
  return evaluateExpression(bindExpression(parseExpression(source), catalog), context);
}

function value(source: string, context: EvalContext = ctx()): number {
  const result = run(source, context);
  if (!result.ok) throw new Error(`expected ok, got: ${result.reason}`);
  return result.value;
}

function reason(source: string, context: EvalContext = ctx()): string {
  const result = run(source, context);
  if (result.ok) throw new Error(`expected no-data, got value ${result.value}`);
  return result.reason;
}

describe("evaluateExpression arithmetic", () => {
  it("computes literals, precedence, parens, and unary minus", () => {
    expect(value("1 + 2 * 3")).toBe(7);
    expect(value("(1 + 2) * 3")).toBe(9);
    expect(value("-5 + 2")).toBe(-3);
    expect(value("10 / 4")).toBe(2.5);
    expect(value("100 / 5 / 2")).toBe(10); // division is left-associative
    expect(value("-(2 - 5)")).toBe(3);
  });

  it("division by zero is no-data, even transitively", () => {
    expect(reason("1 / 0")).toBe("División por cero");
    expect(reason("100 / (2 - 2)")).toBe("División por cero");
  });
});

describe("reference resolution and conversion", () => {
  it("reads metric fields in the monitor currency", () => {
    expect(value("banchile:cuenta_corriente:balance")).toBe(2500000);
    expect(value("banchile:tarjeta_clp:owed")).toBe(300000);
  });

  it("converts a USD product into a CLP monitor", () => {
    expect(value("banchile:tarjeta_usd:owed")).toBe(90000); // 100 * 900 / 1
  });

  it("does NOT convert percent fields", () => {
    expect(value("fintual:fondo_a:var_daily_pct")).toBe(2.5);
  });

  it("does NOT convert count fields", () => {
    expect(value("banchile:credito:installments_paid")).toBe(12);
  });

  it("reads a stock position's per-unit fields", () => {
    expect(value("fintual:acme:unit_price")).toBe(90000); // 100 * 900 / 1
    expect(value("fintual:acme:units")).toBe(2.5);
    expect(value("fintual:acme:nav")).toBe(225000);
  });

  it("converts across a cross rate into a non-CLP monitor currency", () => {
    const usdCtx = ctx({ currency: "USD" });
    // 2500000 CLP * 1 / 900 CLP-per-USD
    expect(value("banchile:cuenta_corriente:balance", usdCtx)).toBeCloseTo(
      2500000 / 900,
      6
    );
    // USD product into USD monitor: no conversion.
    expect(value("banchile:tarjeta_usd:owed", usdCtx)).toBe(100);
  });

  it("converts with the referenced product's own institution rate", () => {
    const override = {
      institutionId: "00000000-0000-4000-8000-0000000000b1",
      institutionSlug: "banchile",
      currency: "USD",
      source: "Fuente de prueba",
      reportedAsOf: null,
      observedAt: new Date("2026-07-15T12:00:00Z"),
    };
    const overridden = ctx({
      rates: {
        provider: rates.provider,
        overrides: [
          { ...override, productId: null, rate: 950 },
          { ...override, productId: CARD_USD_ID, rate: 999.99 },
        ],
      },
    });
    // Product rate wins: 100 USD * 999.99.
    expect(value("banchile:tarjeta_usd:owed", overridden)).toBeCloseTo(99999, 6);
    // Institution rate for its other USD products: 1000 USD * 950.
    expect(value("banchile:credito:owed", overridden)).toBe(950000);
    // Another institution keeps the provider rate: 1000 USD * 900.
    expect(value("fintual:fondo_a:nav", overridden)).toBe(900000);
    // The CLP-to-USD leg of a banchile product uses banchile's rate too.
    expect(
      value("banchile:cuenta_corriente:balance", { ...overridden, currency: "USD" })
    ).toBeCloseTo(2500000 / 950, 6);
  });

  it("same product and monitor currency needs no rate", () => {
    // EUR has no rate, but an EUR monitor reads EUR products directly.
    expect(value("banchile:billetera_eur:balance", ctx({ currency: "EUR" }))).toBe(
      100000
    );
  });

  it("missing product rate is no-data and names the reference", () => {
    expect(reason("banchile:billetera_eur:balance")).toContain(
      "No hay tipo de cambio para EUR"
    );
    expect(reason("banchile:billetera_eur:balance")).toContain(
      "banchile:billetera_eur:balance"
    );
  });

  it("missing monitor-currency rate is no-data", () => {
    expect(
      reason("banchile:cuenta_corriente:balance", ctx({ currency: "GBP" }))
    ).toContain("moneda del monitor GBP");
  });

  it("unbound reference is no-data", () => {
    // Parsed but never bound: the ref still carries only its slugs.
    const result = evaluateExpression(
      parseExpression("banchile:cuenta_corriente:balance"),
      ctx()
    );
    expect(result).toEqual({
      ok: false,
      reason:
        "La referencia banchile:cuenta_corriente:balance no está asociada a un producto",
    });
  });

  it("unknown product id is no-data", () => {
    expect(reason(`@{${UNKNOWN_ID}:owed}`)).toBe(
      `La referencia @{${UNKNOWN_ID}:owed} apunta a un producto que ya no existe`
    );
  });

  it("inactive product is no-data", () => {
    expect(reason("banchile:tarjeta_cerrada:owed")).toBe(
      "El producto banchile:tarjeta_cerrada:owed está inactivo"
    );
  });

  it("field not valid for the product's kind is no-data", () => {
    expect(reason(`@{${CHECKING_ID}:owed}`)).toBe(
      "El campo 'owed' no es válido para productos de tipo 'Cuenta corriente'" +
        " (referencia banchile:cuenta_corriente:owed)"
    );
  });

  it("field absent from the metrics payload is no-data", () => {
    // tarjeta_clp has no 'limit' in its metrics (valid for the kind, unreported).
    expect(reason("banchile:tarjeta_clp:limit")).toBe(
      "Falta el campo 'limit' en banchile:tarjeta_clp:limit"
    );
  });

  it("null metrics is no-data", () => {
    const withNullMetrics = buildProducts(2500000);
    withNullMetrics.get(CARD_CLP_ID)!.metrics = null;
    expect(
      reason("banchile:tarjeta_clp:owed", ctx({ products: withNullMetrics }))
    ).toBe(
      "Todavía no hay datos para banchile:tarjeta_clp:owed (actualiza su institución)"
    );
  });

  it("null metrics on the headline field is no-data", () => {
    const withNullMetrics = buildProducts(2500000);
    withNullMetrics.get(CHECKING_ID)!.metrics = null;
    expect(
      reason(
        "banchile:cuenta_corriente:balance",
        ctx({ products: withNullMetrics })
      )
    ).toContain("Todavía no hay datos");
  });

  it("a no-data reference poisons the whole expression (never a silent 0)", () => {
    expect(
      reason("banchile:cuenta_corriente:balance - banchile:tarjeta_cerrada:owed")
    ).toContain("está inactivo");
  });
});

describe("date helpers", () => {
  it("DAY_OF_MONTH and DAYS_IN_MONTH use the context's local date", () => {
    expect(value("DAY_OF_MONTH()", ctx({ date: new Date(2026, 6, 15) }))).toBe(15);
    expect(value("DAYS_IN_MONTH()", ctx({ date: new Date(2026, 6, 15) }))).toBe(31);
  });

  it("handles February, leap and non-leap", () => {
    expect(value("DAYS_IN_MONTH()", ctx({ date: new Date(2026, 1, 10) }))).toBe(28);
    expect(value("DAYS_IN_MONTH()", ctx({ date: new Date(2028, 1, 10) }))).toBe(29);
  });
});

describe("comparatorHolds", () => {
  it("implements every comparator", () => {
    expect(comparatorHolds("<", 1, 2)).toBe(true);
    expect(comparatorHolds("<", 2, 2)).toBe(false);
    expect(comparatorHolds("<=", 2, 2)).toBe(true);
    expect(comparatorHolds(">", 3, 2)).toBe(true);
    expect(comparatorHolds(">", 2, 2)).toBe(false);
    expect(comparatorHolds(">=", 2, 2)).toBe(true);
    expect(comparatorHolds("=", 2, 2)).toBe(true);
    expect(comparatorHolds("=", 2, 3)).toBe(false);
    expect(comparatorHolds("!=", 2, 3)).toBe(true);
    expect(comparatorHolds("!=", 2, 2)).toBe(false);
  });
});

// The issue #41 end-to-end example (synthetic figures): checking balance
// minus two card oweds (one in USD) minus 70% of another card's owed,
// against a monthly ramp that starts at 1000000 and drops 30000 per day.
const EXAMPLE_LEFT =
  "banchile:cuenta_corriente:balance" +
  " - banchile:tarjeta_clp:owed" +
  " - banchile:tarjeta_usd:owed" +
  " - bci_lider:tarjeta:owed * 0.7";

function exampleDef(): MonitorDefinition {
  const bound = bindExpression(parseExpression(EXAMPLE_LEFT), catalog);
  return {
    currency: "CLP",
    expression: serializeExpression(bound, "uuid"),
    thresholds: [
      {
        severity: "alert",
        comparator: "<",
        expression: "1000000 - 30000 * (DAY_OF_MONTH() - 1)",
      },
    ],
  };
}

describe("evaluateMonitor: issue #41 example", () => {
  it("computes value, threshold, margin, and status on 2026-07-15", () => {
    const result = evaluateMonitor(exampleDef(), ctx());
    // 2500000 - 300000 - (100 USD -> 90000) - 200000 * 0.7 = 1970000
    expect(result.value).toBe(1970000);
    // Day 15: 1000000 - 30000 * 14 = 580000
    expect(result.thresholds).toEqual([
      { severity: "alert", comparator: "<", value: 580000, margin: 1390000, adjustment: 0 },
    ]);
    expect(result.margin).toBe(1390000);
    expect(result.status).toBe("ok");
    // Oldest observation among the four referenced products.
    expect(result.staleAsOf).toBe("2026-07-13T12:00:00.000Z");
    expect(result.noDataReason).toBeNull();
  });

  it("transitions breached -> ok along the ramp, and resets monthly", () => {
    // Poorer checking balance: 930000 - 300000 - 90000 - 140000 = 400000.
    const poor = ctx({ products: buildProducts(930000) });
    const def = exampleDef();

    // Day 15: threshold 580000; 400000 < 580000 -> breached.
    const day15 = evaluateMonitor(def, { ...poor, date: new Date(2026, 6, 15) });
    expect(day15.value).toBe(400000);
    expect(day15.status).toBe("breached");
    expect(day15.margin).toBe(400000 - 580000);

    // Day 22: threshold 1000000 - 30000 * 21 = 370000; 400000 >= it -> ok.
    const day22 = evaluateMonitor(def, { ...poor, date: new Date(2026, 6, 22) });
    expect(day22.status).toBe("ok");
    expect(day22.thresholds[0].value).toBe(370000);
    expect(day22.margin).toBe(30000);

    // Aug 1: DAY_OF_MONTH resets, threshold back to 1000000 -> breached again.
    const aug1 = evaluateMonitor(def, { ...poor, date: new Date(2026, 7, 1) });
    expect(aug1.thresholds[0].value).toBe(1000000);
    expect(aug1.status).toBe("breached");
    expect(aug1.margin).toBe(-600000);
  });
});

describe("evaluateMonitor: status precedence and margins", () => {
  const left = "banchile:cuenta_corriente:balance"; // 2500000

  function def(
    thresholds: MonitorDefinition["thresholds"]
  ): MonitorDefinition {
    return {
      currency: "CLP",
      expression: serializeExpression(
        bindExpression(parseExpression(left), catalog),
        "uuid"
      ),
      thresholds,
    };
  }

  it("no_data beats breached: a broken threshold hides a firing alert", () => {
    const result = evaluateMonitor(
      def([
        { severity: "alert", comparator: "<", expression: "9999999" }, // would fire
        { severity: "warning", comparator: "<", expression: `@{${UNKNOWN_ID}:owed}` },
      ]),
      ctx()
    );
    expect(result.status).toBe("no_data");
    expect(result.noDataReason).toContain("ya no existe");
    expect(result.value).toBe(2500000);
    expect(result.thresholds[1].value).toBeNull();
    expect(result.margin).toBeNull();
  });

  it("no_data when the left side itself cannot be computed", () => {
    const broken = def([
      { severity: "alert", comparator: "<", expression: "1000000" },
    ]);
    broken.expression = `@{${UNKNOWN_ID}:owed}`;
    const result = evaluateMonitor(broken, ctx());
    expect(result.status).toBe("no_data");
    expect(result.value).toBeNull();
    expect(result.thresholds[0].value).toBe(1000000); // still computed
    expect(result.thresholds[0].margin).toBeNull();
  });

  it("breached beats warning when both conditions hold", () => {
    const result = evaluateMonitor(
      def([
        { severity: "alert", comparator: "<", expression: "9999999" },
        { severity: "warning", comparator: "<", expression: "9999999" },
      ]),
      ctx()
    );
    expect(result.status).toBe("breached");
  });

  it("warning beats ok when only the warning condition holds", () => {
    const result = evaluateMonitor(
      def([
        { severity: "alert", comparator: "<", expression: "1000000" },
        { severity: "warning", comparator: "<", expression: "2600000" },
      ]),
      ctx()
    );
    expect(result.status).toBe("warning");
    // Monitor margin is the minimum across thresholds (nearest to crossing).
    expect(result.thresholds[0].margin).toBe(1500000);
    expect(result.thresholds[1].margin).toBe(-100000);
    expect(result.margin).toBe(-100000);
  });

  it("margin is null for = and !=", () => {
    const equal = evaluateMonitor(
      def([{ severity: "alert", comparator: "=", expression: "2500000" }]),
      ctx()
    );
    expect(equal.status).toBe("breached"); // exact match fires
    expect(equal.thresholds[0].margin).toBeNull();
    expect(equal.margin).toBeNull();

    const notEqual = evaluateMonitor(
      def([{ severity: "alert", comparator: "!=", expression: "2500000" }]),
      ctx()
    );
    expect(notEqual.status).toBe("ok");
    expect(notEqual.margin).toBeNull();
  });

  it("greater-than margins measure the distance up to the threshold", () => {
    const result = evaluateMonitor(
      def([{ severity: "alert", comparator: ">", expression: "3000000" }]),
      ctx()
    );
    expect(result.status).toBe("ok");
    expect(result.margin).toBe(500000); // 3000000 - 2500000
  });

  it("an unparseable stored expression degrades to no_data", () => {
    const result = evaluateMonitor(
      { currency: "CLP", expression: "1 +", thresholds: [] },
      ctx()
    );
    expect(result.status).toBe("no_data");
    expect(result.noDataReason).toMatch(/^Expresión inválida: /);
  });

  it("an unparseable stored threshold degrades to no_data", () => {
    const result = evaluateMonitor(
      def([{ severity: "alert", comparator: "<", expression: "1 +" }]),
      ctx()
    );
    expect(result.status).toBe("no_data");
    expect(result.noDataReason).toMatch(/^Umbral inválido: /);
    expect(result.value).toBe(2500000);
  });
});

describe("variaciones: thresholdShift and adjustmentOnDate", () => {
  // Synthetic transactions on the fixture products (see buildProducts):
  // checking balance 2500000 CLP, CLP card owed 300000, USD card owed 100.
  const CHECKING = `@{${CHECKING_ID}:balance}`;
  const CARD_OWED = `@{${CARD_CLP_ID}:owed}`;
  const USD_OWED = `@{${CARD_USD_ID}:owed}`;

  function txn(
    productId: string,
    transactionDate: string,
    amount: number,
    extra: Partial<MonitorAdjustment> = {}
  ): MonitorAdjustment {
    return {
      transactionId: `${transactionDate}:${amount}`,
      productId,
      transactionDate,
      amount,
      ...extra,
    };
  }

  /** The shift of a constant threshold, or null when it cannot be computed. */
  const shift = (
    expression: string,
    adjustments: MonitorAdjustment[],
    context: EvalContext = ctx()
  ) => {
    const result = thresholdShift(
      parseExpression(expression),
      null,
      adjustments,
      context
    );
    return result.ok ? result.value : null;
  };

  it("ignoring a checking expense lowers the thresholds by it", () => {
    // Undo -1000000 on balance: f rises by 1000000, thresholds drop by it.
    expect(shift(CHECKING, [txn(CHECKING_ID, "2026-07-10", -1000000)])).toBe(
      -1000000
    );
    // An ignored income raises them instead.
    expect(shift(CHECKING, [txn(CHECKING_ID, "2026-07-10", 999999)])).toBe(
      999999
    );
  });

  it("ignoring a card purchase undoes it on owed, through the sign", () => {
    // -owed: undoing a -1000000 purchase lowers owed, so -owed rises.
    expect(
      shift(`${CHECKING} - ${CARD_OWED}`, [
        txn(CARD_CLP_ID, "2026-07-10", -1000000),
      ])
    ).toBe(-1000000);
    // +owed reads the other way.
    expect(
      shift(CARD_OWED, [txn(CARD_CLP_ID, "2026-07-10", -1000000)])
    ).toBe(1000000);
  });

  it("follows coefficients exactly", () => {
    expect(
      shift(`${CHECKING} - ${CARD_OWED} * 0.7`, [
        txn(CARD_CLP_ID, "2026-07-10", -1000000),
      ])
    ).toBe(-700000);
  });

  it("converts a USD product's undo at the provider rate", () => {
    // owed 100 USD at 900: a -1234.56 USD purchase undone moves -owed by
    // 1234.56 * 900 CLP.
    expect(
      shift(`- ${USD_OWED}`, [txn(CARD_USD_ID, "2026-07-10", -1234.56)])
    ).toBe(-1111104);
  });

  it("converts at the product's own override rate when it has one", () => {
    const overridden: RateBook = {
      ...rates,
      overrides: [
        {
          institutionId: "00000000-0000-4000-8000-000000000000",
          institutionSlug: "banchile",
          productId: CARD_USD_ID,
          currency: "USD",
          rate: 1000,
          source: "Tipo de cambio de prueba",
          reportedAsOf: null,
          observedAt: new Date("2026-07-15T12:00:00Z"),
        },
      ],
    };
    expect(
      shift(
        `- ${USD_OWED}`,
        [txn(CARD_USD_ID, "2026-07-10", -1234.56)],
        ctx({ rates: overridden })
      )
    ).toBe(-1234560);
  });

  it("is 0 for a product or field the expression does not read", () => {
    expect(shift(CHECKING, [txn(CARD_CLP_ID, "2026-07-10", -1000000)])).toBe(0);
    expect(
      shift(`@{${LOAN_ID}:installments_paid}`, [txn(LOAN_ID, "2026-07-10", 999)])
    ).toBe(0);
  });

  it("is null when the expression has no data, and never throws", () => {
    expect(
      shift(`${CHECKING} + @{${UNKNOWN_ID}:balance}`, [
        txn(CHECKING_ID, "2026-07-10", -1000000),
      ])
    ).toBeNull();
  });

  it("skips deleted transactions", () => {
    expect(
      shift(CHECKING, [
        txn(CHECKING_ID, "2026-07-10", -1000000, { deleted: true }),
      ])
    ).toBe(0);
  });

  it("undoes refs on the threshold side too", () => {
    // balance < owed * 2: ignoring a -1000000 card purchase lowers owed by
    // 1000000, so the threshold falls by 2000000 (shift = +dT - dL).
    const result = thresholdShift(
      parseExpression(CHECKING),
      parseExpression(`${CARD_OWED} * 2`),
      [txn(CARD_CLP_ID, "2026-07-10", -1000000)],
      ctx()
    );
    expect(result).toEqual({ ok: true, value: -2000000 });
    // The same product on both sides with the same weight cancels out.
    expect(
      thresholdShift(
        parseExpression(`${CHECKING} - ${CARD_OWED}`),
        parseExpression(`1000000 - ${CARD_OWED}`),
        [txn(CARD_CLP_ID, "2026-07-10", -1000000)],
        ctx()
      )
    ).toEqual({ ok: true, value: 0 });
  });

  it("gives a reason when only the undo breaks the expression", () => {
    // owed is 300000: undoing a -300000 purchase leaves it at 0.
    const result = thresholdShift(
      parseExpression(`${CHECKING} / ${CARD_OWED}`),
      null,
      [txn(CARD_CLP_ID, "2026-07-10", -300000)],
      ctx()
    );
    expect(result).toEqual({
      ok: false,
      reason: "No se pudieron aplicar las variaciones: División por cero",
    });
  });

  describe("adjustmentOnDate", () => {
    const adjustments = [
      txn(CHECKING_ID, "2025-07-10", -999999),
      txn(CHECKING_ID, "2026-06-30", -999999),
      txn(CHECKING_ID, "2026-07-01", 30000),
      txn(CHECKING_ID, "2026-07-15", -50000),
      txn(CHECKING_ID, "2026-07-15", 10000),
      txn(CHECKING_ID, "2026-07-31", 1000000),
      txn(CHECKING_ID, "2026-07-20", -1000000, { deleted: true }),
    ];
    const expr = parseExpression(CHECKING);
    const on = (date: Date) =>
      adjustmentOnDate(adjustments, expr, null, ctx({ date }));

    it.each([
      ["before any variación of the month", new Date(2026, 5, 29), 0],
      ["on the last day of the previous month", new Date(2026, 5, 30), -999999],
      ["from the first day, without the previous month", new Date(2026, 6, 1), 30000],
      ["the day before a same-day pair", new Date(2026, 6, 14), 30000],
      ["on the day of a same-day pair, together", new Date(2026, 6, 15), -10000],
      ["past a deleted one, which never applies", new Date(2026, 6, 25), -10000],
      ["on the last day of the month", new Date(2026, 6, 31), 990000],
      // Evenings whose UTC date is already the next month still count locally.
      ["late on the month's last evening", new Date(2026, 6, 31, 23, 30), 990000],
      ["after the month ends", new Date(2026, 7, 1), 0],
    ])("%s", (_label, date, expected) => {
      expect(on(date)).toEqual({ ok: true, value: expected });
    });

    it("is 0 without variaciones", () => {
      const zero = { ok: true, value: 0 };
      expect(adjustmentOnDate(undefined, expr, null, ctx())).toEqual(zero);
      expect(adjustmentOnDate([], expr, null, ctx())).toEqual(zero);
    });

    it("lists only the ones in effect", () => {
      expect(
        adjustmentsInEffect(adjustments, new Date(2026, 6, 15)).map(
          (a) => a.transactionDate
        )
      ).toEqual(["2026-07-01", "2026-07-15", "2026-07-15"]);
    });
  });
});

describe("evaluateMonitor: variaciones", () => {
  const left = "banchile:cuenta_corriente:balance"; // 2500000

  function def(adjustments: MonitorAdjustment[]): MonitorDefinition {
    return {
      currency: "CLP",
      expression: serializeExpression(
        bindExpression(parseExpression(left), catalog),
        "uuid"
      ),
      thresholds: [
        { severity: "alert", comparator: "<", expression: "2000000" },
        { severity: "warning", comparator: "<", expression: "2400000" },
      ],
      adjustments,
    };
  }

  const expense = (transactionDate: string, amount: number) => ({
    transactionId: transactionDate,
    productId: CHECKING_ID,
    transactionDate,
    amount,
  });

  it("shifts every threshold and its margin; the value stays real", () => {
    const result = evaluateMonitor(def([expense("2026-07-10", -50000)]), ctx());
    expect(result.value).toBe(2500000);
    expect(result.adjustment).toBe(-50000);
    expect(result.thresholds).toEqual([
      {
        severity: "alert",
        comparator: "<",
        value: 1950000,
        margin: 550000,
        adjustment: -50000,
      },
      {
        severity: "warning",
        comparator: "<",
        value: 2350000,
        margin: 150000,
        adjustment: -50000,
      },
    ]);
    expect(result.margin).toBe(150000);
  });

  it("flips the status once an ignored income is taken out", () => {
    const result = evaluateMonitor(
      def([expense("2026-07-15", 100000), expense("2026-07-15", 50000)]),
      ctx()
    );
    expect(result.adjustment).toBe(150000);
    expect(result.status).toBe("warning"); // 2500000 < 2400000 + 150000
  });

  it("ignores transactions dated later in the month or in another month", () => {
    const result = evaluateMonitor(
      def([expense("2026-07-16", -500000), expense("2026-06-01", -500000)]),
      ctx()
    );
    expect(result.adjustment).toBe(0);
    expect(result.thresholds.map((t) => t.value)).toEqual([2000000, 2400000]);
  });

  it("is re-evaluated against the expression it is given", () => {
    const result = evaluateMonitor(
      {
        ...def([expense("2026-07-10", -50000)]),
        expression: `@{${CARD_CLP_ID}:owed}`,
      },
      ctx()
    );
    expect(result.adjustment).toBe(0);
  });

  it("shifts nothing when the value has no data", () => {
    const result = evaluateMonitor(
      {
        ...def([expense("2026-07-01", -50000)]),
        expression: `@{${CHECKING_ID}:balance} + @{${UNKNOWN_ID}:balance}`,
      },
      ctx()
    );
    expect(result.status).toBe("no_data");
    expect(result.adjustment).toBe(0);
    expect(result.thresholds[0].value).toBe(2000000);
  });

  it("shifts each threshold by its own refs", () => {
    const result = evaluateMonitor(
      {
        ...def([
          {
            transactionId: "card",
            productId: CARD_CLP_ID,
            transactionDate: "2026-07-10",
            amount: -100000,
          },
        ]),
        thresholds: [
          { severity: "warning", comparator: "<", expression: "2400000" },
          {
            severity: "alert",
            comparator: "<",
            expression: `@{${CARD_CLP_ID}:owed} * 8`,
          },
        ],
      },
      ctx()
    );
    // owed 300000 -> 200000 once undone: the alert threshold 2400000 falls
    // by 800000; the constant warning one does not move.
    expect(result.thresholds.map((t) => [t.value, t.adjustment])).toEqual([
      [2400000, 0],
      [1600000, -800000],
    ]);
    // The monitor-level shift reports the alert threshold.
    expect(result.adjustment).toBe(-800000);
    expect(result.status).toBe("ok");
  });

  it("is no_data with a reason when the undo breaks a threshold", () => {
    const result = evaluateMonitor(
      {
        ...def([
          {
            transactionId: "card",
            productId: CARD_CLP_ID,
            transactionDate: "2026-07-10",
            amount: -300000,
          },
        ]),
        thresholds: [
          {
            severity: "alert",
            comparator: "<",
            expression: `1000000000 / @{${CARD_CLP_ID}:owed}`,
          },
        ],
      },
      ctx()
    );
    expect(result.status).toBe("no_data");
    expect(result.noDataReason).toBe(
      "No se pudieron aplicar las variaciones: División por cero"
    );
    expect(result.thresholds[0].value).toBeNull();
  });
});
