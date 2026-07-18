import { describe, it, expect } from "vitest";
import {
  bindExpression,
  comparatorHolds,
  evaluateExpression,
  evaluateMonitor,
  parseExpression,
  serializeExpression,
  type EvalContext,
  type MonitorDefinition,
  type ProductCatalog,
  type ProductInfo,
} from "@/lib/monitors";
import type { ClpRates } from "@/lib/rates";

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

const rates: ClpRates = { CLP: 1, USD: 900 };

type Overrides = Partial<ProductInfo> &
  Pick<ProductInfo, "id" | "kind" | "slug" | "institutionSlug">;

function productInfo(overrides: Overrides): ProductInfo {
  return {
    currency: "CLP",
    isActive: true,
    currentBalance: null,
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
      currentBalance: checkingBalance,
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
      currentBalance: 100000,
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
    expect(value("-(2 - 5)")).toBe(3);
  });

  it("division by zero is no-data, even transitively", () => {
    expect(reason("1 / 0")).toBe("Division by zero");
    expect(reason("100 / (2 - 2)")).toBe("Division by zero");
  });
});

describe("reference resolution and conversion", () => {
  it("reads current_balance and metric fields in the monitor currency", () => {
    expect(value("banchile:cuenta_corriente:current_balance")).toBe(2500000);
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

  it("same product and monitor currency needs no rate", () => {
    // EUR has no rate, but an EUR monitor reads EUR products directly.
    expect(value("banchile:billetera_eur:balance", ctx({ currency: "EUR" }))).toBe(
      100000
    );
  });

  it("missing product rate is no-data and names the reference", () => {
    expect(reason("banchile:billetera_eur:balance")).toContain("No rate for EUR");
    expect(reason("banchile:billetera_eur:balance")).toContain(
      "banchile:billetera_eur:balance"
    );
  });

  it("missing monitor-currency rate is no-data", () => {
    expect(
      reason("banchile:cuenta_corriente:balance", ctx({ currency: "GBP" }))
    ).toContain("monitor currency GBP");
  });

  it("unknown product id is no-data", () => {
    expect(reason(`@{${UNKNOWN_ID}:owed}`)).toContain("Unknown product");
  });

  it("inactive product is no-data", () => {
    expect(reason("banchile:tarjeta_cerrada:owed")).toContain("inactive");
  });

  it("field absent from the metrics payload is no-data", () => {
    // tarjeta_clp has no 'limit' in its metrics (valid for the kind, unreported).
    expect(reason("banchile:tarjeta_clp:limit")).toContain("'limit' is missing");
  });

  it("null metrics is no-data", () => {
    const withNullMetrics = buildProducts(2500000);
    withNullMetrics.get(CARD_CLP_ID)!.metrics = null;
    expect(
      reason("banchile:tarjeta_clp:owed", ctx({ products: withNullMetrics }))
    ).toContain("has no metrics");
  });

  it("null current_balance is no-data", () => {
    const withNullBalance = buildProducts(2500000);
    withNullBalance.get(CHECKING_ID)!.currentBalance = null;
    expect(
      reason(
        "banchile:cuenta_corriente:current_balance",
        ctx({ products: withNullBalance })
      )
    ).toContain("no current balance");
  });

  it("a no-data reference poisons the whole expression (never a silent 0)", () => {
    expect(
      reason("banchile:cuenta_corriente:balance - banchile:tarjeta_cerrada:owed")
    ).toContain("inactive");
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
      { severity: "alert", comparator: "<", value: 580000, margin: 1390000 },
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
    expect(result.noDataReason).toContain("Unknown product");
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
    expect(result.noDataReason).toContain("Invalid expression");
  });
});
