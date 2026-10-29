import { describe, expect, it } from "vitest";

import {
  buildApprovedWorkflowRunLineageOptions,
  selectedWorkflowRunLineageOption,
  WORKFLOW_LINEAGE_EMPTY_MESSAGE,
  WORKFLOW_LINEAGE_SELECTION_MESSAGE,
} from "@/lib/workflowRunLineage";
import type {
  WorkspaceEnvironment,
  WorkspaceRelease,
} from "@/api/workflowTypes";

const environment = (
  overrides: Partial<WorkspaceEnvironment> = {},
): WorkspaceEnvironment => ({
  environment_id: "env-prod",
  project_id: "project-1",
  name: "Production",
  environment_class: "prod",
  promotion_order: 2,
  status: "active",
  recovery_policy_enabled: false,
  current_release_id: "release-prod",
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
});

const release = (
  overrides: Partial<WorkspaceRelease> = {},
): WorkspaceRelease => ({
  release_id: "release-prod",
  project_id: "project-1",
  revision_id: "revision-1",
  environment_id: "env-prod",
  change_request_id: null,
  change_request_head_id: null,
  version_tag_id: null,
  predecessor_release_id: null,
  environment_config_sha256: "config",
  runtime_dependencies_sha256: "runtime",
  policy_sha256: "policy",
  request_idempotency_key: "request-1",
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
});

describe("workflow runtime lineage selection", () => {
  it("offers only active environments whose current pointer is an approved matching release", () => {
    const options = buildApprovedWorkflowRunLineageOptions(
      "project-1",
      [
        environment(),
        environment({
          environment_id: "env-disabled",
          name: "Disabled",
          status: "disabled",
          current_release_id: "release-disabled",
          promotion_order: 1,
        }),
        environment({
          environment_id: "env-stale",
          name: "Stale",
          current_release_id: "release-rejected",
          promotion_order: 3,
        }),
        environment({
          environment_id: "env-foreign",
          name: "Foreign",
          project_id: "project-2",
          current_release_id: "release-foreign",
          promotion_order: 0,
        }),
      ],
      [
        release(),
        release({
          release_id: "release-disabled",
          environment_id: "env-disabled",
        }),
        release({
          release_id: "release-rejected",
          environment_id: "env-stale",
          status: "rejected",
        }),
        release({
          release_id: "release-foreign",
          project_id: "project-2",
          environment_id: "env-foreign",
        }),
      ],
    );

    expect(options).toEqual([
      expect.objectContaining({
        workspace_release_id: "release-prod",
        environment_id: "env-prod",
        environment_label: "Production",
      }),
    ]);
  });

  it("sorts eligible pairs by promotion order and preserves an exact selection", () => {
    const options = buildApprovedWorkflowRunLineageOptions(
      "project-1",
      [
        environment({
          environment_id: "env-prod",
          name: "Production",
          promotion_order: 2,
        }),
        environment({
          environment_id: "env-dev",
          name: "Development",
          environment_class: "dev",
          promotion_order: 1,
          current_release_id: "release-dev",
        }),
      ],
      [
        release(),
        release({ release_id: "release-dev", environment_id: "env-dev" }),
      ],
    );

    expect(options.map((option) => option.workspace_release_id)).toEqual([
      "release-dev",
      "release-prod",
    ]);
    expect(
      selectedWorkflowRunLineageOption(options, "release-dev", "env-dev"),
    ).toEqual(options[0]);
    expect(
      selectedWorkflowRunLineageOption(options, "release-prod", "env-dev"),
    ).toBeNull();
  });

  it("uses environment and release labels as deterministic sort tie-breakers", () => {
    const options = buildApprovedWorkflowRunLineageOptions(
      "project-1",
      [
        environment({
          environment_id: "env-z",
          name: "Same",
          current_release_id: "release-z",
          promotion_order: 1,
        }),
        environment({
          environment_id: "env-b",
          name: "Beta",
          current_release_id: "release-b",
          promotion_order: 1,
        }),
        environment({
          environment_id: "env-a",
          name: "Same",
          current_release_id: "release-a",
          promotion_order: 1,
        }),
      ],
      [
        release({ release_id: "release-z", environment_id: "env-z" }),
        release({ release_id: "release-b", environment_id: "env-b" }),
        release({ release_id: "release-a", environment_id: "env-a" }),
      ],
    );

    expect(options.map((option) => option.workspace_release_id)).toEqual([
      "release-b",
      "release-a",
      "release-z",
    ]);
  });

  it("keeps stable refusal messages for empty and incomplete selections", () => {
    expect(WORKFLOW_LINEAGE_EMPTY_MESSAGE).toContain("lineage_required");
    expect(WORKFLOW_LINEAGE_SELECTION_MESSAGE).toContain("lineage_required");
    expect(selectedWorkflowRunLineageOption([], null, null)).toBeNull();
  });
});
