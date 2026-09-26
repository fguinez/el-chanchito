import { describe, it, expect } from "vitest";
import {
  CATEGORY_ICON_NAMES,
  UUID_RE,
  isUuid,
  normalizeKeyword,
  validateCategoryInput,
  validateRuleInput,
  type ValidationResult,
} from "@/lib/categories";

// All identifiers and keywords below are synthetic (see the repo's personal
// data policy): fake uuids, made-up merchant keywords.
const CATEGORY_ID = "11111111-1111-4111-8111-111111111111";

function expectFailure<T>(result: ValidationResult<T>) {
  if (result.ok) throw new Error("expected a validation failure");
  return result;
}

function expectValue<T>(result: ValidationResult<T>): T {
  if (!result.ok) throw new Error(`unexpected failure: ${result.error}`);
  return result.value;
}

describe("UUID_RE / isUuid", () => {
  it("accepts a canonical uuid in either case", () => {
    expect(isUuid(CATEGORY_ID)).toBe(true);
    expect(UUID_RE.test("AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA")).toBe(true);
  });

  it("rejects malformed ids and non-strings", () => {
    for (const value of ["rules", "1111", `${CATEGORY_ID}0`, 999999, null]) {
      expect(isUuid(value)).toBe(false);
    }
  });
});

describe("CATEGORY_ICON_NAMES", () => {
  it("includes every icon the V007 seed categories use", () => {
    const seeds = [
      "shopping-cart",
      "car",
      "utensils",
      "gamepad-2",
      "heart-pulse",
      "settings",
      "arrow-left-right",
      "circle",
    ];
    for (const icon of seeds) {
      expect(CATEGORY_ICON_NAMES).toContain(icon);
    }
  });

  it("has no duplicates", () => {
    expect(new Set(CATEGORY_ICON_NAMES).size).toBe(CATEGORY_ICON_NAMES.length);
  });
});

describe("validateCategoryInput (create)", () => {
  it("trims the name and defaults color and icon to null", () => {
    const value = expectValue(
      validateCategoryInput({ name: "  Categoria Ejemplo  " })
    );
    expect(value).toEqual({
      name: "Categoria Ejemplo",
      color: null,
      icon: null,
    });
  });

  it("keeps an allowed icon and a color", () => {
    const value = expectValue(
      validateCategoryInput({
        name: "Mascotas Ejemplo",
        color: "#22c55e",
        icon: "paw-print",
      })
    );
    expect(value.color).toBe("#22c55e");
    expect(value.icon).toBe("paw-print");
  });

  it("lowercases and trims the color", () => {
    const value = expectValue(
      validateCategoryInput({ name: "Ejemplo", color: " #A855F7 " })
    );
    expect(value.color).toBe("#a855f7");
  });

  it("accepts explicit nulls for color and icon", () => {
    const value = expectValue(
      validateCategoryInput({ name: "Ejemplo", color: null, icon: null })
    );
    expect(value).toEqual({ name: "Ejemplo", color: null, icon: null });
  });

  it("rejects a non-object body", () => {
    for (const body of [null, undefined, "categoria", 999999, ["categoria"]]) {
      const failure = expectFailure(validateCategoryInput(body));
      expect(failure.status).toBe(400);
      expect(failure.error).toBe("Request body must be a JSON object");
    }
  });

  it("requires a name", () => {
    const failure = expectFailure(validateCategoryInput({ color: "#000000" }));
    expect(failure.field).toBe("name");
    expect(failure.error).toBe("Field 'name' is required");
  });

  it("rejects a blank or non-string name", () => {
    for (const name of ["   ", "", 999999, null]) {
      const failure = expectFailure(validateCategoryInput({ name }));
      expect(failure.field).toBe("name");
    }
  });

  it("accepts a 50-character name and rejects 51", () => {
    expect(validateCategoryInput({ name: "a".repeat(50) }).ok).toBe(true);
    const failure = expectFailure(validateCategoryInput({ name: "a".repeat(51) }));
    expect(failure.field).toBe("name");
  });

  it("measures the name length after trimming", () => {
    const value = expectValue(
      validateCategoryInput({ name: `  ${"a".repeat(50)}  ` })
    );
    expect(value.name).toHaveLength(50);
  });

  it("rejects colors that are not #rrggbb", () => {
    for (const color of ["22c55e", "#fff", "#22c55e80", "#gggggg", "red", 0]) {
      const failure = expectFailure(
        validateCategoryInput({ name: "Ejemplo", color })
      );
      expect(failure.field).toBe("color");
    }
  });

  it("rejects an icon outside the allow-list", () => {
    for (const icon of ["rocket", "ShoppingCart", "shopping_cart", "", 7]) {
      const failure = expectFailure(
        validateCategoryInput({ name: "Ejemplo", icon })
      );
      expect(failure.field).toBe("icon");
    }
  });
});

