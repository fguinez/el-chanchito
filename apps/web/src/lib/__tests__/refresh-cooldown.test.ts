import { describe, expect, it } from "vitest";
import { refreshCooldownNotice } from "@/lib/refresh-cooldown";

// Response bodies mirror the scraper control server's contract
// (apps/scrapers/main.py `_make_control_handler`).

describe("refreshCooldownNotice", () => {
  it("explains a refused single-institution refresh in minutes", () => {
    const body = {
      error: "banchile is cooling down; retry in 420s",
      skipped: [{ slug: "banchile", retry_after_seconds: 420 }],
    };
    expect(refreshCooldownNotice(429, body)).toBe(
      "Banco de Chile se consultó hace poco; para evitar el bloqueo del banco, se podrá actualizar de nuevo en 7 minutos."
    );
  });

  it("reports what a refresh-all skipped", () => {
    const body = {
      triggered: ["buda", "fintual"],
      skipped: [{ slug: "banchile", retry_after_seconds: 600 }],
    };
    expect(refreshCooldownNotice(202, body)).toBe(
      "Banco de Chile se consultó hace poco; para evitar el bloqueo del banco, se podrá actualizar de nuevo en 10 minutos."
    );
  });

  it.each([
    [1, "1 minuto"],
    [60, "1 minuto"],
    [61, "2 minutos"],
  ])("rounds %i s up to %s so nobody retries too early", (seconds, minutes) => {
    const body = { skipped: [{ slug: "banchile", retry_after_seconds: seconds }] };
    expect(refreshCooldownNotice(429, body)).toContain(`de nuevo en ${minutes}.`);
  });

  it("falls back to the slug for an institution without a display name", () => {
    const body = { skipped: [{ slug: "nuevo_banco", retry_after_seconds: 120 }] };
    expect(refreshCooldownNotice(429, body)).toMatch(/^nuevo_banco se consultó/);
  });

  it.each([
    ["nothing skipped", { triggered: ["banchile"], skipped: [] }],
    ["a server without cooldowns", { triggered: ["banchile"] }],
  ])("is null for an accepted refresh with %s", (_, body) => {
    expect(refreshCooldownNotice(202, body)).toBeNull();
  });

  it.each([
    ["no body", null],
    ["a malformed skip list", { skipped: [{ slug: "banchile" }, "banchile"] }],
  ])("still explains a 429 with %s", (_, body) => {
    expect(refreshCooldownNotice(429, body)).toBe(
      "Esta institución se consultó hace poco; inténtalo de nuevo en unos minutos."
    );
  });
});
