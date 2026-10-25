import { describe, expect, it } from "vitest";
import {
  MIN_CONFIDENCE,
  buildHistoryIndex,
  descriptionTokens,
  dismissalKey,
  normalizeDescription,
  suggestCategory,
  suggestionReason,
  tokenSimilarity,
  type CategorizedGroup,
} from "@/lib/category-suggestions";

// The pure side of the category suggestions: normalisation, similarity and
// the vote. Every description, id and category name is synthetic (see the
// repo's personal data policy).

const SUPER = "11111111-1111-4111-8111-111111111111";
const FOOD = "22222222-2222-4222-8222-222222222222";
const TRANSPORT = "33333333-3333-4333-8333-333333333333";
const NAMES = new Map([
  [SUPER, "Supermercado"],
  [FOOD, "Comida"],
  [TRANSPORT, "Transporte"],
]);

function suggest(description: string, history: CategorizedGroup[]) {
  return suggestCategory(description, buildHistoryIndex(history), NAMES);
}

describe("normalizeDescription", () => {
  it.each([
    ["COMERCIO EJEMPLO SPA 1234", "comercio ejemplo spa"],
    ["  Comercio   Ejemplo SpA #5678 ", "comercio ejemplo spa"],
    ["SUPERMERCADO FICTICIO LTDA", "supermercado ficticio ltda"],
    ["Compra 01/02 CAFÉ DE PRUEBA", "compra cafe de prueba"],
    ["PAGO REF TRX9F8A2 SERVICIO-FICTICIO", "pago ref servicio ficticio"],
    ["Ñandú Ficticio, S.A.", "nandu ficticio s a"],
    ["PAGO*COMERCIO123", "pago comercio"],
    ["COMERCIO/EJEMPLO#1234", "comercio ejemplo"],
    ["7ELEVEN FICTICIO", "eleven ficticio"],
    ["12345 00-000-00000-01", ""],
    ["", ""],
  ])("%j -> %j", (input, expected) => {
    expect(normalizeDescription(input)).toBe(expected);
  });
});

describe("descriptionTokens", () => {
  it.each([
    ["comercio ejemplo spa", ["comercio", "ejemplo"]],
    ["compra cafe de prueba", ["cafe", "prueba"]],
    ["nandu ficticio s a", ["nandu", "ficticio"]],
    ["uber uber eats", ["uber", "eats"]],
    ["pago con www", []],
  ])("%j -> %j", (input, expected) => {
    expect(descriptionTokens(input)).toEqual(expected);
  });
});

describe("tokenSimilarity", () => {
  it.each([
    [["comercio", "ejemplo"], ["comercio", "ejemplo"], 1],
    [["supermercado", "ficticio"], ["supermercado", "ficticio", "sucursal", "centro"], 0.75],
    [["servicio", "luz"], ["servicio", "agua"], (1 / 3 + 1 / 2) / 2],
    [["cafe"], ["libreria"], 0],
    [[], ["cafe"], 0],
  ])("%j vs %j = %d", (a, b, expected) => {
    expect(tokenSimilarity(a, b)).toBeCloseTo(expected);
    expect(tokenSimilarity(b, a)).toBeCloseTo(expected);
  });
});

