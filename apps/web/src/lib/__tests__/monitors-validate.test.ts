import { describe, it, expect } from "vitest";
import type { CatalogEntry, ProductCatalog } from "@/lib/monitors";
import { validateMonitorInput } from "@/lib/monitors/validate";

// All figures and identifiers below are synthetic (see the repo's personal
// data policy): fake uuids, fake CLP amounts, fake slugs.
const CHECKING_ID = "11111111-1111-4111-8111-111111111111";
const CARD_ID = "22222222-2222-4222-8222-222222222222";
const UNKNOWN_ID = "99999999-9999-4999-8999-999999999999";

const entries: CatalogEntry[] = [
  {
    id: CHECKING_ID,
    kind: "checking",
    slug: "cuenta_corriente",
    institutionSlug: "banchile",
  },
  {
    id: CARD_ID,
    kind: "credit_card",
    slug: "tarjeta_clp",
    institutionSlug: "banchile",
  },
];

const catalog: ProductCatalog = {
  byId: new Map(entries.map((e) => [e.id, e])),
  bySlug: new Map(entries.map((e) => [`${e.institutionSlug}:${e.slug}`, e])),
};

/** A valid create body in display syntax; tests override single fields. */
function createBody(overrides: Record<string, unknown> = {}) {
  return {
    name: "Colchon de la cuenta",
    expression: "banchile:cuenta_corriente:balance",
    thresholds: [
      {
        severity: "alert",
        comparator: "<",
        expression: "1000000 - 30000 * (DAY_OF_MONTH() - 1)",
      },
    ],
    ...overrides,
  };
}

function expectFailure(result: ReturnType<typeof validateMonitorInput>) {
  if (result.ok) throw new Error("expected a validation failure");
  return result;
}

describe("validateMonitorInput (create)", () => {
  it("accepts display-form input, normalizes to stored form, applies defaults", () => {
    const result = validateMonitorInput(createBody(), catalog);
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.value).toEqual({
      name: "Colchon de la cuenta",
      description: null,
      currency: "CLP",
      expression: `@{${CHECKING_ID}:balance}`,
      thresholds: [
        {
          severity: "alert",
          comparator: "<",
          expression: "1000000 - 30000 * (DAY_OF_MONTH() - 1)",
        },
      ],
      display: { chart: "line", show_margin: true },
      isActive: true,
    });
  });

  it("accepts stored (uuid-ref) form input untouched", () => {
    const result = validateMonitorInput(
      createBody({
        expression: `@{${CARD_ID}:owed} + @{${CHECKING_ID}:current_balance}`,
      }),
      catalog
    );
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.value.expression).toBe(
      `@{${CARD_ID}:owed} + @{${CHECKING_ID}:current_balance}`
    );
  });

  it("keeps explicit fields: description, currency, display, isActive", () => {
    const result = validateMonitorInput(
      createBody({
        description: "Margen antes del sueldo",
        currency: "usd",
        display: { chart: "stat", show_margin: false },
        isActive: false,
        thresholds: [
          { severity: "alert", comparator: ">=", expression: "999999" },
          { severity: "warning", comparator: ">=", expression: "2500000" },
        ],
      }),
      catalog
    );
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.value.description).toBe("Margen antes del sueldo");
    expect(result.value.currency).toBe("USD");
    expect(result.value.display).toEqual({ chart: "stat", show_margin: false });
    expect(result.value.isActive).toBe(false);
    expect(result.value.thresholds).toHaveLength(2);
  });

  it("fills display defaults per key", () => {
    const result = validateMonitorInput(
      createBody({ display: { chart: "stat" } }),
      catalog
    );
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.value.display).toEqual({ chart: "stat", show_margin: true });
  });

  it("rejects a non-object body", () => {
    for (const body of [null, undefined, "monitor", 999999, ["monitor"]]) {
      const failure = expectFailure(validateMonitorInput(body, catalog));
      expect(failure.status).toBe(400);
      expect(failure.error).toBe("Request body must be a JSON object");
    }
  });

  it("rejects a missing or empty name", () => {
    const missing = expectFailure(
      validateMonitorInput(createBody({ name: undefined }), catalog)
    );
    expect(missing.field).toBe("name");
    const empty = expectFailure(
      validateMonitorInput(createBody({ name: "   " }), catalog)
    );
    expect(empty.field).toBe("name");
  });

  it("rejects an invalid currency", () => {
    const failure = expectFailure(
      validateMonitorInput(createBody({ currency: "C$" }), catalog)
    );
    expect(failure.field).toBe("currency");
  });

  it("reports parse errors with the offending position", () => {
    // Index:                                      0123456789A
    const failure = expectFailure(
      validateMonitorInput(createBody({ expression: "1000000 + )" }), catalog)
    );
    expect(failure.field).toBe("expression");
    expect(failure.error).toBe("Unexpected character ')'");
    expect(failure.position).toBe(10);
  });

  it("rejects an unknown institution:product slug pair, with position", () => {
    const failure = expectFailure(
      validateMonitorInput(
        createBody({ expression: "2500000 - acme:nada:balance" }),
        catalog
      )
    );
    expect(failure.field).toBe("expression");
    expect(failure.error).toBe("Unknown product 'acme:nada'");
    expect(failure.position).toBe(10);
  });

  it("rejects an unknown product uuid", () => {
    const failure = expectFailure(
      validateMonitorInput(
        createBody({ expression: `@{${UNKNOWN_ID}:balance}` }),
        catalog
      )
    );
    expect(failure.field).toBe("expression");
    expect(failure.error).toBe(`Unknown product '${UNKNOWN_ID}'`);
    expect(failure.position).toBe(0);
  });

  it("rejects a field that is invalid for the product's kind", () => {
    const failure = expectFailure(
      validateMonitorInput(
        createBody({ expression: "banchile:cuenta_corriente:owed" }),
        catalog
      )
    );
    expect(failure.field).toBe("expression");
    expect(failure.error).toBe("Field 'owed' is not valid for kind 'checking'");
    expect(failure.position).toBe(0);
  });

  it("rejects missing or empty thresholds", () => {
    for (const thresholds of [undefined, []]) {
      const failure = expectFailure(
        validateMonitorInput(createBody({ thresholds }), catalog)
      );
      expect(failure.field).toBe("thresholds");
      expect(failure.error).toBe("At least one threshold is required");
    }
  });

  it("reports threshold expression errors under thresholds[i].expression", () => {
    const failure = expectFailure(
      validateMonitorInput(
        createBody({
          thresholds: [
            { severity: "alert", comparator: "<", expression: "((" },
          ],
        }),
        catalog
      )
    );
    expect(failure.field).toBe("thresholds[0].expression");
    expect(failure.error).toBe("Unexpected end of input");
    expect(failure.position).toBe(2);
  });

  it("rejects duplicate severities", () => {
    const failure = expectFailure(
      validateMonitorInput(
        createBody({
          thresholds: [
            { severity: "alert", comparator: "<", expression: "1000000" },
            { severity: "alert", comparator: "<", expression: "999999" },
          ],
        }),
        catalog
      )
    );
    expect(failure.field).toBe("thresholds");
    expect(failure.error).toBe("Duplicate 'alert' threshold");
  });

  it("requires an alert threshold", () => {
    const failure = expectFailure(
      validateMonitorInput(
        createBody({
          thresholds: [
            { severity: "warning", comparator: "<", expression: "1000000" },
          ],
        }),
        catalog
      )
    );
    expect(failure.field).toBe("thresholds");
    expect(failure.error).toBe("An 'alert' threshold is required");
  });

  it("rejects a bad comparator", () => {
    const failure = expectFailure(
      validateMonitorInput(
        createBody({
          thresholds: [
            { severity: "alert", comparator: "<>", expression: "1000000" },
          ],
        }),
        catalog
      )
    );
    expect(failure.field).toBe("thresholds[0].comparator");
  });

  it("rejects a bad severity", () => {
    const failure = expectFailure(
      validateMonitorInput(
        createBody({
          thresholds: [
            { severity: "critical", comparator: "<", expression: "1000000" },
          ],
        }),
        catalog
      )
    );
    expect(failure.field).toBe("thresholds[0].severity");
  });

  it("rejects a bad display config", () => {
    for (const display of [
      "line",
      { chart: "pie" },
      { show_margin: "yes" },
    ]) {
      const failure = expectFailure(
        validateMonitorInput(createBody({ display }), catalog)
      );
      expect(failure.field).toBe("display");
    }
  });

  it("rejects a non-boolean isActive", () => {
    const failure = expectFailure(
      validateMonitorInput(createBody({ isActive: "yes" }), catalog)
    );
    expect(failure.field).toBe("isActive");
  });
});

