import { describe, it, expect } from "vitest";
import {
  ExprError,
  bindExpression,
  collectRefs,
  parseExpression,
  serializeExpression,
  validateExpression,
  type CatalogEntry,
  type Expr,
  type ProductCatalog,
} from "@/lib/monitors";

// Synthetic uuids and slugs only; no real product identifiers.
const CHECKING_ID = "11111111-1111-4111-8111-111111111111";
const CARD_CLP_ID = "22222222-2222-4222-8222-222222222222";
const CARD_USD_ID = "33333333-3333-4333-8333-333333333333";
const CARD_LIDER_ID = "44444444-4444-4444-8444-444444444444";
const UNKNOWN_ID = "99999999-9999-4999-8999-999999999999";

const entries: CatalogEntry[] = [
  { id: CHECKING_ID, kind: "checking", slug: "cuenta_corriente", institutionSlug: "banchile" },
  { id: CARD_CLP_ID, kind: "credit_card", slug: "tarjeta_clp", institutionSlug: "banchile" },
  { id: CARD_USD_ID, kind: "credit_card", slug: "tarjeta_usd", institutionSlug: "banchile" },
  { id: CARD_LIDER_ID, kind: "credit_card", slug: "tarjeta", institutionSlug: "bci_lider" },
];

const catalog: ProductCatalog = {
  byId: new Map(entries.map((e) => [e.id, e])),
  bySlug: new Map(entries.map((e) => [`${e.institutionSlug}:${e.slug}`, e])),
};

function parseError(source: string): ExprError {
  try {
    parseExpression(source);
  } catch (e) {
    if (e instanceof ExprError) return e;
    throw e;
  }
  throw new Error(`expected parse of ${JSON.stringify(source)} to fail`);
}

describe("parseExpression", () => {
  it("parses integer and decimal literals", () => {
    expect(parseExpression("42")).toEqual({ type: "number", value: 42 });
    expect(parseExpression("0.7")).toEqual({ type: "number", value: 0.7 });
  });

  it("gives * precedence over +", () => {
    expect(parseExpression("1 + 2 * 3")).toEqual({
      type: "binary",
      op: "+",
      left: { type: "number", value: 1 },
      right: {
        type: "binary",
        op: "*",
        left: { type: "number", value: 2 },
        right: { type: "number", value: 3 },
      },
    });
  });

  it("parens override precedence", () => {
    expect(parseExpression("(1 + 2) * 3")).toEqual({
      type: "binary",
      op: "*",
      left: {
        type: "binary",
        op: "+",
        left: { type: "number", value: 1 },
        right: { type: "number", value: 2 },
      },
      right: { type: "number", value: 3 },
    });
  });

  it("parses unary minus, including over a factor", () => {
    expect(parseExpression("-5")).toEqual({
      type: "unary",
      op: "-",
      operand: { type: "number", value: 5 },
    });
    // -a * b binds the minus to the factor, not the product.
    const expr = parseExpression("-2 * 3");
    expect(expr).toEqual({
      type: "binary",
      op: "*",
      left: { type: "unary", op: "-", operand: { type: "number", value: 2 } },
      right: { type: "number", value: 3 },
    });
  });

  it("parses uuid refs (stored form), normalizing case", () => {
    const expr = parseExpression(`@{${CHECKING_ID.toUpperCase()}:balance}`);
    expect(expr).toEqual({
      type: "ref",
      productId: CHECKING_ID,
      institutionSlug: null,
      productSlug: null,
      field: "balance",
      position: 0,
    });
  });

  it("parses display slug refs (institution:product:field)", () => {
    expect(parseExpression("banchile:cuenta_corriente:balance")).toEqual({
      type: "ref",
      productId: null,
      institutionSlug: "banchile",
      productSlug: "cuenta_corriente",
      field: "balance",
      position: 0,
    });
  });

  it("parses slug refs starting with a digit and containing dashes", () => {
    expect(parseExpression("24horas:cuenta-vista:balance")).toMatchObject({
      type: "ref",
      institutionSlug: "24horas",
      productSlug: "cuenta-vista",
      field: "balance",
    });
  });

  it("parses the date helper functions", () => {
    expect(parseExpression("DAY_OF_MONTH()")).toEqual({
      type: "func",
      name: "DAY_OF_MONTH",
    });
    expect(parseExpression("DAYS_IN_MONTH()")).toEqual({
      type: "func",
      name: "DAYS_IN_MONTH",
    });
  });

  it("is whitespace-insensitive between tokens", () => {
    expect(parseExpression("  1+2 *   3 ")).toEqual(parseExpression("1 + 2 * 3"));
  });

  it("rejects an unknown function with its position", () => {
    const e = parseError("1 + FOO()");
    expect(e.message).toContain("Unknown function 'FOO'");
    expect(e.position).toBe(4);
  });

  it("rejects a malformed uuid ref with its position", () => {
    const e = parseError("@{not-a-uuid:balance}");
    expect(e.message).toContain("@{uuid:field}");
    expect(e.position).toBe(0);
  });

  it("rejects a two-part slug ref as malformed", () => {
    const e = parseError("banchile:cuenta");
    expect(e.message).toContain("institution:product:field");
    expect(e.position).toBe(0);
  });

  it("rejects a four-part slug ref as malformed", () => {
    const e = parseError("a:b:c:d");
    expect(e.message).toContain("institution:product:field");
    expect(e.position).toBe(0);
  });

  it("rejects an unbalanced paren at the end of input", () => {
    const e = parseError("(1 + 2");
    expect(e.message).toContain("Expected ')'");
    expect(e.position).toBe(6);
  });

  it("rejects trailing garbage with its position", () => {
    const e = parseError("1 + 2)");
    expect(e.message).toContain("Unexpected character ')'");
    expect(e.position).toBe(5);
  });

  it("rejects empty input", () => {
    expect(parseError("").message).toBe("Empty expression");
    expect(parseError("").position).toBe(0);
    expect(parseError("   ").message).toBe("Empty expression");
  });
});

