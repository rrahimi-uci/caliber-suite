import { useEffect, useMemo, useState } from "react";

import { ApiError, caliberApi } from "@/api/caliberApi";
import { useApiQuery } from "@/hooks/useApiQuery";
import {
  buildApprovedWorkflowRunLineageOptions,
  selectedWorkflowRunLineageOption,
  type WorkflowRunLineageOption,
} from "@/lib/workflowRunLineage";

export function useWorkflowRunLineage(
  projectId: string | null | undefined,
  enabled = true,
): {
  options: WorkflowRunLineageOption[];
  selectedOption: WorkflowRunLineageOption | null;
  selectOption: (option: WorkflowRunLineageOption) => void;
  selectedWorkspaceReleaseId: string | null;
  selectedEnvironmentId: string | null;
  isLoading: boolean;
  error: ApiError | null;
} {
  const environmentsQuery = useApiQuery(
    ["workflow-run-lineage-environments", projectId],
    (signal) => caliberApi.listProjectEnvironments(projectId!, signal),
    { enabled: Boolean(projectId) && enabled },
  );
  const releasesQuery = useApiQuery(
    ["workflow-run-lineage-releases", projectId],
    (signal) =>
      caliberApi.listWorkspaceReleases(
        projectId!,
        { status: "approved", limit: 200 },
        signal,
      ),
    { enabled: Boolean(projectId) && enabled },
  );
  const options = useMemo(
    () =>
      projectId && environmentsQuery.data && releasesQuery.data
        ? buildApprovedWorkflowRunLineageOptions(
            projectId,
            environmentsQuery.data.environments,
            releasesQuery.data,
          )
        : [],
    [environmentsQuery.data, projectId, releasesQuery.data],
  );
  const [selectedWorkspaceReleaseId, setSelectedWorkspaceReleaseId] = useState<
    string | null
  >(null);
  const [selectedEnvironmentId, setSelectedEnvironmentId] = useState<
    string | null
  >(null);

  useEffect(() => {
    const selected = selectedWorkflowRunLineageOption(
      options,
      selectedWorkspaceReleaseId,
      selectedEnvironmentId,
    );
    if (selected) return;
    const first = options[0] ?? null;
    setSelectedWorkspaceReleaseId(first?.workspace_release_id ?? null);
    setSelectedEnvironmentId(first?.environment_id ?? null);
  }, [options, selectedEnvironmentId, selectedWorkspaceReleaseId]);

  const selectedOption = selectedWorkflowRunLineageOption(
    options,
    selectedWorkspaceReleaseId,
    selectedEnvironmentId,
  );
  return {
    options,
    selectedOption,
    selectOption: (option) => {
      setSelectedWorkspaceReleaseId(option.workspace_release_id);
      setSelectedEnvironmentId(option.environment_id);
    },
    selectedWorkspaceReleaseId,
    selectedEnvironmentId,
    isLoading:
      Boolean(projectId) &&
      (environmentsQuery.isLoading || releasesQuery.isLoading),
    error: environmentsQuery.error ?? releasesQuery.error,
  };
}