describe("suggestCategory", () => {
  it("suggests the category of the same normalised description", () => {
    const result = suggest("COMERCIO EJEMPLO SPA 9999", [
      { description: "comercio ejemplo spa 1234", categoryId: FOOD, count: 3 },
    ]);
    expect(result).toEqual({
      categoryId: FOOD,
      categoryName: "Comida",
      confidence: 1,
      matchCount: 3,
      exact: true,
      reason: "3 transacciones con la misma descripción están en Comida",
    });
  });

  it("merges stored descriptions that normalise alike", () => {
    const result = suggest("SUPERMERCADO FICTICIO LTDA", [
      { description: "supermercado ficticio ltda 0001", categoryId: SUPER, count: 1 },
      { description: "supermercado ficticio ltda 0002", categoryId: SUPER, count: 1 },
    ]);
    expect(result?.matchCount).toBe(2);
    expect(result?.reason).toBe(
      "2 transacciones con la misma descripción están en Supermercado"
    );
  });

  it("suggests from similar descriptions", () => {
    const result = suggest("SUPERMERCADO FICTICIO SUCURSAL CENTRO", [
      { description: "supermercado ficticio ltda", categoryId: SUPER, count: 2 },
    ]);
    expect(result).toMatchObject({
      categoryId: SUPER,
      exact: false,
      matchCount: 2,
      reason: "2 transacciones similares están en Supermercado",
    });
    expect(result!.confidence).toBeCloseTo(0.75);
  });

  it("says singular for one row", () => {
    const result = suggest("SUPERMERCADO FICTICIO SUCURSAL CENTRO", [
      { description: "supermercado ficticio", categoryId: SUPER, count: 1 },
    ]);
    expect(result?.reason).toBe("1 transacción similar está en Supermercado");
  });

  it.each<[string, string, CategorizedGroup[]]>([
    ["no history", "COMERCIO EJEMPLO SPA", []],
    [
      "a weak match (one shared word of several)",
      "SERVICIO LUZ FICTICIA",
      [{ description: "servicio agua ejemplo", categoryId: SUPER, count: 10 }],
    ],
    [
      "only noise words",
      "PAGO 1234 COMPRA",
      [{ description: "pago compra", categoryId: SUPER, count: 10 }],
    ],
    [
      "a split vote",
      "COMERCIO EJEMPLO",
      [
        { description: "comercio ejemplo", categoryId: FOOD, count: 2 },
        { description: "comercio ejemplo", categoryId: SUPER, count: 2 },
      ],
    ],
    [
      "an unknown category",
      "COMERCIO EJEMPLO",
      [
        {
          description: "comercio ejemplo",
          categoryId: "44444444-4444-4444-8444-444444444444",
          count: 5,
        },
      ],
    ],
  ])("returns null for %s", (_name, description, history) => {
    expect(suggest(description, history)).toBeNull();
  });

  it("lets the majority win when it is clear enough", () => {
    const result = suggest("COMERCIO EJEMPLO", [
      { description: "comercio ejemplo", categoryId: FOOD, count: 9 },
      { description: "comercio ejemplo", categoryId: SUPER, count: 1 },
    ]);
    expect(result?.categoryId).toBe(FOOD);
    expect(result!.confidence).toBeGreaterThanOrEqual(MIN_CONFIDENCE);
    expect(result!.confidence).toBeCloseTo(0.9);
  });

  it("weighs exact matches above similar ones", () => {
    const result = suggest("TAXI FICTICIO CENTRO", [
      { description: "taxi ficticio centro", categoryId: TRANSPORT, count: 3 },
      { description: "taxi ficticio aeropuerto", categoryId: FOOD, count: 1 },
    ]);
    expect(result).toMatchObject({ categoryId: TRANSPORT, exact: true });
  });

  it("words a mix of exact and similar rows as similar", () => {
    const result = suggest("TAXI FICTICIO CENTRO", [
      { description: "taxi ficticio centro", categoryId: TRANSPORT, count: 3 },
      { description: "taxi ficticio aeropuerto", categoryId: TRANSPORT, count: 1 },
    ]);
    expect(result).toMatchObject({
      categoryId: TRANSPORT,
      exact: false,
      matchCount: 4,
      confidence: 1,
    });
  });
});

describe("suggestionReason", () => {
  it.each([
    [1, true, "1 transacción con la misma descripción está en Comida"],
    [1, false, "1 transacción similar está en Comida"],
    [1200, false, "1.200 transacciones similares están en Comida"],
  ])("%d, exact %s", (count, exact, expected) => {
    expect(suggestionReason(count, exact, "Comida")).toBe(expected);
  });
});

describe("dismissalKey", () => {
  it("lowercases the category id", () => {
    expect(dismissalKey("comercio ejemplo", FOOD.toUpperCase())).toBe(
      dismissalKey("comercio ejemplo", FOOD)
    );
  });
});
