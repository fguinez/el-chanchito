import { NextRequest } from "next/server";
import { describe, expect, it, vi } from "vitest";

import { DEFAULT_SHARED_RATIO } from "@/lib/fixed-expenses";

// The insert chain echoes back whatever the route would have written, so the
// assertions see the exact row values without a database.
vi.mock("@/lib/db", () => ({
  db: {
    insert: () => ({
      values: (row: Record<string, unknown>) => ({
        returning: async () => [row],
      }),
    }),
  },
}));

const { POST } = await import("@/app/api/fixed-expenses/route");

function createRequest(body: Record<string, unknown>) {
  return new NextRequest(new URL("http://localhost/api/fixed-expenses"), {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

describe("POST /api/fixed-expenses shared ratio", () => {
  it.each([
    ["shared without a ratio", { isShared: true }, DEFAULT_SHARED_RATIO],
    ["shared with a ratio", { isShared: true, sharedRatio: 0.5 }, "0.5"],
    ["not shared", { isShared: false, sharedRatio: 0.5 }, null],
  ])("%s -> %j", async (_case, fields, expected) => {
    const res = await POST(
      createRequest({ name: "Arriendo", amount: 999999, ...fields })
    );

    expect(res.status).toBe(201);
    expect((await res.json()).sharedRatio).toBe(expected);
  });
});
