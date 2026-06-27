import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getActiveProjectId, setActiveProjectId } from "@/workspace/activeWorkspace";

function ok<T>(data: T): Response {
  return new Response(JSON.stringify({ data }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

const READINESS = {
  providers: { llm: "fake", eval: "fake", promoter: "fake", artifact_store: "fake" },
  simulated: ["llm"],
  all_real: false,
  tracing_enabled: true,
  tracing_autolog_enabled: true,
  workflow_llm_judge_enabled: false,
};

async function loadApi(): Promise<typeof import("@/api/caliberApi")> {
  vi.resetModules();
  return import("@/api/caliberApi");
}

describe("activeWorkspace", () => {
  beforeEach(() => setActiveProjectId(null));
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    setActiveProjectId(null);
  });

  it("persists, reads, and clears the active project id", () => {
    expect(getActiveProjectId()).toBeNull();
    setActiveProjectId("PRJ-1");
    expect(getActiveProjectId()).toBe("PRJ-1");
    expect(window.localStorage.getItem("caliber.active_project_id")).toBe("PRJ-1");
    setActiveProjectId(null);
    expect(getActiveProjectId()).toBeNull();
  });

  it("dispatches a workspace-changed event on set", () => {
    const handler = vi.fn();
    window.addEventListener("caliber-workspace-changed", handler);
    setActiveProjectId("PRJ-2");
    expect(handler).toHaveBeenCalled();
    window.removeEventListener("caliber-workspace-changed", handler);
  });

  it("injects X-CALIBER-Project header when a workspace is active", async () => {
    const fetchMock = vi.fn().mockResolvedValue(ok(READINESS));
    vi.stubGlobal("fetch", fetchMock);
    setActiveProjectId("PRJ-9");

    const api = await loadApi();
    await api.caliberApi.getProviderReadiness();

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect((init.headers as Record<string, string>)["X-CALIBER-Project"]).toBe("PRJ-9");
  });

  it("omits X-CALIBER-Project header when no workspace is active", async () => {
    const fetchMock = vi.fn().mockResolvedValue(ok(READINESS));
    vi.stubGlobal("fetch", fetchMock);
    setActiveProjectId(null);

    const api = await loadApi();
    await api.caliberApi.getProviderReadiness();

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect((init.headers as Record<string, string>)["X-CALIBER-Project"]).toBeUndefined();
  });
});
