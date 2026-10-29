import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { clearLocalAuthSession } from "@/auth/localAuth";
import { caliberApi } from "@/api/caliberApi";
import { setActiveProjectId } from "@/workspace/activeWorkspace";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

function envelope<T>(data: T): { data: T } {
  return { data };
}

describe("workflow runtime lineage API contract", () => {
  beforeEach(() => {
    clearLocalAuthSession();
    setActiveProjectId("project-1");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    clearLocalAuthSession();
    setActiveProjectId(null);
  });

  it("reads project environments and approved releases with the scoped query", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify(envelope({ environments: [] })), {
          status: 200,
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify(envelope([])), { status: 200 }),
      );
    vi.stubGlobal("fetch", fetchMock);

    await caliberApi.listProjectEnvironments("project/1");
    await caliberApi.listWorkspaceReleases("project/1", {
      status: "approved",
      limit: 200,
    });

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      `${API_BASE}/projects/project%2F1/environments`,
      expect.objectContaining({ method: "GET" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      `${API_BASE}/projects/project%2F1/releases?status=approved&limit=200`,
      expect.objectContaining({ method: "GET" }),
    );
  });

  it("includes the selected lineage IDs in version runs and event triggers", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify(
            envelope({ workflow_run_id: "run-1", status: "queued" }),
          ),
          {
            status: 200,
          },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify(
            envelope({ workflow_run_id: "run-2", status: "queued" }),
          ),
          {
            status: 200,
          },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    await caliberApi.runWorkflowVersion(
      "version-1",
      "input",
      undefined,
      "manual",
      undefined,
      { workspace_release_id: "release-1", environment_id: "environment-1" },
    );
    await caliberApi.triggerWorkflowEvent("workflow-1", {
      alias: "prod",
      workspace_release_id: "release-1",
      environment_id: "environment-1",
    });

    const firstBody = JSON.parse(
      String((fetchMock.mock.calls[0] as [string, RequestInit])[1].body),
    );
    const secondBody = JSON.parse(
      String((fetchMock.mock.calls[1] as [string, RequestInit])[1].body),
    );
    expect(firstBody).toMatchObject({
      workspace_release_id: "release-1",
      environment_id: "environment-1",
    });
    expect(secondBody).toMatchObject({
      alias: "prod",
      workspace_release_id: "release-1",
      environment_id: "environment-1",
    });
  });
});
