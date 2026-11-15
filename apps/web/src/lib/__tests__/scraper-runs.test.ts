import { describe, it, expect } from "vitest";
import {
  hasRunDetails,
  runStatusStyle,
  truncateRunMessage,
} from "@/lib/scraper-runs";

describe("runStatusStyle", () => {
  it("gives partial runs their own label and color", () => {
    const partial = runStatusStyle("partial");

    expect(partial.label).toBe("parcial");
    expect(partial.badgeClass).not.toBe(runStatusStyle("success").badgeClass);
    expect(partial.badgeClass).not.toBe(runStatusStyle("error").badgeClass);
  });

  it("gives runs that need a sign-in their own label and color", () => {
    const needsSignIn = runStatusStyle("needs_sign_in");

    expect(needsSignIn.label).toBe("inicia sesión");
    for (const other of ["success", "partial", "error", "running"]) {
      expect(needsSignIn.badgeClass).not.toBe(runStatusStyle(other).badgeClass);
    }
  });

  it("gives skipped runs a neutral label and color of their own", () => {
    const skipped = runStatusStyle("skipped");

    expect(skipped.label).toBe("omitido");
    const others = ["success", "partial", "error", "needs_sign_in", "running"];
    for (const other of others) {
      expect(skipped.badgeClass).not.toBe(runStatusStyle(other).badgeClass);
    }
  });

  it("shows an unknown status as-is, unstyled", () => {
    expect(runStatusStyle("constructor")).toEqual({
      label: "constructor",
      badgeClass: "",
    });
  });
});

describe("hasRunDetails", () => {
  it.each([
    ["error", "products: login failed", true],
    ["needs_sign_in", "Stub needs sign-in: run `make stub-login`.", true],
    ["partial", "transactions: 1 of 2 not written", true],
    ["skipped", "full login skipped (allowed again in 540s)", true],
    ["partial", null, false],
    ["success", null, false],
    ["success", "retired: 1 mercadopago savings CLP product(s) no longer listed", true],
    ["running", null, false],
    ["running", "products: still reading", false],
  ])("%s with message %j -> %s", (status, error_message, expected) => {
    expect(hasRunDetails({ status, error_message })).toBe(expected);
  });
});

describe("truncateRunMessage", () => {
  it("keeps a short message whole and cuts a long one", () => {
    expect(truncateRunMessage("ok", 5)).toBe("ok");
    expect(truncateRunMessage("abcdefgh", 5)).toBe("abcde...");
  });
});
