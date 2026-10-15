/**
 * The alpha feature flags, as the SPA reads them from ``GET /capabilities``.
 *
 * The property under test is the one that keeps the clean alpha clean: an area is on only when
 * the server said so. Loading, a failed request, a server that predates the flags, and a flag the
 * server does not know all read as off.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { delay, http, HttpResponse } from "msw";
import type { ReactNode } from "react";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import {
  FEATURE_FALLBACKS,
  useFeature,
  useFeatureFlags,
} from "@/hooks/useFeature";
import { capabilitiesBody } from "@/test/handlers";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

/** A fresh cache per test (and per mount), so one test's capabilities never answer the next's. */
function makeWrapper(): ({ children }: { children: ReactNode }) => JSX.Element {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return function Wrapper({ children }) {
    return (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
  };
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("useFeature", () => {
  it("reads every area as off while capabilities are loading", async () => {
    server.use(
      http.get(`${API_BASE}/capabilities`, async () => {
        await delay(50);
        return HttpResponse.json(capabilitiesBody({ aria: true }));
      }),
    );
    const { result } = renderHook(() => useFeature("aria"), {
      wrapper: makeWrapper(),
    });
    expect(result.current.loading).toBe(true);
    expect(result.current.enabled).toBe(false);
    // The fallback still names the variable, so a panel rendered meanwhile is not empty.
    expect(result.current.info.env_var).toBe("CALIBER_FEATURE_ARIA");

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.enabled).toBe(true);
  });

  it("reports an area as enabled only when the server says so", async () => {
    server.use(
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json(capabilitiesBody({ cookbooks: true, aria: false })),
      ),
    );
    const { result } = renderHook(
      () => ({
        cookbooks: useFeature("cookbooks"),
        aria: useFeature("aria"),
        integrations: useFeature("integrations"),
      }),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.cookbooks.loading).toBe(false));
    expect(result.current.cookbooks.enabled).toBe(true);
    expect(result.current.cookbooks.info).toEqual({
      enabled: true,
      env_var: "CALIBER_FEATURE_COOKBOOKS",
      label: "Cookbooks",
      stability: "preview",
    });
    expect(result.current.aria.enabled).toBe(false);
    expect(result.current.integrations.enabled).toBe(false);
  });

  it("reads every area as off when the capabilities request fails", async () => {
    server.use(
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json(
          { detail: "boom", status_code: 500 },
          { status: 500 },
        ),
      ),
    );
    const { result } = renderHook(() => useFeatureFlags(), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.loading).toBe(false));
    for (const id of Object.keys(FEATURE_FALLBACKS) as Array<
      keyof typeof FEATURE_FALLBACKS
    >) {
      expect(result.current.isEnabled(id)).toBe(false);
      expect(result.current.info(id)).toEqual(FEATURE_FALLBACKS[id]);
    }
  });

  it("reads every area as off when the server reports no features (an older server)", async () => {
    server.use(
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json({
          data: {
            workflow_runs: { queue_enabled: true },
            sync_workflow_version_run: true,
          },
        }),
      ),
    );
    const { result } = renderHook(() => useFeatureFlags(), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.isEnabled("aria")).toBe(false);
    expect(result.current.isEnabled("integrations")).toBe(false);
    expect(result.current.isEnabled("cookbooks")).toBe(false);
    expect(result.current.isEnabled("release_candidates")).toBe(false);
  });

  it("reads a flag the server omits, or reports as something other than true, as off", async () => {
    server.use(
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json({
          data: {
            features: {
              aria: { ...FEATURE_FALLBACKS.aria, enabled: "true" },
              integrations: null,
              cookbooks: { ...FEATURE_FALLBACKS.cookbooks, enabled: true },
            },
            workflow_runs: {},
            sync_workflow_version_run: true,
          },
        }),
      ),
    );
    const { result } = renderHook(() => useFeatureFlags(), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.isEnabled("aria")).toBe(false); // truthy string, not ``true``
    expect(result.current.isEnabled("integrations")).toBe(false); // null entry
    expect(result.current.isEnabled("release_candidates")).toBe(false); // omitted
    expect(result.current.isEnabled("cookbooks")).toBe(true);
    expect(result.current.info("release_candidates")).toEqual(
      FEATURE_FALLBACKS.release_candidates,
    );
  });

  it("prefers the server's label and variable over the built-in fallback", async () => {
    server.use(
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json({
          data: {
            features: {
              aria: {
                enabled: false,
                env_var: "CALIBER_FEATURE_ARIA_RENAMED",
                label: "Renamed area",
                stability: "record_only",
              },
            },
            workflow_runs: {},
            sync_workflow_version_run: true,
          },
        }),
      ),
    );
    const { result } = renderHook(() => useFeature("aria"), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.info).toEqual({
      enabled: false,
      env_var: "CALIBER_FEATURE_ARIA_RENAMED",
      label: "Renamed area",
      stability: "record_only",
    });
  });
});
