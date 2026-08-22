import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { loginClientKey } from "@/lib/auth/throttle";

// The point of this file is the wiring: deleting the throttle wrapper from the
// login route used to leave the whole suite green. The throttle module is
// replaced by a recording double, so every assertion here fails the moment the
// handler stops consulting it.

const runMock = vi.fn();

vi.mock("@/lib/auth/throttle", async () => {
  const actual = await vi.importActual<typeof import("@/lib/auth/throttle")>(
    "@/lib/auth/throttle"
  );
  return {
    ...actual,
    loginThrottle: {
      run: (clientKey: string, attempt: () => Promise<unknown>) =>
        runMock(clientKey, attempt),
    },
  };
});

// Obviously fake shared secret; never use a real one in tests.
const PASSWORD = "changeme-example";
const CLIENT_IP = "203.0.113.10";

const { POST } = await import("@/app/api/auth/login/route");

function loginRequest(password: string, headers: Record<string, string> = {}) {
  return new NextRequest(new URL("https://dashboard.example/api/auth/login"), {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-forwarded-for": CLIENT_IP,
      ...headers,
    },
    body: JSON.stringify({ password }),
  });
}

const originalPassword = process.env.DASHBOARD_PASSWORD;
const originalTrustProxy = process.env.DASHBOARD_TRUST_PROXY;

beforeEach(() => {
  runMock.mockReset();
  process.env.DASHBOARD_PASSWORD = PASSWORD;
  delete process.env.DASHBOARD_TRUST_PROXY;
});

afterEach(() => {
  if (originalPassword === undefined) delete process.env.DASHBOARD_PASSWORD;
  else process.env.DASHBOARD_PASSWORD = originalPassword;
  if (originalTrustProxy === undefined) delete process.env.DASHBOARD_TRUST_PROXY;
  else process.env.DASHBOARD_TRUST_PROXY = originalTrustProxy;
});

/** Lets the recorded attempt through, reporting whatever it decided. */
function passThrough() {
  runMock.mockImplementation(
    async (_key: string, attempt: () => Promise<{ ok: boolean; result: unknown }>) => {
      const { ok, result } = await attempt();
      return ok
        ? { status: "ran", result }
        : { status: "ran", result: false };
    }
  );
}

describe("POST /api/auth/login throttle wiring", () => {
  it("runs every password check inside the throttle", async () => {
    passThrough();
    const response = await POST(loginRequest(PASSWORD));

    expect(runMock).toHaveBeenCalledTimes(1);
    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual({ authenticated: true });
  });

  it("scopes the attempt to the calling client", async () => {
    passThrough();
    await POST(loginRequest("wrong-password-example"));

    expect(runMock.mock.calls[0][0]).toBe(CLIENT_IP);
    // Same value the identity helper derives, so the two cannot drift apart.
    expect(runMock.mock.calls[0][0]).toBe(
      loginClientKey(new Headers({ "x-forwarded-for": CLIENT_IP }), false)
    );
  });

  it("answers 429 with Retry-After when the throttle says the client is locked", async () => {
    runMock.mockResolvedValue({
      status: "locked",
      retryAfterSeconds: 42,
      reason: "lockout",
    });

    const response = await POST(loginRequest(PASSWORD));

    expect(response.status).toBe(429);
    expect(response.headers.get("retry-after")).toBe("42");
    // The correct password must not be turned into a session while locked.
    expect(response.headers.get("set-cookie")).toBeNull();
  });

  it("answers 429 for a queue rejection as well", async () => {
    runMock.mockResolvedValue({
      status: "locked",
      retryAfterSeconds: 1,
      reason: "queue",
    });

    const response = await POST(loginRequest(PASSWORD));
    expect(response.status).toBe(429);
    expect(response.headers.get("retry-after")).toBe("1");
  });

  it("reports a wrong password as 401 with no cookie", async () => {
    passThrough();
    const response = await POST(loginRequest("wrong-password-example"));

    expect(response.status).toBe(401);
    expect(response.headers.get("set-cookie")).toBeNull();
    // The verdict came from the attempt the throttle ran, not from the handler.
    expect(runMock).toHaveBeenCalledTimes(1);
  });

  it("sets the host-pinned cookie on a secure request", async () => {
    passThrough();
    const response = await POST(loginRequest(PASSWORD));
    expect(response.headers.get("set-cookie")).toContain(
      "__Host-chanchito_session="
    );
  });

  it("sets the plain cookie when only an untrusted header claims https", async () => {
    passThrough();
    const response = await POST(
      new NextRequest(new URL("http://localhost:3000/api/auth/login"), {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-forwarded-proto": "https",
        },
        body: JSON.stringify({ password: PASSWORD }),
      })
    );

    const cookie = response.headers.get("set-cookie") ?? "";
    expect(cookie).toContain("chanchito_session=");
    expect(cookie).not.toContain("__Host-");
    expect(cookie).not.toContain("Secure");
  });
});
