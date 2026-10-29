import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { caliberApi } from "@/api/caliberApi";
import type {
  WorkspaceEnvironment,
  WorkspaceRelease,
} from "@/api/workflowTypes";
import { useWorkflowRunLineage } from "@/hooks/useWorkflowRunLineage";

const environment = (
  overrides: Partial<WorkspaceEnvironment> = {},
): WorkspaceEnvironment =>
  ({
    environment_id: "env-dev",
    project_id: "project-1",
    name: "Development",
    environment_class: "dev",
    promotion_order: 1,
    status: "active",
    recovery_policy_enabled: false,
    current_release_id: "release-dev",
    pending_operation_id: null,
    operation_state: "idle",
    policy_sha256: "policy",
    lock_version: 1,
    created_by: "operator",
    created_at: null,
    updated_at: null,
    access_role: "owner",
    permissions: [],
    ...overrides,
  }) as WorkspaceEnvironment;

const release = (overrides: Partial<WorkspaceRelease> = {}): WorkspaceRelease =>
  ({
    release_id: "release-dev",
    project_id: "project-1",
    revision_id: "revision-dev",
    environment_id: "env-dev",
    change_request_id: null,
    change_request_head_id: null,
    version_tag_id: null,
    predecessor_release_id: null,
    environment_config_sha256: "config",
    runtime_dependencies_sha256: "runtime",
    policy_sha256: "policy",
    request_idempotency_key: "request-dev",
    evaluation_evidence_sha256: null,
    decision_set_sha256: null,
    status: "approved",
    requested_by: "operator",
    requested_at: null,
    evaluated_by: "operator",
    evaluated_at: null,
    lock_version: 1,
    error_code: null,
    error_summary: null,
    created_at: null,
    updated_at: null,
    ...overrides,
  }) as WorkspaceRelease;

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

afterEach(() => vi.restoreAllMocks());

describe("useWorkflowRunLineage", () => {
  it("loads approved pairs and updates the selected pair explicitly", async () => {
    const environments = [
      environment(),
      environment({
        environment_id: "env-prod",
        name: "Production",
        environment_class: "prod",
        promotion_order: 2,
        current_release_id: "release-prod",
      }),
    ];
    const releases = [
      release(),
      release({
        release_id: "release-prod",
        revision_id: "revision-prod",
        environment_id: "env-prod",
        request_idempotency_key: "request-prod",
      }),
    ];
    vi.spyOn(caliberApi, "listProjectEnvironments").mockResolvedValue({
      environments,
    });
    vi.spyOn(caliberApi, "listWorkspaceReleases").mockResolvedValue(releases);

    const { result } = renderHook(() => useWorkflowRunLineage("project-1"), {
      wrapper: makeWrapper(),
    });

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.options).toHaveLength(2);
    expect(result.current.selectedOption?.workspace_release_id).toBe(
      "release-dev",
    );

    act(() => result.current.selectOption(result.current.options[1]!));
    expect(result.current.selectedWorkspaceReleaseId).toBe("release-prod");
    expect(result.current.selectedEnvironmentId).toBe("env-prod");
    expect(result.current.selectedOption?.environment_id).toBe("env-prod");
  });
});
