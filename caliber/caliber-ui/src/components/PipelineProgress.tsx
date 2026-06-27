/**
 * Compact pipeline indicator.
 *
 * Renders the calibration/optimization pipeline stages in canonical order
 * from the backend's `_STAGE_DISPATCH` table:
 * triage → evidence → diagnosis → candidate → eval. Jobs now terminate at
 * `done`/`candidate_ready` (an operator applies a candidate) — there is no
 * approval stage.
 *
 * Terminal statuses are derived from the job's status, not its stage —
 * a `failed/triage` job paints triage red, the rest gray.
 */

import type { JobStage, JobStatus } from "@/api/types";

const STAGES: { key: JobStage; label: string }[] = [
  { key: "triage", label: "Triage" },
  { key: "evidence", label: "Evidence" },
  { key: "diagnosis", label: "Diagnosis" },
  { key: "candidate", label: "Candidate" },
  { key: "eval", label: "Eval" },
];

interface PipelineProgressProps {
  currentStage: JobStage;
  status: JobStatus;
  /** Compact mode renders dots only — for table cells. */
  compact?: boolean;
}

type StageState = "complete" | "current" | "pending" | "failed";

function computeStates(
  current: JobStage,
  status: JobStatus,
): Record<JobStage, StageState> {
  const result: Record<JobStage, StageState> = {} as Record<JobStage, StageState>;
  const currentIndex = STAGES.findIndex((s) => s.key === current);
  const isTerminalSuccess = status === "completed" || current === "done";

  for (let i = 0; i < STAGES.length; i++) {
    const stageEntry = STAGES[i];
    if (!stageEntry) continue;
    const stage: JobStage = stageEntry.key;
    if (isTerminalSuccess) {
      result[stage] = "complete";
      continue;
    }
    if (currentIndex < 0) {
      // Unknown stage — treat as all complete unless the status says otherwise.
      result[stage] = "complete";
      continue;
    }
    if (i < currentIndex) {
      result[stage] = "complete";
    } else if (i === currentIndex) {
      result[stage] = status === "failed" || status === "rejected" ? "failed" : "current";
    } else {
      result[stage] = "pending";
    }
  }
  result["done"] = "complete";
  return result;
}

const TONE: Record<StageState, { dot: string; ring: string; text: string }> = {
  complete: {
    dot: "bg-green-500",
    ring: "ring-green-200",
    text: "text-green-700",
  },
  current: {
    dot: "bg-caliber-purple animate-pulse",
    ring: "ring-caliber-purple/30",
    text: "text-caliber-purple",
  },
  pending: {
    dot: "bg-surface-300",
    ring: "ring-surface-200",
    text: "text-gray-400",
  },
  failed: {
    dot: "bg-red-500",
    ring: "ring-red-200",
    text: "text-red-700",
  },
};

export function PipelineProgress({
  currentStage,
  status,
  compact = false,
}: PipelineProgressProps): JSX.Element {
  const states = computeStates(currentStage, status);

  if (compact) {
    return (
      <div className="flex items-center gap-1" aria-label="Pipeline progress">
        {STAGES.map((s) => {
          const state = states[s.key];
          return (
            <span
              key={s.key}
              className={`w-2 h-2 rounded-full ${TONE[state].dot}`}
              title={`${s.label}: ${state}`}
              aria-hidden="true"
            />
          );
        })}
      </div>
    );
  }

  return (
    <ol className="flex items-center justify-between gap-2 overflow-x-auto">
      {STAGES.map((s, i) => {
        const state = states[s.key];
        return (
          <li key={s.key} className="flex items-center gap-2 flex-1 min-w-0">
            <div className="flex flex-col items-center text-center min-w-[72px]">
              <span
                className={`w-3 h-3 rounded-full ring-4 ${TONE[state].dot} ${TONE[state].ring}`}
                aria-hidden="true"
              />
              <span className={`text-xs mt-1.5 font-medium ${TONE[state].text}`}>
                {s.label}
              </span>
            </div>
            {i < STAGES.length - 1 && (
              <span className="h-px flex-1 bg-surface-200" aria-hidden="true" />
            )}
          </li>
        );
      })}
    </ol>
  );
}