describe("serializeExpression round-trips", () => {
  it("parse -> serialize uuid form is stable", () => {
    const source = `@{${CHECKING_ID}:balance} - 2 * @{${CARD_CLP_ID}:owed}`;
    const expr = parseExpression(source);
    expect(serializeExpression(expr, "uuid")).toBe(source);
  });

  it("keeps necessary parens and drops redundant ones", () => {
    expect(serializeExpression(parseExpression("(1 + 2) * 3"), "uuid")).toBe(
      "(1 + 2) * 3"
    );
    expect(serializeExpression(parseExpression("5 - (3 - 1)"), "uuid")).toBe(
      "5 - (3 - 1)"
    );
    expect(serializeExpression(parseExpression("6 / (3 * 2)"), "uuid")).toBe(
      "6 / (3 * 2)"
    );
    expect(serializeExpression(parseExpression("(1 * 2) + 3"), "uuid")).toBe(
      "1 * 2 + 3"
    );
    expect(serializeExpression(parseExpression("-(1 + 2)"), "uuid")).toBe(
      "-(1 + 2)"
    );
  });

  it("parse display -> bind -> serialize display restores the slugs", () => {
    const source =
      "banchile:cuenta_corriente:balance - banchile:tarjeta_usd:owed * 0.7";
    const bound = bindExpression(parseExpression(source), catalog);
    expect(serializeExpression(bound, "display", catalog)).toBe(source);
    // And the bound form serializes to stored uuid refs.
    expect(serializeExpression(bound, "uuid")).toBe(
      `@{${CHECKING_ID}:balance} - @{${CARD_USD_ID}:owed} * 0.7`
    );
  });

  it("serializes an unresolvable id to the recognizable @{uuid:field} form", () => {
    const expr = parseExpression(`@{${UNKNOWN_ID}:owed} + 1`);
    expect(serializeExpression(expr, "display", catalog)).toBe(
      `@{${UNKNOWN_ID}:owed} + 1`
    );
  });

  it("refuses to serialize an unbound ref to uuid form", () => {
    const expr = parseExpression("1 + banchile:cuenta_corriente:balance");
    try {
      serializeExpression(expr, "uuid");
      throw new Error("expected serialize to fail");
    } catch (e) {
      expect(e).toBeInstanceOf(ExprError);
      expect((e as ExprError).position).toBe(4);
    }
  });
});

