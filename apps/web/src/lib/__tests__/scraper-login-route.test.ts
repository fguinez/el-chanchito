import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GET as list } from "@/app/api/scrapers/login/route";
import { GET as status, POST as start } from "@/app/api/scrapers/login/[institution]/route";
import { POST as submitCode } from "@/app/api/scrapers/login/[institution]/code/route";

// The sign-in routes open windows and mail codes on the scraper service, so a
// request they cannot trust must never reach it. The control server is a
// fetch double.

const CONTROL_URL = "http://scrapers.test:8080";

const fetchMock = vi.fn();
const originalControlUrl = process.env.SCRAPER_CONTROL_URL;

function post(path: string, body?: string, contentType?: string) {
  return new NextRequest(new URL(`http://localhost:3000${path}`), {
    method: "POST",
    headers: contentType ? { "content-type": contentType } : {},
    body: body === undefined ? undefined : new TextEncoder().encode(body),
  });
}

const params = (institution: string) => ({ params: Promise.resolve({ institution }) });

beforeEach(() => {
  process.env.SCRAPER_CONTROL_URL = CONTROL_URL;
  fetchMock.mockReset();
  fetchMock.mockResolvedValue(
    Response.json({ institution: "fintual", method: "code", state: "running" }, { status: 202 })
  );
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  if (originalControlUrl === undefined) delete process.env.SCRAPER_CONTROL_URL;
  else process.env.SCRAPER_CONTROL_URL = originalControlUrl;
});

describe("GET /api/scrapers/login", () => {
  it("lists the service's sign-ins", async () => {
    fetchMock.mockResolvedValue(
      Response.json({ logins: [{ institution: "mercadopago", method: "browser" }] })
    );

    const response = await list();

    expect(await response.json()).toEqual({
      logins: [{ institution: "mercadopago", method: "browser" }],
    });
  });

  it("degrades to none when the service is down", async () => {
    fetchMock.mockRejectedValue(new Error("ECONNREFUSED"));

    const response = await list();

    expect([response.status, await response.json()]).toEqual([200, { logins: [] }]);
  });
});

describe("POST /api/scrapers/login/{institution}", () => {
  it("starts the sign-in with a JSON body", async () => {
    const response = await start(
      post("/api/scrapers/login/fintual", "{}", "application/json"),
      params("fintual")
    );

    expect(response.status).toBe(202);
    expect(fetchMock).toHaveBeenCalledWith(
      `${CONTROL_URL}/login/fintual`,
      expect.objectContaining({
        method: "POST",
        headers: { "content-type": "application/json" },
      })
    );
  });

  it.each([["text/plain;charset=UTF-8"], ["application/x-www-form-urlencoded"], [undefined]])(
    "answers 415 to a %s body and starts nothing",
    async (contentType) => {
      const response = await start(
        post("/api/scrapers/login/fintual", "{}", contentType),
        params("fintual")
      );

      expect(response.status).toBe(415);
      expect(fetchMock).not.toHaveBeenCalled();
    }
  );

  it("refuses a slug that could reach another control route", async () => {
    const response = await start(
      post("/api/scrapers/login/x", "{}", "application/json"),
      params("../refresh")
    );

    expect(response.status).toBe(400);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("passes the service's 409 through", async () => {
    fetchMock.mockResolvedValue(Response.json({ error: "already in progress" }, { status: 409 }));

    const response = await start(
      post("/api/scrapers/login/fintual", "{}", "application/json"),
      params("fintual")
    );

    expect(response.status).toBe(409);
  });

  it("answers 503 when the service is not configured", async () => {
    delete process.env.SCRAPER_CONTROL_URL;

    const response = await start(
      post("/api/scrapers/login/fintual", "{}", "application/json"),
      params("fintual")
    );

    expect(response.status).toBe(503);
  });
});

describe("GET /api/scrapers/login/{institution}", () => {
  it("reports the sign-in", async () => {
    const response = await status(new Request("http://localhost:3000"), params("fintual"));

    expect(fetchMock).toHaveBeenCalledWith(
      `${CONTROL_URL}/login/fintual`,
      expect.objectContaining({ method: "GET" })
    );
    expect((await response.json()).state).toBe("running");
  });
});

describe("POST /api/scrapers/login/{institution}/code", () => {
  it("forwards a 6-digit code", async () => {
    await submitCode(
      post("/api/scrapers/login/fintual/code", '{"code":" 000000 "}', "application/json"),
      params("fintual")
    );

    expect(fetchMock).toHaveBeenCalledWith(
      `${CONTROL_URL}/login/fintual/code`,
      expect.objectContaining({ body: JSON.stringify({ code: "000000" }) })
    );
  });

  it.each([['{"code":"12345"}'], ['{"code":123456}'], ["{}"]])(
    "answers 400 to %s and forwards nothing",
    async (body) => {
      const response = await submitCode(
        post("/api/scrapers/login/fintual/code", body, "application/json"),
        params("fintual")
      );

      expect(response.status).toBe(400);
      expect(fetchMock).not.toHaveBeenCalled();
    }
  );

  it("answers 415 to a body not labelled JSON", async () => {
    const response = await submitCode(
      post("/api/scrapers/login/fintual/code", '{"code":"000000"}', "text/plain"),
      params("fintual")
    );

    expect(response.status).toBe(415);
  });
});
