import { describe, it, expect } from "vitest";
import {
  createsParentCycle,
  isUniqueViolation,
  planProductUpdate,
  validateAccountPatch,
  validateInstitutionPatch,
  validateProductPatch,
  type ManagementResult,
  type ProductRuleRow,
} from "@/lib/management";

// All figures and identifiers below are synthetic (see the repo's personal
// data policy): fake uuids, fake CLP amounts, fake slugs.
const CHECKING_ID = "11111111-1111-4111-8111-111111111111";
const DEBIT_ID = "22222222-2222-4222-8222-222222222222";
const LINE_ID = "33333333-3333-4333-8333-333333333333";
const GHOST_ID = "44444444-4444-4444-8444-444444444444";
const OTHER_INSTITUTION_ID = "99999999-9999-4999-8999-999999999999";

const checking: ProductRuleRow = {
  id: CHECKING_ID,
  slug: "cuenta-corriente",
  parentProductId: null,
  isActive: true,
  currentBalance: "2500000.00000000",
};
const debit: ProductRuleRow = {
  id: DEBIT_ID,
  slug: "tarjeta-de-debito",
  parentProductId: CHECKING_ID,
  isActive: true,
  currentBalance: null,
};
const line: ProductRuleRow = {
  id: LINE_ID,
  slug: "linea-de-credito",
  parentProductId: null,
  isActive: true,
  currentBalance: "1000000.00000000",
};
// A retired roll-up: inactive, balance stripped. Keeps its slug reserved.
const ghost: ProductRuleRow = {
  id: GHOST_ID,
  slug: "inversiones",
  parentProductId: null,
  isActive: false,
  currentBalance: null,
};

const institutionProducts = [checking, debit, line, ghost];

function expectFailure<T>(result: ManagementResult<T>) {
  if (result.ok) throw new Error("expected a failure");
  return result;
}

function expectValue<T>(result: ManagementResult<T>): T {
  if (!result.ok) throw new Error(`unexpected failure: ${result.error}`);
  return result.value;
}

describe("validateProductPatch", () => {
  it("accepts every editable field", () => {
    const value = expectValue(
      validateProductPatch({
        name: "  Cuenta Corriente  ",
        slug: "cuenta-principal",
        parentProductId: CHECKING_ID,
        isActive: false,
        displayOrder: 2,
      })
    );
    expect(value).toEqual({
      name: "Cuenta Corriente",
      slug: "cuenta-principal",
      parentProductId: CHECKING_ID,
      isActive: false,
      displayOrder: 2,
    });
  });

  it("canonicalizes a typed slug with the generated-slug rules", () => {
    const value = expectValue(validateProductPatch({ slug: " Cuenta Ahorro Ñuñoa " }));
    expect(value.slug).toBe("cuenta-ahorro-nunoa");
  });

  it("accepts null to clear the parent", () => {
    const value = expectValue(validateProductPatch({ parentProductId: null }));
    expect(value).toEqual({ parentProductId: null });
  });

  it.each([
    ["kind", { kind: "savings" }],
    ["currency", { currency: "USD" }],
    ["externalRef", { externalRef: "00-000-00000-01" }],
  ])("refuses the scraper-identity field %s", (field, body) => {
    const failure = expectFailure(validateProductPatch(body));
    expect(failure.status).toBe(400);
    expect(failure.field).toBe(field);
    expect(failure.error).toMatch(/cannot be edited/);
  });

  it.each([
    ["a non-object body", null, undefined],
    ["an array body", [], undefined],
    ["an empty body", {}, undefined],
    ["an unknown field", { nickname: "x" }, "nickname"],
    ["a blank name", { name: "   " }, "name"],
    ["a non-string name", { name: 42 }, "name"],
    ["an overlong name", { name: "x".repeat(121) }, "name"],
    ["a slug with nothing keepable", { slug: "***" }, "slug"],
    ["an overlong slug", { slug: "a".repeat(81) }, "slug"],
    ["a parent that is not a uuid", { parentProductId: "cuenta-corriente" }, "parentProductId"],
    ["a non-boolean isActive", { isActive: "false" }, "isActive"],
    ["a fractional displayOrder", { displayOrder: 1.5 }, "displayOrder"],
    ["an out-of-range displayOrder", { displayOrder: 1_000_001 }, "displayOrder"],
  ])("rejects %s", (_label, body, field) => {
    const failure = expectFailure(validateProductPatch(body));
    expect(failure.status).toBe(400);
    expect(failure.field).toBe(field);
  });
});