describe("validateMonitorInput (partial update)", () => {
  it("validates only the fields present", () => {
    const result = validateMonitorInput(
      { name: "Nuevo nombre" },
      catalog,
      { partial: true }
    );
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.value).toEqual({ name: "Nuevo nombre" });
  });

  it("accepts an empty patch", () => {
    const result = validateMonitorInput({}, catalog, { partial: true });
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.value).toEqual({});
  });

  it("normalizes an updated expression to stored form", () => {
    const result = validateMonitorInput(
      { expression: "banchile:tarjeta_clp:owed * 0.7" },
      catalog,
      { partial: true }
    );
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.value).toEqual({
      expression: `@{${CARD_ID}:owed} * 0.7`,
    });
  });

  it("still validates present fields", () => {
    const failure = expectFailure(
      validateMonitorInput({ name: "" }, catalog, { partial: true })
    );
    expect(failure.field).toBe("name");
  });

  it("requires alert when the patch includes thresholds", () => {
    const failure = expectFailure(
      validateMonitorInput(
        {
          thresholds: [
            { severity: "warning", comparator: "<", expression: "1000000" },
          ],
        },
        catalog,
        { partial: true }
      )
    );
    expect(failure.field).toBe("thresholds");
    expect(failure.error).toBe("An 'alert' threshold is required");
  });

  it("accepts a full thresholds replacement that includes alert", () => {
    const result = validateMonitorInput(
      {
        thresholds: [
          { severity: "alert", comparator: "<", expression: "999999" },
          {
            severity: "warning",
            comparator: "<",
            expression: "banchile:cuenta_corriente:balance * 0.5",
          },
        ],
      },
      catalog,
      { partial: true }
    );
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.value.thresholds).toEqual([
      { severity: "alert", comparator: "<", expression: "999999" },
      {
        severity: "warning",
        comparator: "<",
        expression: `@{${CHECKING_ID}:balance} * 0.5`,
      },
    ]);
  });
});
