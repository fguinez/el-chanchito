import { describe, it, expect } from "vitest";
import {
  ALIAS_MAX_LENGTH,
  MATCH_DESCRIPTION_MAX_LENGTH,
  MATCH_TYPES,
  normalizeAlias,
  normalizeMatchDescription,
  normalizeMatchType,
  validateAliasInput,
} from "@/lib/aliases";
import { type ValidationResult } from "@/lib/categories";

// All descriptions, aliases and amounts below are synthetic (see the repo's
// personal data policy): made-up merchant text, no real figures.

function expectFailure<T>(result: ValidationResult<T>) {
  if (result.ok) throw new Error("expected a validation failure");
  return result;
}

function expectValue<T>(result: ValidationResult<T>): T {
  if (!result.ok) throw new Error(`unexpected failure: ${result.error}`);
  return result.value;
}

describe("MATCH_TYPES", () => {
  it("is exactly exact and substring", () => {
    expect([...MATCH_TYPES]).toEqual(["exact", "substring"]);
  });
});

describe("normalizeMatchDescription", () => {
  it("trims but preserves case (matching lowercases later)", () => {
    expect(expectValue(normalizeMatchDescription("  COMPRA Web  "))).toBe(
      "COMPRA Web"
    );
  });

  it("does not escape % or _", () => {
    expect(expectValue(normalizeMatchDescription("50%_OFF"))).toBe("50%_OFF");
  });

  it("rejects blank, missing, and non-string values", () => {
    for (const raw of ["", "   ", null, undefined, 999999]) {
      const failure = expectFailure(normalizeMatchDescription(raw));
      expect(failure.field).toBe("matchDescription");
    }
  });

  it("accepts the max length and rejects one over", () => {
    expect(normalizeMatchDescription("a".repeat(MATCH_DESCRIPTION_MAX_LENGTH)).ok).toBe(
      true
    );
    expect(
      normalizeMatchDescription("a".repeat(MATCH_DESCRIPTION_MAX_LENGTH + 1)).ok
    ).toBe(false);
  });
});

describe("normalizeAlias", () => {
  it("trims the alias", () => {
    expect(expectValue(normalizeAlias("  Supermercado  "))).toBe("Supermercado");
  });

  it("rejects blank, missing, and non-string values", () => {
    for (const raw of ["", "   ", null, undefined, 7]) {
      const failure = expectFailure(normalizeAlias(raw));
      expect(failure.field).toBe("alias");
    }
  });

  it("accepts the max length and rejects one over", () => {
    expect(normalizeAlias("a".repeat(ALIAS_MAX_LENGTH)).ok).toBe(true);
    expect(normalizeAlias("a".repeat(ALIAS_MAX_LENGTH + 1)).ok).toBe(false);
  });
});

describe("normalizeMatchType", () => {
  it("defaults an absent value to exact", () => {
    for (const raw of [undefined, null, ""]) {
      expect(expectValue(normalizeMatchType(raw))).toBe("exact");
    }
  });

  it("keeps each supported type", () => {
    for (const type of MATCH_TYPES) {
      expect(expectValue(normalizeMatchType(type))).toBe(type);
    }
  });

  it("rejects an unsupported value", () => {
    for (const raw of ["EXACT", "regex", "contains", 7]) {
      const failure = expectFailure(normalizeMatchType(raw));
      expect(failure.field).toBe("matchType");
    }
  });
});

describe("validateAliasInput (create)", () => {
  it("defaults matchType to exact and priority to 0", () => {
    const value = expectValue(
      validateAliasInput({ matchDescription: " COMPRA WEB ", alias: " Tienda " })
    );
    expect(value).toEqual({
      matchDescription: "COMPRA WEB",
      matchType: "exact",
      alias: "Tienda",
      priority: 0,
    });
  });

  it("keeps an explicit matchType and priority", () => {
    const value = expectValue(
      validateAliasInput({
        matchDescription: "compra",
        alias: "Tienda",
        matchType: "substring",
        priority: -5,
      })
    );
    expect(value.matchType).toBe("substring");
    expect(value.priority).toBe(-5);
  });

  it("rejects a non-object body", () => {
    for (const body of [null, undefined, "alias", 999999, ["alias"]]) {
      const failure = expectFailure(validateAliasInput(body));
      expect(failure.status).toBe(400);
      expect(failure.error).toBe("Request body must be a JSON object");
    }
  });

  it("requires a matchDescription", () => {
    const failure = expectFailure(validateAliasInput({ alias: "Tienda" }));
    expect(failure.field).toBe("matchDescription");
    expect(failure.error).toBe("Field 'matchDescription' is required");
  });

  it("requires an alias", () => {
    const failure = expectFailure(
      validateAliasInput({ matchDescription: "compra" })
    );
    expect(failure.field).toBe("alias");
    expect(failure.error).toBe("Field 'alias' is required");
  });

  it("rejects an unsupported matchType", () => {
    const failure = expectFailure(
      validateAliasInput({
        matchDescription: "compra",
        alias: "Tienda",
        matchType: "regex",
      })
    );
    expect(failure.field).toBe("matchType");
  });

  it("accepts priorities at both bounds", () => {
    for (const priority of [-1000, 1000]) {
      const result = validateAliasInput({
        matchDescription: "compra",
        alias: "Tienda",
        priority,
      });
      expect(result.ok).toBe(true);
    }
  });

  it("rejects priorities out of range, fractional, or not numbers", () => {
    for (const priority of [-1001, 1001, 1.5, "10", null, Number.NaN]) {
      const failure = expectFailure(
        validateAliasInput({
          matchDescription: "compra",
          alias: "Tienda",
          priority,
        })
      );
      expect(failure.field).toBe("priority");
    }
  });
});

describe("validateAliasInput (partial)", () => {
  it("returns only the fields present", () => {
    const value = expectValue(
      validateAliasInput({ priority: 10 }, { partial: true })
    );
    expect(value).toEqual({ priority: 10 });
  });

  it("normalizes a matchDescription that is present", () => {
    const value = expectValue(
      validateAliasInput({ matchDescription: "  COMPRA  " }, { partial: true })
    );
    expect(value).toEqual({ matchDescription: "COMPRA" });
  });

  it("still validates an alias that is present", () => {
    const failure = expectFailure(
      validateAliasInput({ alias: "   " }, { partial: true })
    );
    expect(failure.field).toBe("alias");
  });

  it("still validates a matchType that is present", () => {
    const failure = expectFailure(
      validateAliasInput({ matchType: "regex" }, { partial: true })
    );
    expect(failure.field).toBe("matchType");
  });

  it("rejects a body with no known fields", () => {
    for (const body of [{}, { unrelated: "x" }]) {
      const failure = expectFailure(
        validateAliasInput(body, { partial: true })
      );
      expect(failure.status).toBe(400);
      expect(failure.field).toBeUndefined();
    }
  });
});
