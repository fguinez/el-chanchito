import { describe, expect, it } from "vitest";
import { PgDialect } from "drizzle-orm/pg-core";
import { alias } from "drizzle-orm/pg-core";
import type { SQL } from "drizzle-orm";
import {
  MAX_SEARCH_TOKENS,
  descriptionTitle,
  matchesTransactionSearch,
  normalizeSearchText,
  SEARCH_QUERY_MAX_LENGTH,
  searchTokens,
} from "@/lib/transaction-search";
import {
  descriptionMatches,
  displayDescription,
  displayDescriptionOf,
} from "@/lib/db/transactions";
import { products, transactions } from "@/lib/db/schema";
import { searchQuerySchema } from "@/lib/api/schemas";

// Every description is synthetic (see the repo's personal data policy).

const render = (query: SQL) => new PgDialect().sqlToQuery(query);

describe("normalizeSearchText", () => {
  it("drops accents and case, keeping punctuation and spacing", () => {
    expect(normalizeSearchText("CAFÉ DE PRUEBA Ñuñoa")).toBe("cafe de prueba nunoa");
    expect(normalizeSearchText("Pingüino S.A.")).toBe("pinguino s.a.");
  });

  it("folds decomposed accents like composed ones", () => {
    expect(normalizeSearchText("Cafe\u0301")).toBe("cafe");
    expect(normalizeSearchText("Caf\u00e9")).toBe("cafe");
  });
});

describe("searchTokens", () => {
  it("splits on any whitespace, normalized and without repeats", () => {
    expect(searchTokens("  Café\tDE  café\nprueba ")).toEqual(["cafe", "de", "prueba"]);
  });

  it("is empty for a blank or missing query", () => {
    expect(searchTokens("")).toEqual([]);
    expect(searchTokens("   ")).toEqual([]);
    expect(searchTokens(null)).toEqual([]);
    expect(searchTokens(undefined)).toEqual([]);
  });

  it("keeps at most MAX_SEARCH_TOKENS words", () => {
    const words = Array.from({ length: MAX_SEARCH_TOKENS + 3 }, (_, i) => `w${i}`);
    expect(searchTokens(words.join(" "))).toEqual(words.slice(0, MAX_SEARCH_TOKENS));
  });
});

describe("matchesTransactionSearch", () => {
  const RAW = "COMPRA SUPERMERCADO EJEMPLO";
  const ALIAS = "Café de prueba";

  it("ignores accents and case on either side", () => {
    expect(matchesTransactionSearch("cafe", ALIAS, RAW)).toBe(true);
    expect(matchesTransactionSearch("CAFÉ", ALIAS, RAW)).toBe(true);
    expect(matchesTransactionSearch("supermercado", ALIAS, RAW)).toBe(true);
  });

  it("needs every word, each in either field", () => {
    expect(matchesTransactionSearch("cafe supermercado", ALIAS, RAW)).toBe(true);
    expect(matchesTransactionSearch("cafe farmacia", ALIAS, RAW)).toBe(false);
  });

  it("matches substrings literally, wildcards included", () => {
    expect(matchesTransactionSearch("mercado", RAW)).toBe(true);
    expect(matchesTransactionSearch("50%", "DESCUENTO 50% DE PRUEBA")).toBe(true);
    expect(matchesTransactionSearch("5_%", "DESCUENTO 50% DE PRUEBA")).toBe(false);
  });

  it("matches everything for a blank query", () => {
    expect(matchesTransactionSearch("", RAW)).toBe(true);
    expect(matchesTransactionSearch([], RAW)).toBe(true);
  });

  it("takes precomputed tokens and skips missing fields", () => {
    const tokens = searchTokens("Café");
    expect(matchesTransactionSearch(tokens, null, undefined, ALIAS)).toBe(true);
    expect(matchesTransactionSearch(tokens, null, undefined)).toBe(false);
  });
});

