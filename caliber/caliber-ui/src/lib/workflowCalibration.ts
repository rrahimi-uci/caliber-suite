import type { RefinementJob } from "@/api/types";

export interface WorkflowCalibrationCandidateView {
  candidateId: string;
  summary: string | null;
  accepted: boolean;
  rejectedReason: string | null;
  patchKind: string | null;
  scores: Record<string, number>;
  deltas: Record<string, number>;
  semanticOps: string[];
  gateReasons: string[];
}

export interface WorkflowCalibrationView {
  workflowId: string;
  baselineVersionId: string | null;
  objective: string | null;
  epsilon: number | null;
  maxCandidates: number | null;
  datasetName: string | null;
  datasetExampleCount: number | null;
  judgeEnabled: boolean;
  targetAlias: string | null;
  patchId: string | null;
  lowConfidence: boolean;
  nExamples: number | null;
  winnerId: string | null;
  patchKind: string | null;
  summary: string | null;
  promptSuggestion: string | null;
  semanticOps: string[];
  gatePassed: boolean | null;
  gateReasons: string[];
  candidates: WorkflowCalibrationCandidateView[];
  candidateManifestText: string | null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function readString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function readNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function readBoolean(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

function readStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((entry) => readString(entry))
    .filter((entry): entry is string => entry !== null);
}

function readNumberMap(value: unknown): Record<string, number> {
  if (!isRecord(value)) return {};
  return Object.fromEntries(
    Object.entries(value).filter(([, entry]) => typeof entry === "number" && Number.isFinite(entry)),
  ) as Record<string, number>;
}

export function humanizeWorkflowCalibrationLabel(value: string | null): string {
  if (!value) return "—";
  return value.replaceAll("_", " ");
}

export function formatWorkflowCalibrationScore(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(3) : "—";
}

export function formatWorkflowCalibrationDelta(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  const sign = value > 0 ? "+" : "";
  return `${sign}${(value * 100).toFixed(1)}pp`;
}

function semanticOpLabels(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((entry) => {
      if (typeof entry === "string") return humanizeWorkflowCalibrationLabel(entry);
      if (!isRecord(entry)) return null;
      return humanizeWorkflowCalibrationLabel(readString(entry.op) ?? readString(entry.kind));
    })
    .filter((entry): entry is string => entry !== null && entry !== "—");
}

function parseCalibrationCandidate(value: unknown): WorkflowCalibrationCandidateView | null {
  if (!isRecord(value)) return null;
  const gate = isRecord(value.gate) ? value.gate : null;
  const candidateId = readString(value.candidate_id);
  if (!candidateId) return null;
  return {
    candidateId,
    summary: readString(value.summary),
    accepted: Boolean(value.accepted),
    rejectedReason: readString(value.rejected_reason),
    patchKind: readString(value.patch_kind),
    scores: readNumberMap(value.scores),
    deltas: readNumberMap(value.deltas),
    semanticOps: semanticOpLabels(value.semantic_ops),
    gateReasons: readStringArray(gate?.reasons),
  };
}

function workflowCalibrationPayloadView({
  artifactType,
  workflowId,
  calibrationSpec,
  candidatePayload,
  evalResultsPayload,
}: {
  artifactType: string | null | undefined;
  workflowId: string | null | undefined;
  calibrationSpec: Record<string, unknown> | null | undefined;
  candidatePayload: Record<string, unknown> | null | undefined;
  evalResultsPayload: Record<string, unknown> | null | undefined;
}): WorkflowCalibrationView | null {
  if (artifactType !== "workflow_manifest" || !workflowId || !isRecord(calibrationSpec)) {
    return null;
  }
  const spec = calibrationSpec;
  const candidate = isRecord(candidatePayload) ? candidatePayload : null;
  const evalResults = isRecord(evalResultsPayload) ? evalResultsPayload : null;
  const objective = isRecord(spec.objective) ? spec.objective : null;
  const budget = isRecord(spec.budget) ? spec.budget : null;
  const judge = isRecord(spec.judge) ? spec.judge : null;
  const datasetSummary = isRecord(spec.dataset_summary) ? spec.dataset_summary : null;
  const gate = isRecord(evalResults?.gate)
    ? evalResults?.gate
    : isRecord(candidate?.calibration_gate)
      ? candidate?.calibration_gate
      : null;
  const rawCandidates = Array.isArray(candidate?.calibration_candidates)
    ? candidate.calibration_candidates
    : Array.isArray(evalResults?.calibration_candidates)
      ? evalResults.calibration_candidates
      : [];
  const candidates = rawCandidates
    .map((entry) => parseCalibrationCandidate(entry))
    .filter((entry): entry is WorkflowCalibrationCandidateView => entry !== null);
  const winnerId =
    readString(candidate?.calibration_winner_id) ?? readString(evalResults?.calibration_winner_id);
  const winner = candidates.find((entry) => entry.candidateId === winnerId) ?? null;
  const candidateSemanticOps = semanticOpLabels(candidate?.semantic_ops);
  return {
    workflowId,
    baselineVersionId:
      readString(candidate?.base_version_id) ?? readString(spec.workflow_version_id),
    objective: readString(objective?.maximize),
    epsilon: readNumber(objective?.epsilon),
    maxCandidates: readNumber(budget?.max_candidates),
    datasetName: readString(datasetSummary?.dataset_name),
    datasetExampleCount: readNumber(datasetSummary?.example_count),
    judgeEnabled: Boolean(judge?.enabled),
    targetAlias: readString(candidate?.target_alias),
    patchId:
      readString(candidate?.calibration_patch_id) ?? readString(evalResults?.calibration_patch_id),
    lowConfidence:
      readBoolean(candidate?.calibration_low_confidence)
      ?? readBoolean(evalResults?.calibration_low_confidence)
      ?? false,
    nExamples: readNumber(candidate?.calibration_n_examples) ?? readNumber(evalResults?.n_examples),
    winnerId,
    patchKind: readString(candidate?.patch_kind) ?? winner?.patchKind ?? null,
    summary: readString(candidate?.summary) ?? winner?.summary ?? null,
    promptSuggestion: readString(candidate?.prompt_suggestion),
    semanticOps: candidateSemanticOps.length > 0 ? candidateSemanticOps : (winner?.semanticOps ?? []),
    gatePassed: readBoolean(gate?.passed),
    gateReasons: readStringArray(gate?.reasons),
    candidates,
    candidateManifestText: readString(candidate?.content),
  };
}

export function workflowCalibrationView(job: RefinementJob): WorkflowCalibrationView | null {
  return workflowCalibrationPayloadView({
    artifactType: job.artifact_type,
    workflowId: job.workflow_id,
    calibrationSpec: job.calibration_spec,
    candidatePayload: job.candidate,
    evalResultsPayload: job.eval_results,
  });
}
