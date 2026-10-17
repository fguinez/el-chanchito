import { NextRequest } from "next/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { TransferSuggestion } from "@/lib/transfer-suggestions";

// GET /api/transfers/suggestions hands the scored list over as
// `{ suggestions }`, narrows it to one row with `?transactionId=`, and hides a
// failure behind a 500. The query module is
// mocked; its SQL runs against a real PostgreSQL in
// transfer-suggestions.db.test.ts. Every id, name and amount is synthetic
// (see the repo's personal data policy).

const loadTransferSuggestions = vi.fn<() => Promise<TransferSuggestion[]>>();
vi.mock("@/lib/transfer-suggestions-db", () => ({ loadTransferSuggestions }));

const { GET } = await import("@/app/api/transfers/suggestions/route");

const suggestion: TransferSuggestion = {
  id: "pair:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa:bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
  kind: "pair",
  amount: 1_000_000,
  score: 85,
  evidence: ["Mismo monto", "Mismo día", "Glosa 'Pago TC 1234'"],
  source: {
    transactionId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    productId: "11111111-1111-4111-8111-111111111111",
    productName: "Cuenta corriente",
    productSlug: "cuenta-corriente",
    institutionName: "Banco de Prueba",
    institutionSlug: "banco-de-prueba",
    date: "2026-09-15",
    description: "Pago TC 1234",
    amount: -1_000_000,
    currency: "CLP",
  },
  target: {
    transactionId: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    productId: "22222222-2222-4222-8222-222222222222",
    productName: "Tarjeta 1234",
    productSlug: "tarjeta-1234",
    institutionName: "Banco de Prueba",
    institutionSlug: "banco-de-prueba",
    date: "2026-09-15",
    description: "Pago recibido",
    amount: 1_000_000,
    currency: "CLP",
  },
};

function get(query = "") {
  return GET(
    new NextRequest(
      new URL(`https://dashboard.example/api/transfers/suggestions${query}`)
    )
  );
}

beforeEach(() => {
  loadTransferSuggestions.mockReset();
});

describe("GET /api/transfers/suggestions", () => {
  it("returns the suggestions in the order the module gives them", async () => {
    loadTransferSuggestions.mockResolvedValue([suggestion]);

    const response = await get();

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual({ suggestions: [suggestion] });
  });

  it.each([
    ["the source", "?transactionId=AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA", 1],
    ["the target", "?transactionId=bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", 1],
    ["another row", "?transactionId=cccccccc-cccc-4ccc-8ccc-cccccccccccc", 0],
  ])("filters by %s row", async (_, query, count) => {
    loadTransferSuggestions.mockResolvedValue([suggestion]);

    const response = await get(query);

    expect(response.status).toBe(200);
    const json = await response.json();
    expect(json.suggestions).toHaveLength(count);
  });

  it("rejects a malformed transactionId before querying", async () => {
    const response = await get("?transactionId=cargo-1");

    expect(response.status).toBe(400);
    await expect(response.json()).resolves.toMatchObject({
      field: "transactionId",
    });
    expect(loadTransferSuggestions).not.toHaveBeenCalled();
  });

  it("answers 500 when the query fails", async () => {
    loadTransferSuggestions.mockRejectedValue(new Error("connection refused"));
    vi.spyOn(console, "error").mockImplementation(() => {});

    const response = await get();

    expect(response.status).toBe(500);
    await expect(response.json()).resolves.toEqual({ error: "Internal error" });
  });
});
