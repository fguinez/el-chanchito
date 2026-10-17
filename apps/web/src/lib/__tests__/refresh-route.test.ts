import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { POST } from "@/app/api/institutions/refresh/route";

// The refresh route starts live bank logins, so a body it cannot trust must
// never reach the scraper service. The control server is a fetch double.

const CONTROL_URL = "http://scrapers.test:8080";

const fetchMock = vi.fn();
const originalControlUrl = process.env.SCRAPER_CONTROL_URL;

function refresh(body?: string, contentType?: string) {
  return new NextRequest(new URL("http://localhost:3000/api/institutions/refresh"), {
    method: "POST",
    headers: contentType ? { "content-type": contentType } : {},
    body: body === undefined ? undefined : new TextEncoder().encode(body),
  });
}

beforeEach(() => {
  process.env.SCRAPER_CONTROL_URL = CONTROL_URL;
  fetchMock.mockReset();
  fetchMock.mockResolvedValue(Response.json({ triggered: [] }, { status: 202 }));
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  if (originalControlUrl === undefined) delete process.env.SCRAPER_CONTROL_URL;
  else process.env.SCRAPER_CONTROL_URL = originalControlUrl;
});

describe("POST /api/institutions/refresh", () => {
  it("refreshes everything when there is no body", async () => {
    const response = await POST(refresh());

    expect(response.status).toBe(202);
    expect(fetchMock).toHaveBeenCalledWith(`${CONTROL_URL}/refresh`, expect.anything());
  });

  it.each([
    ["{}", "refresh"],
    ['{"institution":"banchile"}', "refresh/banchile"],
  ])("accepts the JSON body %s", async (body, path) => {
    const response = await POST(refresh(body, "application/json; charset=utf-8"));

    expect(response.status).toBe(202);
    expect(fetchMock).toHaveBeenCalledWith(`${CONTROL_URL}/${path}`, expect.anything());
  });

  it.each([
    ["text/plain;charset=UTF-8"],
    ["application/x-www-form-urlencoded"],
    [undefined],
  ])("answers 415 to a %s body and starts nothing", async (contentType) => {
    const response = await POST(refresh('{"institution":"banchile"}', contentType));

    expect(response.status).toBe(415);
    await expect(response.json()).resolves.toEqual({
      error: "Content-Type must be application/json",
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("answers 400 to malformed JSON and starts nothing", async () => {
    const response = await POST(refresh("{not json", "application/json"));

    expect(response.status).toBe(400);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
