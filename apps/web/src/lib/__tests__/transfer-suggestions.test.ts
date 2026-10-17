import { describe, expect, it } from "vitest";
import {
  PATTERN_PREFIX_LENGTH,
  SCORE_BALANCE_MATCH,
  SCORE_DAY_PENALTY,
  SCORE_EXACT_AMOUNT,
  SCORE_KEYWORD,
  SCORE_NAME_MATCH,
  SCORE_PATTERN,
  SCORE_PRODUCT_PAIR,
  SCORE_SAME_DAY,
  SCORE_SHARED_INSTITUTION_MATCH,
  buildSuggestions,
  containsWords,
  dayGapLabel,
  daysBetween,
  descriptionPrefix,
  findTransferKeyword,
  isMatchableName,
  lookbackStart,
  matchesPattern,
  normalizeText,
  scoreSuggestion,
  sortSuggestions,
  type ConfirmedPattern,
  type OneSidedCandidate,
  type PairCandidate,
  type SuggestionTransactionLeg,
} from "@/lib/transfer-suggestions";

// Every id, name, description and amount below is synthetic (see the repo's
// personal data policy): fake uuids, a $ 1.000.000 transfer between fake
// products, account numbers in the 00-000-00000-0X scheme.

const CHECKING_ID = "11111111-1111-4111-8111-111111111111";
const CARD_ID = "22222222-2222-4222-8222-222222222222";
const FUND_ID = "33333333-3333-4333-8333-333333333333";
const AMOUNT = 1_000_000;

function leg(
  overrides: Partial<SuggestionTransactionLeg> = {}
): SuggestionTransactionLeg {
  return {
    transactionId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    productId: CHECKING_ID,
    productName: "Cuenta corriente",
    institutionName: "Banco de Prueba",
    date: "2026-09-15",
    description: "Compra comercio de prueba",
    amount: -AMOUNT,
    currency: "CLP",
    ...overrides,
  };
}

function pair(
  source: Partial<SuggestionTransactionLeg> = {},
  target: Partial<SuggestionTransactionLeg> = {}
): PairCandidate {
  return {
    kind: "pair",
    source: leg(source),
    target: leg({
      transactionId: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
      productId: CARD_ID,
      productName: "Tarjeta de crédito",
      description: "Abono recibido",
      amount: AMOUNT,
      ...target,
    }),
  };
}

function oneSided(overrides: Partial<OneSidedCandidate> = {}): OneSidedCandidate {
  return {
    kind: "one_sided",
    source: leg({ description: "Transf a Fintual 00-000-00000-01" }),
    target: {
      transactionId: null,
      productId: FUND_ID,
      productName: "Fondo de prueba",
      institutionName: "Fintual",
      date: null,
      description: null,
      amount: null,
      currency: "CLP",
    },
    matchedName: "Fintual",
    matchedBy: "institution",
    institutionSiblings: 0,
    balanceMatch: null,
    ...overrides,
  };
}

describe("normalizeText", () => {
  it.each([
    ["Traspaso  A Cuenta", "traspaso a cuenta"],
    ["Depósito Ahorro", "deposito ahorro"],
    ["PAGO-TC/1234", "pago tc 1234"],
    ["  Ñandú  ", "nandu"],
  ])("normalizes %j to %j", (input, expected) => {
    expect(normalizeText(input)).toBe(expected);
  });
});

describe("descriptionPrefix", () => {
  it.each([
    ["Traspaso a 00-000-00000-01", "traspaso a"],
    ["Traspaso a 00-000-00000-02", "traspaso a"],
    ["Pago TC 1234 15/09/2026", "pago tc"],
    ["Transferencia Électrónica  a Terceros", "transferencia electr"],
  ])("keys %j as %j", (input, expected) => {
    expect(descriptionPrefix(input)).toBe(expected);
  });

  it("never exceeds the prefix length", () => {
    const prefix = descriptionPrefix("Una glosa bastante larga para cortarla");

    expect(prefix.length).toBeLessThanOrEqual(PATTERN_PREFIX_LENGTH);
  });
});