describe("planProductUpdate", () => {
  it("renames without touching the slug", () => {
    const changes = expectValue(
      planProductUpdate(checking, { name: "Cuenta Corriente" }, institutionProducts)
    );
    expect(changes).toEqual({ name: "Cuenta Corriente" });
  });

  it("drops fields that already hold the requested value", () => {
    const changes = expectValue(
      planProductUpdate(
        debit,
        { slug: "tarjeta-de-debito", parentProductId: CHECKING_ID, isActive: true },
        institutionProducts
      )
    );
    expect(changes).toEqual({});
  });

  it("refuses to deactivate a product with no balance", () => {
    const failure = expectFailure(
      planProductUpdate(debit, { isActive: false }, institutionProducts)
    );
    expect(failure.status).toBe(409);
    expect(failure.field).toBe("isActive");
  });

  it("deactivates a product that has a balance", () => {
    const changes = expectValue(
      planProductUpdate(checking, { isActive: false }, institutionProducts)
    );
    expect(changes).toEqual({ isActive: false });
  });

  it("reactivates an inactive product that kept its balance", () => {
    const inactive = { ...line, isActive: false };
    const changes = expectValue(
      planProductUpdate(inactive, { isActive: true }, [checking, inactive])
    );
    expect(changes).toEqual({ isActive: true });
  });

  it("accepts a slug that is free across the institution", () => {
    const changes = expectValue(
      planProductUpdate(checking, { slug: "cuenta-principal" }, institutionProducts)
    );
    expect(changes).toEqual({ slug: "cuenta-principal" });
  });

  it.each([
    ["an active sibling", "linea-de-credito"],
    ["a retired ghost", "inversiones"],
  ])("refuses a slug held by %s", (_label, slug) => {
    const failure = expectFailure(
      planProductUpdate(checking, { slug }, institutionProducts)
    );
    expect(failure.status).toBe(409);
    expect(failure.field).toBe("slug");
  });

  it("links a product to a sibling", () => {
    const changes = expectValue(
      planProductUpdate(line, { parentProductId: CHECKING_ID }, institutionProducts)
    );
    expect(changes).toEqual({ parentProductId: CHECKING_ID });
  });

  it("clears a parent", () => {
    const changes = expectValue(
      planProductUpdate(debit, { parentProductId: null }, institutionProducts)
    );
    expect(changes).toEqual({ parentProductId: null });
  });

  it.each([
    ["the product itself", CHECKING_ID, checking],
    ["a product of another institution", OTHER_INSTITUTION_ID, checking],
    ["a retired ghost", GHOST_ID, checking],
    ["its own descendant", DEBIT_ID, checking],
  ])("refuses %s as parent", (_label, parentProductId, product) => {
    const failure = expectFailure(
      planProductUpdate(product, { parentProductId }, institutionProducts)
    );
    expect(failure.status).toBe(400);
    expect(failure.field).toBe("parentProductId");
  });

  it("passes display order through", () => {
    const changes = expectValue(
      planProductUpdate(checking, { displayOrder: -1 }, institutionProducts)
    );
    expect(changes).toEqual({ displayOrder: -1 });
  });
});

describe("createsParentCycle", () => {
  const parentOf = new Map<string, string | null>([
    ["a", null],
    ["b", "a"],
    ["c", "b"],
  ]);

  it("detects the product on the candidate parent's ancestor chain", () => {
    expect(createsParentCycle("a", "c", parentOf)).toBe(true);
  });

  it("allows a parent outside the product's subtree", () => {
    expect(createsParentCycle("c", "a", parentOf)).toBe(false);
  });

  it("terminates on a loop already present in the data", () => {
    const looped = new Map<string, string | null>([
      ["x", "y"],
      ["y", "x"],
    ]);
    expect(createsParentCycle("z", "x", looped)).toBe(false);
  });
});

describe("validateInstitutionPatch", () => {
  it("accepts and normalizes every editable field", () => {
    const value = expectValue(
      validateInstitutionPatch({
        name: " Importado CSV ",
        kind: "bank",
        country: "cl",
        url: "https://bank.example",
      })
    );
    expect(value).toEqual({
      name: "Importado CSV",
      kind: "bank",
      country: "CL",
      url: "https://bank.example",
    });
  });

  it.each([null, "", "   "])("clears country and url with %j", (empty) => {
    const value = expectValue(validateInstitutionPatch({ country: empty, url: empty }));
    expect(value).toEqual({ country: null, url: null });
  });

  it.each([
    ["the slug", { slug: "banco" }, "slug"],
    ["an unknown kind", { kind: "broker" }, "kind"],
    ["a three-letter country", { country: "CHL" }, "country"],
    ["a non-http url", { url: "javascript:alert(1)" }, "url"],
    ["an unparseable url", { url: "bank dot example" }, "url"],
    ["a blank name", { name: "" }, "name"],
  ])("rejects %s", (_label, body, field) => {
    const failure = expectFailure(validateInstitutionPatch(body));
    expect(failure.field).toBe(field);
  });
});

describe("validateAccountPatch", () => {
  it("trims the new name", () => {
    expect(expectValue(validateAccountPatch({ name: "  Empresa " }))).toEqual({
      name: "Empresa",
    });
  });

  it.each([
    ["a blank name", { name: " " }, "name"],
    ["another field", { isActive: false }, "isActive"],
  ])("rejects %s", (_label, body, field) => {
    expect(expectFailure(validateAccountPatch(body)).field).toBe(field);
  });
});

describe("isUniqueViolation", () => {
  const violation = { code: "23505", constraint_name: "uq_products_account_slug" };

  it("recognizes a driver error", () => {
    expect(isUniqueViolation(violation)).toBe(true);
  });

  it("walks a wrapped error's cause chain", () => {
    const wrapped = new Error("Failed query", { cause: violation });
    expect(isUniqueViolation(wrapped, "uq_products_account_slug")).toBe(true);
  });

  it("matches only the named constraint when one is given", () => {
    expect(isUniqueViolation(violation, "uq_products_identity")).toBe(false);
  });

  it.each([
    ["another SQLSTATE", { code: "23503" }],
    ["a plain error", new Error("boom")],
    ["a non-object", "23505"],
    ["nothing", undefined],
  ])("ignores %s", (_label, error) => {
    expect(isUniqueViolation(error)).toBe(false);
  });
});
