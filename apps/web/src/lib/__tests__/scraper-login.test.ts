import { describe, expect, it } from "vitest";

import {
  isSignInPending,
  parseSignInStatus,
  parseSupportedSignIns,
  pruneSignInWatches,
  signInProgressText,
} from "@/lib/scraper-login";

describe("parseSupportedSignIns", () => {
  it("keeps only well-formed entries", () => {
    const parsed = parseSupportedSignIns({
      logins: [
        { institution: "fintual", method: "code" },
        { institution: "mercadopago", method: "teleport" },
        { method: "browser" },
        "bci_lider",
      ],
    });

    expect(parsed).toEqual([{ institution: "fintual", method: "code" }]);
  });

  it.each([[null], [{}], [{ logins: "fintual" }]])("reads %j as none", (data) => {
    expect(parseSupportedSignIns(data)).toEqual([]);
  });
});

describe("parseSignInStatus", () => {
  it("accepts a status body", () => {
    const body = { institution: "fintual", method: "code", state: "awaiting_code" };

    expect(parseSignInStatus(body)).toEqual(body);
  });

  it.each([[{ error: "nope" }], [{ institution: "fintual", method: "code", state: "done" }]])(
    "rejects %j",
    (data) => {
      expect(parseSignInStatus(data)).toBeNull();
    }
  );
});

describe("isSignInPending", () => {
  it.each([
    ["running", true],
    ["awaiting_code", true],
    ["idle", false],
    ["succeeded", false],
    ["failed", false],
  ] as const)("%s -> %s", (state, pending) => {
    expect(isSignInPending(state)).toBe(pending);
  });
});

describe("signInProgressText", () => {
  it("names the service's failure message", () => {
    const text = signInProgressText({
      institution: "mercadopago",
      method: "browser",
      state: "failed",
      message: "Sign-in not detected in time.",
    });

    expect(text).toBe("No se pudo iniciar sesión: Sign-in not detected in time.");
  });

  it("tells a browser sign-in where to sign in", () => {
    const text = signInProgressText({
      institution: "mercadopago",
      method: "browser",
      state: "running",
    });

    expect(text).toContain("ventana de Chrome");
  });
});

describe("pruneSignInWatches", () => {
  const since = Date.parse("2026-01-01T12:00:00Z");
  const run = (started_at: string, finished_at: string | null) => ({
    institution: "fintual",
    started_at,
    finished_at,
  });

  it.each([
    ["a finished run started after the sign-in", run("2026-01-01T12:00:30Z", "2026-01-01T12:01:00Z"), since, {}],
    ["a run still in progress", run("2026-01-01T12:00:30Z", null), since, { fintual: since }],
    ["the old needs_sign_in run", run("2026-01-01T11:00:00Z", "2026-01-01T11:01:00Z"), since, { fintual: since }],
    ["a watch past its limit", run("2026-01-01T11:00:00Z", "2026-01-01T11:01:00Z"), since + 61_000, {}],
  ])("%s", (_label, latest, now, expected) => {
    expect(pruneSignInWatches({ fintual: since }, [latest], now, 60_000)).toEqual(expected);
  });
});
