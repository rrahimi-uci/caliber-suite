/**
 * `/health` is the one route with a bare body. These pin the client side of that
 * contract against the server's real shape (`routes/health.py`):
 * `{status, version, db}` on 200, and the same shape with `status: "degraded"` on 503.
 * The shell indicator once read "API or database unreachable" permanently because
 * `getHealth` unwrapped `.data` from a body that has none, and the mock hid it by
 * wrapping the response in an envelope.
 */
import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import { ApiError, caliberApi } from "@/api/caliberApi";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("caliberApi.getHealth", () => {
  it("returns the bare body the server sends", async () => {
    server.use(
      http.get(`${API_BASE}/health`, () =>
        HttpResponse.json({ status: "ok", version: "0.1.0a1", db: "ok" }),
      ),
    );
    await expect(caliberApi.getHealth()).resolves.toEqual({
      status: "ok",
      version: "0.1.0a1",
      db: "ok",
    });
  });

  it("rejects with the status when the database is down (503)", async () => {
    server.use(
      http.get(`${API_BASE}/health`, () =>
        HttpResponse.json(
          { status: "degraded", version: "0.1.0a1", db: "down" },
          { status: 503 },
        ),
      ),
    );
    await expect(caliberApi.getHealth()).rejects.toMatchObject({
      name: "ApiError",
      status: 503,
    });
  });

  it("rejects with status 0 on a network failure", async () => {
    server.use(http.get(`${API_BASE}/health`, () => HttpResponse.error()));
    const error = await caliberApi.getHealth().catch((err: unknown) => err);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(0);
  });
});

describe("caliberApi.getProviderReadiness", () => {
  const payload = {
    mode: "simulated",
    real: [],
    simulated: ["eval", "promoter"],
    providers: {},
    warnings: [],
    ready: false,
    blockers: ["promoter is simulated"],
  };

  it("returns the readiness payload from a 503 that carries one", async () => {
    server.use(
      http.get(`${API_BASE}/readiness`, () =>
        HttpResponse.json({ data: payload }, { status: 503 }),
      ),
    );
    await expect(caliberApi.getProviderReadiness()).resolves.toEqual(payload);
  });

  it("returns the payload on a normal 200", async () => {
    server.use(
      http.get(`${API_BASE}/readiness`, () => HttpResponse.json({ data: payload })),
    );
    await expect(caliberApi.getProviderReadiness()).resolves.toEqual(payload);
  });

  it("still rejects a 503 that has no readiness payload", async () => {
    server.use(
      http.get(`${API_BASE}/readiness`, () =>
        HttpResponse.json({ error: "down" }, { status: 503 }),
      ),
    );
    await expect(caliberApi.getProviderReadiness()).rejects.toMatchObject({
      name: "ApiError",
      status: 503,
    });
  });
});

describe("caliberApi.putSecret", () => {
  it("PUTs the value to /secrets/{name} with the name in the path", async () => {
    let seen: { method: string; url: string; body: unknown } | null = null;
    server.use(
      http.put(`${API_BASE}/secrets/:name`, async ({ request, params }) => {
        seen = {
          method: request.method,
          url: new URL(request.url).pathname,
          body: await request.json(),
        };
        return HttpResponse.json({ data: { name: params.name, version: 2 } });
      }),
    );
    await expect(caliberApi.putSecret("my key/1", "s3cret")).resolves.toEqual({
      name: "my key/1",
      version: 2,
    });
    expect(seen).toEqual({
      method: "PUT",
      url: `${API_BASE}/secrets/my%20key%2F1`,
      body: { value: "s3cret" },
    });
  });
});