describe("findTransferKeyword", () => {
  it.each([
    ["TRASPASO A TERCEROS", "Traspaso"],
    ["transferencia recibida", "Transferencia"],
    ["Pago Tarjeta de Crédito", "Pago tarjeta"],
    ["Abono por pago", "Abono"],
    ["PAGO TC NACIONAL", "Pago TC"],
    ["Compra supermercado", null],
    ["Abonos varios", null],
  ])("finds the keyword in %j", (description, expected) => {
    expect(findTransferKeyword(description)).toBe(expected);
  });
});

describe("containsWords", () => {
  it.each([
    ["Transf a FINTUAL spa", "Fintual", true],
    ["Transf a Fintualito", "Fintual", false],
    ["Aporte Fondo Ahorro Viaje", "Fondo ahorro", true],
    ["Compra", "", false],
  ])("%j contains %j: %s", (haystack, needle, expected) => {
    expect(containsWords(haystack, needle)).toBe(expected);
  });
});

describe("isMatchableName", () => {
  it.each([
    ["Fintual", true],
    ["BCI", false],
    ["Abono", false],
    ["Transferencia", false],
    ["Fondo de prueba", true],
  ])("%j is matchable: %s", (name, expected) => {
    expect(isMatchableName(name)).toBe(expected);
  });
});

describe("dates", () => {
  it.each([
    ["2026-09-15", "2026-09-15", 0],
    ["2026-09-15", "2026-09-16", 1],
    ["2026-09-30", "2026-10-02", 2],
  ])("%s and %s are %i days apart", (a, b, expected) => {
    expect(daysBetween(a, b)).toBe(expected);
    expect(daysBetween(b, a)).toBe(expected);
  });

  it.each([
    [0, "Mismo día"],
    [1, "1 día de diferencia"],
    [3, "3 días de diferencia"],
  ])("labels a %i day gap as %j", (days, expected) => {
    expect(dayGapLabel(days)).toBe(expected);
  });

  it("starts the lookback 90 days back", () => {
    expect(lookbackStart("2026-10-17")).toBe("2026-07-19");
  });
});