describe("bindExpression / collectRefs", () => {
  it("binds slug refs to product uuids and leaves uuid refs alone", () => {
    const expr = parseExpression(
      `banchile:tarjeta_clp:owed + @{${CHECKING_ID}:balance}`
    );
    const bound = bindExpression(expr, catalog);
    const refs = collectRefs(bound);
    expect(refs.map((r) => r.productId)).toEqual([CARD_CLP_ID, CHECKING_ID]);
    expect(refs.map((r) => r.field)).toEqual(["owed", "balance"]);
  });

  it("throws with the ref's position on an unknown institution:product", () => {
    const expr = parseExpression("1 + nope:nada:balance");
    try {
      bindExpression(expr, catalog);
      throw new Error("expected bind to fail");
    } catch (e) {
      expect(e).toBeInstanceOf(ExprError);
      expect((e as ExprError).message).toContain("nope:nada");
      expect((e as ExprError).position).toBe(4);
    }
  });

  it("collects refs in source order", () => {
    const expr = parseExpression(
      "banchile:cuenta_corriente:balance - (banchile:tarjeta_clp:owed + bci_lider:tarjeta:owed)"
    );
    expect(collectRefs(expr).map((r) => `${r.institutionSlug}:${r.productSlug}`)).toEqual([
      "banchile:cuenta_corriente",
      "banchile:tarjeta_clp",
      "bci_lider:tarjeta",
    ]);
  });

  it("collects nothing from literals and functions", () => {
    expect(collectRefs(parseExpression("1 + DAY_OF_MONTH() * 2"))).toEqual([]);
  });
});

describe("validateExpression", () => {
  it("flags an unknown product (slug form) with its position", () => {
    const expr = parseExpression("1 + nope:nada:balance");
    expect(validateExpression(expr, catalog)).toEqual([
      { message: "Unknown product 'nope:nada'", position: 4 },
    ]);
  });

  it("flags an unknown product (uuid form)", () => {
    const expr = parseExpression(`@{${UNKNOWN_ID}:owed}`);
    const issues = validateExpression(expr, catalog);
    expect(issues).toHaveLength(1);
    expect(issues[0].message).toContain(UNKNOWN_ID);
    expect(issues[0].position).toBe(0);
  });

  it("flags a field that is not valid for the product's kind", () => {
    // checking has no 'owed' metric.
    const expr = parseExpression("banchile:cuenta_corriente:owed");
    expect(validateExpression(expr, catalog)).toEqual([
      { message: "Field 'owed' is not valid for kind 'checking'", position: 0 },
    ]);
  });

  it("rejects current_balance: it is not a valid field for any kind", () => {
    const expr = parseExpression("banchile:cuenta_corriente:current_balance");
    expect(validateExpression(expr, catalog)).toEqual([
      {
        message:
          "Field 'current_balance' is not valid for kind 'checking'",
        position: 0,
      },
    ]);
  });

  it("accepts valid metric fields", () => {
    const expr = parseExpression(
      "banchile:tarjeta_clp:owed + banchile:cuenta_corriente:balance"
    );
    expect(validateExpression(expr, catalog)).toEqual([]);
  });

  it("collects multiple issues", () => {
    const expr = parseExpression(
      "nope:nada:balance + banchile:cuenta_corriente:owed"
    );
    const issues = validateExpression(expr, catalog);
    expect(issues).toHaveLength(2);
  });
});

describe("issue #41 example expression", () => {
  const source =
    "banchile:cuenta_corriente:balance" +
    " - banchile:tarjeta_clp:owed" +
    " - banchile:tarjeta_usd:owed" +
    " - bci_lider:tarjeta:owed * 0.7";

  it("parses, validates, binds, and round-trips", () => {
    const expr: Expr = parseExpression(source);
    expect(validateExpression(expr, catalog)).toEqual([]);
    const bound = bindExpression(expr, catalog);
    expect(collectRefs(bound)).toHaveLength(4);
    expect(serializeExpression(bound, "display", catalog)).toBe(source);
    const stored = serializeExpression(bound, "uuid");
    // Stored form reparses and renders back to the display form.
    const reparsed = parseExpression(stored);
    expect(serializeExpression(reparsed, "display", catalog)).toBe(source);
  });

  it("the ramp threshold round-trips unchanged", () => {
    const ramp = "1000000 - 30000 * (DAY_OF_MONTH() - 1)";
    expect(serializeExpression(parseExpression(ramp), "uuid")).toBe(ramp);
  });
});
