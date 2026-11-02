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

import type { AdapterCapability } from "@/api/workflowTypes";
import {
  FEATURE_FALLBACKS,
  useAdapterSupport,
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

/** An ``adapters`` entry as ``caliber/features.py`` sends it. */
function adapter(
  status: AdapterCapability["status"],
  extra: Partial<AdapterCapability> = {},
): AdapterCapability {
  return {
    status,
    kind: "connector",
    label: "OpenAPI document-request connector",
    reason_code: status === "supported" ? null : "not_reference_profile",
    disabled_by: status === "supported" ? null : "CALIBER_PROFILE",
    message: `message for ${status}`,
    evidence:
      status === "supported" ? ["tests/egi_scenarios/x.py::test_x"] : [],
    ...extra,
  };
}

describe("useAdapterSupport", () => {
  it("shows supported, preview and unavailable from the server's adapters", async () => {
    server.use(
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json(
          capabilitiesBody(
            { integrations: true },
            {
              document_request_connector: adapter("supported"),
              mcp_servers: adapter("preview", {
                kind: "mcp_server",
                reason_code: "no_support_evidence",
                disabled_by: "profile",
              }),
              release_evaluation: adapter("unavailable", {
                kind: "release_evaluation",
                reason_code: "feature_disabled",
                disabled_by: "CALIBER_FEATURE_RELEASE_CANDIDATES",
              }),
            },
          ),
        ),
      ),
    );
    const { result } = renderHook(
      () => ({
        connector: useAdapterSupport("document_request_connector"),
        mcp: useAdapterSupport("mcp_servers"),
        evaluation: useAdapterSupport("release_evaluation"),
        flags: useFeature("integrations"),
      }),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.connector.loading).toBe(false));
    expect(result.current.connector.status).toBe("supported");
    expect(result.current.connector.info?.evidence).toHaveLength(1);
    expect(result.current.mcp.status).toBe("preview");
    expect(result.current.mcp.info?.reason_code).toBe("no_support_evidence");
    expect(result.current.evaluation.status).toBe("unavailable");
    expect(result.current.evaluation.info?.disabled_by).toBe(
      "CALIBER_FEATURE_RELEASE_CANDIDATES",
    );
    // A flag being on is not support: the area is on, the MCP adapter is still preview.
    expect(result.current.flags.enabled).toBe(true);
    expect(result.current.mcp.status).not.toBe("supported");
  });

  it("never reads supported while loading, on failure, or from a server that predates adapters", async () => {
    server.use(
      http.get(`${API_BASE}/capabilities`, async () => {
        await delay(50);
        return HttpResponse.json(capabilitiesBody({}));
      }),
    );
    const loading = renderHook(
      () => useAdapterSupport("document_request_connector"),
      { wrapper: makeWrapper() },
    );
    expect(loading.result.current.loading).toBe(true);
    expect(loading.result.current.status).toBe("preview");
    await waitFor(() => expect(loading.result.current.loading).toBe(false));
    // The body has no ``adapters``: an older server.
    expect(loading.result.current.status).toBe("preview");
    expect(loading.result.current.info).toBeNull();

    server.use(
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json(
          { detail: "boom", status_code: 500 },
          { status: 500 },
        ),
      ),
    );
    const failed = renderHook(() => useAdapterSupport("release_evaluation"), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(failed.result.current.loading).toBe(false));
    expect(failed.result.current.status).toBe("preview");
    expect(failed.result.current.info).toBeNull();
  });

  it("reads a malformed or unknown-status entry as preview, never supported", async () => {
    server.use(
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json({
          data: {
            adapters: {
              document_request_connector: {
                ...adapter("supported"),
                status: "SUPPORTED",
              },
              release_evaluation: null,
              mcp_servers: "supported",
            },
            features: {},
            workflow_runs: {},
            sync_workflow_version_run: true,
          },
        }),
      ),
    );
    const { result } = renderHook(
      () => ({
        connector: useAdapterSupport("document_request_connector"),
        evaluation: useAdapterSupport("release_evaluation"),
        mcp: useAdapterSupport("mcp_servers"),
      }),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.connector.loading).toBe(false));
    for (const state of Object.values(result.current)) {
      expect(state.status).toBe("preview");
      expect(state.info).toBeNull();
    }
  });
});
