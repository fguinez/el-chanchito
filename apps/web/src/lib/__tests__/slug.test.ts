import { describe, it, expect } from "vitest";
import { slugify, uniqueSlug } from "@/lib/db/slug";

// `slugify` and `uniqueSlug` mint the create-only, institution-unique product
// slug. The vectors below are shared verbatim with the Python suite
// (apps/scrapers/tests/test_slug.py) so both creation paths provably agree;
// change them in both places or not at all.

describe("slugify", () => {
  // The cross-language vectors both implementations must satisfy.
  const sharedVectors: [name: string, kind: string, expected: string][] = [
    ["Tarjeta de crédito ****1234", "credit_card", "tarjeta-de-credito-1234"],
    ["Cuenta Corriente", "checking", "cuenta-corriente"],
    ["Fondo Ñuñoa Ültra", "investment", "fondo-nunoa-ultra"],
    [
      "Depósito a Plazo Nº 00-000-00000-01",
      "term_deposit",
      "deposito-a-plazo-no-00-000-00000-01",
    ],
    ["$$$", "credit_card", "credit-card"],
    ["MACH - checking (USD)", "checking", "mach-checking-usd"],
  ];

  it.each(sharedVectors)("shared vector: %s (%s) -> %s", (name, kind, expected) => {
    expect(slugify(name, kind)).toBe(expected);
  });

  it("falls back to the hyphenated kind when nothing is keepable", () => {
    expect(slugify("***", "term_deposit")).toBe("term-deposit");
  });
});

describe("uniqueSlug", () => {
  it("returns a free base unsuffixed", () => {
    expect(uniqueSlug("cuenta-corriente", new Set())).toBe("cuenta-corriente");
  });

  it("starts the suffix series at -2 for the first duplicate", () => {
    expect(uniqueSlug("x", new Set(["x"]))).toBe("x-2");
  });

  it("advances the suffix series past taken slots", () => {
    expect(uniqueSlug("x", new Set(["x", "x-2"]))).toBe("x-3");
  });

  it("does not let a taken suffix block a free base", () => {
    expect(uniqueSlug("x", new Set(["x-2"]))).toBe("x");
  });
});