describe("descriptionTitle", () => {
  it("names the other description when it differs", () => {
    expect(descriptionTitle("Café de prueba", "COMPRA CAFE EJEMPLO")).toBe(
      "Café de prueba\nDescripción original: COMPRA CAFE EJEMPLO"
    );
    expect(
      descriptionTitle("COMPRA CAFE EJEMPLO", "Café de prueba", "Se muestra como")
    ).toBe("COMPRA CAFE EJEMPLO\nSe muestra como: Café de prueba");
  });

  it("is the shown text alone when there is nothing else to say", () => {
    expect(descriptionTitle("COMPRA CAFE EJEMPLO", "COMPRA CAFE EJEMPLO")).toBe(
      "COMPRA CAFE EJEMPLO"
    );
    expect(descriptionTitle("COMPRA CAFE EJEMPLO", null)).toBe("COMPRA CAFE EJEMPLO");
  });
});

describe("searchQuerySchema", () => {
  it("trims the text", () => {
    expect(searchQuerySchema.parse("  café ")).toBe("café");
  });

  it("reads a blank or missing text as no search", () => {
    expect(searchQuerySchema.parse("")).toBeUndefined();
    expect(searchQuerySchema.parse("   ")).toBeUndefined();
    expect(searchQuerySchema.parse(undefined)).toBeUndefined();
  });

  it("caps the length", () => {
    const max = "a".repeat(SEARCH_QUERY_MAX_LENGTH);
    expect(searchQuerySchema.parse(max)).toBe(max);
    expect(searchQuerySchema.safeParse(`${max}a`).success).toBe(false);
  });
});

describe("descriptionMatches", () => {
  it("is undefined for a blank query", () => {
    expect(descriptionMatches("")).toBeUndefined();
    expect(descriptionMatches("   ")).toBeUndefined();
    expect(descriptionMatches(undefined)).toBeUndefined();
  });

  it("checks each folded word against the raw and the display description", () => {
    const { sql, params } = render(descriptionMatches("Café  PRUEBA")!);
    // An AND over the words of an OR over the fields: one strpos per word and
    // field, over the accent-folded text, each word a bound parameter.
    const strpos = String.raw`strpos\((?:(?! and | or ).)+, \$\d+\) > 0`;
    const word = String.raw`\(${strpos} or ${strpos}\)`;
    expect(sql).toMatch(new RegExp(String.raw`^\(${word} and ${word}\)$`));
    expect(params).toEqual(["cafe", "cafe", "prueba", "prueba"]);
    // The raw text leads each word's OR, so a raw hit skips the alias lookup.
    for (const group of sql.split(" and ")) {
      expect(group).toMatch(/^\(+strpos\(lower\(regexp_replace\(normalize\("transactions"\."description", NFD\)/);
    }
    expect(sql).toContain("lower(regexp_replace(normalize(");
    expect(sql).toContain("'[\\u0300-\\u036f]'");
    expect(sql).toContain("alias_for_transaction(");
    expect(sql).toContain('"transactions"."description"');
    expect(sql).not.toMatch(/\blike\b/i);
  });

  it("binds wildcard characters as plain text", () => {
    const { sql, params } = render(descriptionMatches("50%_")!);
    expect(sql).not.toMatch(/\blike\b/i);
    expect(params).toContain("50%_");
  });

  it("searches the fields it is given, aliased tables included", () => {
    const leg = alias(transactions, "from_txn");
    const legProduct = alias(products, "from_product");
    const { sql } = render(
      descriptionMatches("prueba", [leg.description, displayDescriptionOf(leg, legProduct)])!
    );
    expect(sql).toContain('"from_txn"."custom_description"');
    expect(sql).toContain('"from_product"."currency"');
    expect(sql).not.toContain('"transactions"');
  });
});

describe("displayDescriptionOf", () => {
  it("builds the shared displayDescription", () => {
    expect(render(displayDescriptionOf(transactions, products)).sql).toBe(
      render(displayDescription).sql
    );
  });
});
