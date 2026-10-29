import type { ApiError } from "@/api/caliberApi";
import type { WorkflowRunLineageOption } from "@/lib/workflowRunLineage";
import {
  WORKFLOW_LINEAGE_EMPTY_MESSAGE,
  WORKFLOW_LINEAGE_SELECTION_MESSAGE,
} from "@/lib/workflowRunLineage";

interface WorkflowRunLineagePickerProps {
  options: WorkflowRunLineageOption[];
  selectedWorkspaceReleaseId: string | null;
  selectedEnvironmentId: string | null;
  onSelect: (option: WorkflowRunLineageOption) => void;
  isLoading: boolean;
  error: ApiError | null;
  disabled?: boolean;
  compact?: boolean;
}

export function WorkflowRunLineagePicker({
  options,
  selectedWorkspaceReleaseId,
  selectedEnvironmentId,
  onSelect,
  isLoading,
  error,
  disabled = false,
  compact = false,
}: WorkflowRunLineagePickerProps): JSX.Element {
  const selectedOption = options.find(
    (option) =>
      option.workspace_release_id === selectedWorkspaceReleaseId &&
      option.environment_id === selectedEnvironmentId,
  );
  const stateMessage = isLoading
    ? "Loading approved workspace releases and environments…"
    : error
      ? `Unable to load approved workspace releases and environments: ${error.message}`
      : options.length === 0
        ? WORKFLOW_LINEAGE_EMPTY_MESSAGE
        : selectedOption
          ? null
          : WORKFLOW_LINEAGE_SELECTION_MESSAGE;

  return (
    <div
      data-testid="workflow-run-lineage-picker"
      className={`grid gap-2 ${compact ? "sm:grid-cols-2" : "lg:grid-cols-2"}`}
    >
      <label className="text-xs font-semibold text-slate-500">
        Approved release
        <select
          data-testid="workflow-run-release-select"
          aria-label="Approved workspace release"
          className="mt-1 w-full rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm font-medium text-slate-700 outline-none transition-colors focus:border-caliber-purple disabled:cursor-not-allowed disabled:bg-slate-50 disabled:text-slate-400"
          value={selectedOption?.workspace_release_id ?? ""}
          disabled={disabled || isLoading || options.length === 0}
          onChange={(event) => {
            const option = options.find(
              (candidate) =>
                candidate.workspace_release_id === event.target.value,
            );
            if (option) onSelect(option);
          }}
        >
          {options.length > 0 ? (
            options.map((option) => (
              <option
                key={option.workspace_release_id}
                value={option.workspace_release_id}
              >
                {option.release_label} · {option.environment_label}
              </option>
            ))
          ) : (
            <option value="">No approved release available</option>
          )}
        </select>
      </label>
      <label className="text-xs font-semibold text-slate-500">
        Environment
        <select
          data-testid="workflow-run-environment-select"
          aria-label="Approved runtime environment"
          className="mt-1 w-full rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm font-medium text-slate-700 outline-none transition-colors focus:border-caliber-purple disabled:cursor-not-allowed disabled:bg-slate-50 disabled:text-slate-400"
          value={selectedOption?.environment_id ?? ""}
          disabled={disabled || isLoading || options.length === 0}
          onChange={(event) => {
            const option = options.find(
              (candidate) => candidate.environment_id === event.target.value,
            );
            if (option) onSelect(option);
          }}
        >
          {options.length > 0 ? (
            options.map((option) => (
              <option key={option.environment_id} value={option.environment_id}>
                {option.environment_label} · {option.environment_class}
              </option>
            ))
          ) : (
            <option value="">No approved environment available</option>
          )}
        </select>
      </label>
      {stateMessage && (
        <div
          data-testid="workflow-run-lineage-state"
          role="alert"
          className="sm:col-span-2 rounded-xl border border-amber-200/70 bg-amber-50/80 px-3 py-2 text-xs text-amber-900"
        >
          {stateMessage}
        </div>
      )}
    </div>
  );
}
