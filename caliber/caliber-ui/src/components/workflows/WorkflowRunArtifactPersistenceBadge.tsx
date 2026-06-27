import type { WorkflowRun } from "@/api/workflowTypes";
import { workflowRunArtifactPersistence } from "@/lib/workflowRunSummary";

function badgeTone(status: string): string {
  switch (status) {
    case "persisted":
      return "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-800/70 dark:bg-emerald-950/40 dark:text-emerald-300";
    case "failed":
      return "border-red-200 bg-red-50 text-red-700 dark:border-red-800/70 dark:bg-red-950/40 dark:text-red-300";
    default:
      return "border-slate-200 bg-slate-50 text-slate-600 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300";
  }
}

function artifactLabel(run: WorkflowRun): string | null {
  const persistence = workflowRunArtifactPersistence(run);
  if (!persistence) return null;
  if (persistence.status === "failed") {
    return "Artifact upload failed";
  }
  const namedArtifactCount = persistence.artifact_names.length;
  if (namedArtifactCount > 0) {
    return `${namedArtifactCount} artifact${namedArtifactCount === 1 ? "" : "s"} stored`;
  }
  return `${persistence.object_count} object${persistence.object_count === 1 ? "" : "s"} stored`;
}

function artifactTitle(run: WorkflowRun): string | null {
  const persistence = workflowRunArtifactPersistence(run);
  if (!persistence) return null;
  const namedArtifacts =
    persistence.artifact_names.length > 0
      ? ` Named artifacts: ${persistence.artifact_names.join(", ")}.`
      : "";
  if (persistence.status === "failed") {
    const persistedObjectCount = persistence.persisted_object_count ?? 0;
    const progress =
      persistedObjectCount > 0
        ? ` after ${persistedObjectCount} of ${persistence.object_count} object${persistence.object_count === 1 ? "" : "s"} were stored`
        : "";
    const failedObject = persistence.failed_object_key
      ? ` Failing object: ${persistence.failed_object_key}.`
      : "";
    return persistence.error
      ? `Artifact upload to ${persistence.bucket} failed after completion${progress}. ${persistence.error}.${failedObject}${namedArtifacts}`
      : `Artifact upload to ${persistence.bucket} failed after completion${progress}.${failedObject}${namedArtifacts}`;
  }
  return `Stored ${persistence.object_count} object${persistence.object_count === 1 ? "" : "s"} in ${persistence.bucket}.${namedArtifacts}`;
}

export interface WorkflowRunArtifactPersistenceBadgeProps {
  run: WorkflowRun;
  compact?: boolean;
  dataTestId?: string;
}

export function WorkflowRunArtifactPersistenceBadge({
  run,
  compact = false,
  dataTestId,
}: WorkflowRunArtifactPersistenceBadgeProps): JSX.Element | null {
  const persistence = workflowRunArtifactPersistence(run);
  const label = artifactLabel(run);
  const title = artifactTitle(run);
  if (!persistence || !label) return null;
  return (
    <span
      data-testid={dataTestId}
      title={title ?? undefined}
      className={`inline-flex items-center rounded-full border font-semibold ${badgeTone(persistence.status)} ${
        compact ? "px-1.5 py-0.5 text-[10px]" : "px-2 py-0.5 text-[10px]"
      }`}
    >
      {label}
    </span>
  );
}
