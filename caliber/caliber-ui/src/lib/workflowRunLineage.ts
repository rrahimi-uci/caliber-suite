import type {
  WorkspaceEnvironment,
  WorkspaceRelease,
} from "@/api/workflowTypes";

export const WORKFLOW_LINEAGE_EMPTY_MESSAGE =
  "lineage_required: no approved workspace release and environment is available for this project.";
export const WORKFLOW_LINEAGE_SELECTION_MESSAGE =
  "lineage_required: select an approved workspace release and environment before starting a run.";

export interface WorkflowRunLineageOption {
  workspace_release_id: string;
  environment_id: string;
  release_label: string;
  environment_label: string;
  environment_class: string;
  promotion_order: number;
}

/**
 * Join the server's durable environment pointer to an approved release.
 *
 * The join is intentionally exact. A release merely belonging to the project
 * is not executable if it is not the environment's current release, and a
 * current pointer is not executable if the pointed release is not approved.
 */
export function buildApprovedWorkflowRunLineageOptions(
  projectId: string,
  environments: WorkspaceEnvironment[],
  releases: WorkspaceRelease[],
): WorkflowRunLineageOption[] {
  const approvedById = new Map(
    releases
      .filter(
        (release) =>
          release.project_id === projectId && release.status === "approved",
      )
      .map((release) => [release.release_id, release]),
  );

  return environments
    .filter(
      (environment) =>
        environment.project_id === projectId &&
        environment.status === "active" &&
        environment.current_release_id !== null,
    )
    .flatMap((environment) => {
      const release = approvedById.get(environment.current_release_id!);
      if (!release || release.environment_id !== environment.environment_id) {
        return [];
      }
      return [
        {
          workspace_release_id: release.release_id,
          environment_id: environment.environment_id,
          release_label: release.release_id,
          environment_label: environment.name,
          environment_class: environment.environment_class,
          promotion_order: environment.promotion_order,
        },
      ];
    })
    .sort(
      (left, right) =>
        left.promotion_order - right.promotion_order ||
        left.environment_label.localeCompare(right.environment_label) ||
        left.workspace_release_id.localeCompare(right.workspace_release_id),
    );
}

export function selectedWorkflowRunLineageOption(
  options: WorkflowRunLineageOption[],
  workspaceReleaseId: string | null,
  environmentId: string | null,
): WorkflowRunLineageOption | null {
  return (
    options.find(
      (option) =>
        option.workspace_release_id === workspaceReleaseId &&
        option.environment_id === environmentId,
    ) ?? null
  );
}