describe("validateCategoryInput (partial)", () => {
  it("returns only the fields present", () => {
    const value = expectValue(
      validateCategoryInput({ icon: "gift" }, { partial: true })
    );
    expect(value).toEqual({ icon: "gift" });
  });

  it("can clear the color with null", () => {
    const value = expectValue(
      validateCategoryInput({ color: null }, { partial: true })
    );
    expect(value).toEqual({ color: null });
  });

  it("still validates a name that is present", () => {
    const failure = expectFailure(
      validateCategoryInput({ name: "  " }, { partial: true })
    );
    expect(failure.field).toBe("name");
  });

  it("rejects a body with no known fields", () => {
    for (const body of [{}, { parentId: CATEGORY_ID }]) {
      const failure = expectFailure(
        validateCategoryInput(body, { partial: true })
      );
      expect(failure.status).toBe(400);
      expect(failure.field).toBeUndefined();
    }
  });
});

describe("normalizeKeyword", () => {
  it("trims and lowercases", () => {
    expect(expectValue(normalizeKeyword("  EJEMPLO Super  "))).toBe(
      "ejemplo super"
    );
  });

  it("keeps LIKE wildcards as plain characters", () => {
    expect(expectValue(normalizeKeyword("50%_OFF"))).toBe("50%_off");
  });

  it("rejects blank, missing, and non-string keywords", () => {
    for (const raw of ["", "   ", null, undefined, 999999]) {
      const failure = expectFailure(normalizeKeyword(raw));
      expect(failure.field).toBe("keyword");
    }
  });

  it("accepts a 100-character keyword and rejects 101", () => {
    expect(normalizeKeyword("k".repeat(100)).ok).toBe(true);
    expect(normalizeKeyword("k".repeat(101)).ok).toBe(false);
  });
});

describe("validateRuleInput (create)", () => {
  it("normalizes the keyword and defaults priority to 0", () => {
    const value = expectValue(
      validateRuleInput({ keyword: " EJEMPLO ", categoryId: CATEGORY_ID })
    );
    expect(value).toEqual({
      keyword: "ejemplo",
      categoryId: CATEGORY_ID,
      priority: 0,
    });
  });

  it("keeps an explicit priority", () => {
    const value = expectValue(
      validateRuleInput({
        keyword: "ejemplo",
        categoryId: CATEGORY_ID,
        priority: -5,
      })
    );
    expect(value.priority).toBe(-5);
  });

  it("requires a keyword", () => {
    const failure = expectFailure(validateRuleInput({ categoryId: CATEGORY_ID }));
    expect(failure.field).toBe("keyword");
    expect(failure.error).toBe("Field 'keyword' is required");
  });

  it("requires a categoryId", () => {
    const failure = expectFailure(validateRuleInput({ keyword: "ejemplo" }));
    expect(failure.field).toBe("categoryId");
    expect(failure.error).toBe("Field 'categoryId' is required");
  });

  it("rejects a categoryId that is not a uuid", () => {
    for (const categoryId of ["supermercado", 7, null]) {
      const failure = expectFailure(
        validateRuleInput({ keyword: "ejemplo", categoryId })
      );
      expect(failure.field).toBe("categoryId");
    }
  });

  it("accepts priorities at both bounds", () => {
    for (const priority of [-1000, 1000]) {
      const result = validateRuleInput({
        keyword: "ejemplo",
        categoryId: CATEGORY_ID,
        priority,
      });
      expect(result.ok).toBe(true);
    }
  });

  it("rejects priorities out of range, fractional, or not numbers", () => {
    for (const priority of [-1001, 1001, 1.5, "10", null, Number.NaN]) {
      const failure = expectFailure(
        validateRuleInput({
          keyword: "ejemplo",
          categoryId: CATEGORY_ID,
          priority,
        })
      );
      expect(failure.field).toBe("priority");
    }
  });
});

describe("validateRuleInput (partial)", () => {
  it("returns only the fields present", () => {
    const value = expectValue(
      validateRuleInput({ priority: 10 }, { partial: true })
    );
    expect(value).toEqual({ priority: 10 });
  });

  it("normalizes a keyword that is present", () => {
    const value = expectValue(
      validateRuleInput({ keyword: " NUEVO " }, { partial: true })
    );
    expect(value).toEqual({ keyword: "nuevo" });
  });

  it("still validates a categoryId that is present", () => {
    const failure = expectFailure(
      validateRuleInput({ categoryId: "no-es-uuid" }, { partial: true })
    );
    expect(failure.field).toBe("categoryId");
  });

  it("rejects a body with no known fields", () => {
    const failure = expectFailure(validateRuleInput({}, { partial: true }));
    expect(failure.status).toBe(400);
    expect(failure.field).toBeUndefined();
  });
});