describe("scoreSuggestion", () => {
  it("scores a same-day pair on amount and date", () => {
    const result = scoreSuggestion(pair({}, { description: "Compra devuelta" }));

    expect(result).toEqual({
      score: SCORE_EXACT_AMOUNT + SCORE_SAME_DAY,
      evidence: ["Mismo monto", "Mismo día"],
    });
  });

  it.each([
    ["2026-09-16", SCORE_SAME_DAY - SCORE_DAY_PENALTY, "1 día de diferencia"],
    ["2026-09-18", 0, "3 días de diferencia"],
  ])("lowers the date signal for a target on %s", (date, points, label) => {
    const result = scoreSuggestion(
      pair({}, { date, description: "Compra devuelta" })
    );

    expect(result.score).toBe(SCORE_EXACT_AMOUNT + points);
    expect(result.evidence).toContain(label);
  });

  it("adds a keyword once and quotes the description", () => {
    const result = scoreSuggestion(
      pair({ description: "Traspaso a 00-000-00000-02 de prueba" })
    );

    expect(result.score).toBe(SCORE_EXACT_AMOUNT + SCORE_SAME_DAY + SCORE_KEYWORD);
    expect(result.evidence).toEqual([
      "Mismo monto",
      "Mismo día",
      "Glosa 'Traspaso a 00-000-00...'",
    ]);
  });

  it("ranks a confirmed pattern on top of the same products", () => {
    const candidate = pair({ description: "Pago TC 1234" });
    const patterns: ConfirmedPattern[] = [
      {
        sourceProductId: CHECKING_ID,
        targetProductId: CARD_ID,
        descriptionPrefix: "pago tc",
      },
    ];

    const withPattern = scoreSuggestion(candidate, patterns);
    const withPairOnly = scoreSuggestion(candidate, [
      { ...patterns[0], descriptionPrefix: "otra glosa" },
    ]);
    const without = scoreSuggestion(candidate);

    expect(withPattern.score - without.score).toBe(SCORE_PATTERN);
    expect(withPattern.evidence).toContain("Patrón confirmado antes");
    expect(withPairOnly.score - without.score).toBe(SCORE_PRODUCT_PAIR);
    expect(withPairOnly.evidence).toContain(
      "Mismos productos que una transferencia confirmada"
    );
  });

  it("ignores a pattern for the products the other way around", () => {
    const candidate = pair({ description: "Pago TC 1234" });

    const result = scoreSuggestion(candidate, [
      {
        sourceProductId: CARD_ID,
        targetProductId: CHECKING_ID,
        descriptionPrefix: "pago tc",
      },
    ]);

    expect(result.evidence).not.toContain("Patrón confirmado antes");
  });

  it("scores a one-sided name match", () => {
    const result = scoreSuggestion(oneSided());

    expect(result).toEqual({
      score: SCORE_NAME_MATCH,
      evidence: ["La glosa menciona Fintual"],
    });
  });

  it.each([
    ["the product's own name", "product", 2, SCORE_NAME_MATCH],
    ["the only feedless product's institution", "institution", 0, SCORE_NAME_MATCH],
    [
      "an institution with several feedless products",
      "institution",
      2,
      SCORE_SHARED_INSTITUTION_MATCH,
    ],
  ] as const)("scores a match on %s", (_, matchedBy, siblings, points) => {
    const result = scoreSuggestion(
      oneSided({ matchedBy, institutionSiblings: siblings })
    );

    expect(result.score).toBe(points);
  });

  it("boosts a one-sided match backed by a balance rise", () => {
    const result = scoreSuggestion(
      oneSided({ balanceMatch: { date: "2026-09-16", delta: 999_999 } })
    );

    expect(result.score).toBe(
      SCORE_NAME_MATCH +
        SCORE_BALANCE_MATCH +
        SCORE_SAME_DAY -
        SCORE_DAY_PENALTY
    );
    expect(result.evidence).toEqual([
      "La glosa menciona Fintual",
      "Depósito similar en Fondo de prueba",
      "1 día de diferencia",
    ]);
  });
});

describe("matchesPattern", () => {
  const patterns: ConfirmedPattern[] = [
    {
      sourceProductId: CHECKING_ID,
      targetProductId: FUND_ID,
      descriptionPrefix: "aporte mensual",
    },
  ];

  it.each([
    ["Aporte mensual 0001", FUND_ID, true],
    ["Aporte mensual 0001", CARD_ID, false],
    ["Otro aporte", FUND_ID, false],
  ])("%j to %s matches: %s", (description, target, expected) => {
    expect(
      matchesPattern({ productId: CHECKING_ID, description }, target, patterns)
    ).toBe(expected);
  });
});

describe("buildSuggestions and sortSuggestions", () => {
  it("keys, prices and orders the candidates", () => {
    const strong = pair({ description: "Traspaso a terceros" });
    const weakRecent = pair(
      {
        transactionId: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        date: "2026-09-20",
        description: "Compra",
      },
      {
        transactionId: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
        date: "2026-09-22",
        description: "Compra devuelta",
      }
    );

    const result = buildSuggestions([weakRecent, strong, oneSided()]);

    expect(result.map((s) => s.id)).toEqual([
      "pair:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa:bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
      "pair:cccccccc-cccc-4ccc-8ccc-cccccccccccc:dddddddd-dddd-4ddd-8ddd-dddddddddddd",
      `one_sided:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa:${FUND_ID}`,
    ]);
    expect(result.every((s) => s.amount === AMOUNT)).toBe(true);
  });

  it("breaks a score tie by the most recent source row", () => {
    const older = { id: "a", score: 10, source: { date: "2026-09-01" } };
    const newer = { id: "b", score: 10, source: { date: "2026-09-10" } };

    expect(sortSuggestions([older, newer])).toEqual([newer, older]);
  });
});
