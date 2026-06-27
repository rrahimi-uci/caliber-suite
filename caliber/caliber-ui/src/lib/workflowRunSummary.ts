import type {
  ManifestEdge,
  ManifestNode,
  PortSpec,
  WorkflowManifest,
  WorkflowRunArtifactPersistenceSummary,
  WorkflowNodeType,
  WorkflowRun,
  WorkflowRunCheckpoint,
  WorkflowRunStep,
} from "@/api/workflowTypes";

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function readString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function readNonNegativeNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value) && value >= 0) {
    return Math.trunc(value);
  }
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    if (Number.isFinite(parsed) && parsed >= 0) {
      return Math.trunc(parsed);
    }
  }
  return null;
}

function readStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string" && item.trim().length > 0);
}

const WORKFLOW_NODE_TYPES = new Set<WorkflowNodeType>([
  "start",
  "file_input",
  "folder_input",
  "input_bucket",
  "output_bucket",
  "output_folder",
  "wait_until",
  "wait_for_event",
  "parallel",
  "join",
  "for_each",
  "loop",
  "error_boundary",
  "subworkflow",
  "tool",
  "mcp_resource",
  "knowledge_query",
  "knowledge_build",
  "template",
  "python_code",
  "agent",
  "guardrail",
  "router",
  "human_approval",
  "output",
  "note",
  "external_app",
]);

function isWorkflowNodeType(value: unknown): value is WorkflowNodeType {
  return typeof value === "string" && WORKFLOW_NODE_TYPES.has(value as WorkflowNodeType);
}

function isPortSpec(value: unknown): value is PortSpec {
  return isRecord(value) && typeof value.type === "string";
}

function inferSyntheticPortSpec(value: unknown): PortSpec {
  if (typeof value === "string") {
    return { type: "string" };
  }
  if (typeof value === "boolean") {
    return { type: "boolean" };
  }
  return { type: "structured" };
}

function summaryString(run: WorkflowRun | null | undefined, key: string): string | null {
  if (!isRecord(run?.summary)) return null;
  return readString((run.summary as Record<string, unknown>)[key]);
}

export function normalizeWorkflowRunArtifactPersistence(
  value: unknown,
): WorkflowRunArtifactPersistenceSummary | null {
  if (!isRecord(value)) return null;
  const status = readString(value.status);
  const bucket = readString(value.bucket);
  const objectCount = readNonNegativeNumber(value.object_count);
  if (!status || !bucket || objectCount === null) return null;
  const artifactNames = readStringArray(value.artifact_names);
  const error = readString(value.error);
  const persistedObjectCount = readNonNegativeNumber(value.persisted_object_count);
  const recentPersistedKeys = readStringArray(value.recent_persisted_keys);
  const failedObjectKey = readString(value.failed_object_key);
  return {
    status,
    bucket,
    object_count: objectCount,
    artifact_names: artifactNames,
    ...(error ? { error } : {}),
    ...(persistedObjectCount !== null ? { persisted_object_count: persistedObjectCount } : {}),
    ...(recentPersistedKeys.length > 0 ? { recent_persisted_keys: recentPersistedKeys } : {}),
    ...(failedObjectKey ? { failed_object_key: failedObjectKey } : {}),
  };
}

export function workflowRunArtifactPersistence(
  run: WorkflowRun | null | undefined,
): WorkflowRunArtifactPersistenceSummary | null {
  if (!isRecord(run?.summary)) return null;
  return normalizeWorkflowRunArtifactPersistence(run.summary.artifact_persistence);
}

export function normalizeWorkflowRunStep(value: unknown): WorkflowRunStep | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as Partial<WorkflowRunStep>;
  if (typeof raw.node_id !== "string" || typeof raw.node_type !== "string") return null;
  const inputByPort = isRecord(raw.input_by_port) ? raw.input_by_port : null;
  const outputByPort = isRecord(raw.output_by_port) ? raw.output_by_port : null;
  return {
    node_id: raw.node_id,
    node_type: raw.node_type,
    status: typeof raw.status === "string" ? raw.status : "ok",
    output: typeof raw.output === "string" ? raw.output : "",
    tokens: typeof raw.tokens === "number" ? raw.tokens : undefined,
    prompt_tokens: typeof raw.prompt_tokens === "number" ? raw.prompt_tokens : undefined,
    completion_tokens: typeof raw.completion_tokens === "number" ? raw.completion_tokens : undefined,
    cached_prompt_tokens:
      typeof raw.cached_prompt_tokens === "number" ? raw.cached_prompt_tokens : undefined,
    cost_usd: typeof raw.cost_usd === "number" ? raw.cost_usd : undefined,
    model: typeof raw.model === "string" ? raw.model : null,
    prompt_version: typeof raw.prompt_version === "string" ? raw.prompt_version : null,
    tool_calls: Array.isArray(raw.tool_calls) ? raw.tool_calls : [],
    handoff_target: typeof raw.handoff_target === "string" ? raw.handoff_target : null,
    detail: typeof raw.detail === "string" ? raw.detail : "",
    duration_ms: typeof raw.duration_ms === "number" ? raw.duration_ms : 0,
    input_by_port: inputByPort,
    output_by_port: outputByPort,
  };
}

export function workflowRunSummarySteps(run: WorkflowRun | null | undefined): WorkflowRunStep[] {
  const rawSteps = Array.isArray(run?.summary?.steps) ? run.summary.steps : [];
  return rawSteps
    .map((step) => normalizeWorkflowRunStep(step))
    .filter((step): step is WorkflowRunStep => step !== null);
}

function workflowRunNodePath(run: WorkflowRun | null | undefined): string[] {
  const path = Array.isArray(run?.summary?.node_path) ? run.summary.node_path : [];
  return path.filter((nodeId): nodeId is string => typeof nodeId === "string" && nodeId.trim().length > 0);
}

function readCheckpointNodeId(checkpoint: WorkflowRunCheckpoint): string | null {
  if (typeof checkpoint.node_id === "string" && checkpoint.node_id.trim()) {
    return checkpoint.node_id;
  }
  const state = isRecord(checkpoint.state_blob) ? checkpoint.state_blob : null;
  return readString(state?.node_id);
}

function checkpointNodePath(checkpoints: WorkflowRunCheckpoint[]): string[] {
  const ordered = [...checkpoints].sort((left, right) => left.sequence - right.sequence);
  const seen = new Set<string>();
  const nodeIds: string[] = [];
  for (const checkpoint of ordered) {
    const nodeId = readCheckpointNodeId(checkpoint);
    if (!nodeId || seen.has(nodeId)) continue;
    nodeIds.push(nodeId);
    seen.add(nodeId);
  }
  return nodeIds;
}

function checkpointKind(checkpoint: WorkflowRunCheckpoint | null): string | null {
  const state = checkpoint && isRecord(checkpoint.state_blob) ? checkpoint.state_blob : null;
  return readString(state?.kind);
}

function inferSyntheticNodeType(
  {
    run,
    nodeId,
    step,
    checkpoint,
    index,
    total,
  }: {
    run: WorkflowRun;
    nodeId: string;
    step: WorkflowRunStep | null;
    checkpoint: WorkflowRunCheckpoint | null;
    index: number;
    total: number;
  },
): WorkflowNodeType {
  if (step && isWorkflowNodeType(step.node_type)) {
    return step.node_type;
  }
  const checkpointNodeKind = checkpointKind(checkpoint);
  if (checkpointNodeKind === "wait_for_event") return "wait_for_event";
  if (checkpointNodeKind === "wait_until") return "wait_until";
  if (checkpointNodeKind === "human_approval") return "human_approval";
  if (index === 0 || nodeId === "start") {
    return "start";
  }
  if (
    run.status === "completed" &&
    (index === total - 1 || /(?:^|[_-])(final|output)(?:$|[_-])/i.test(nodeId))
  ) {
    return "output";
  }
  return "note";
}

function syntheticNodePorts(nodeType: WorkflowNodeType): {
  inputs?: Record<string, PortSpec>;
  outputs?: Record<string, PortSpec>;
} {
  switch (nodeType) {
    case "start":
      return { outputs: { output: { type: "string" } } };
    case "output":
      return { inputs: { response: { type: "string" } } };
    case "agent":
      return {
        inputs: { input: { type: "string" } },
        outputs: { final_output: { type: "string" } },
      };
    case "wait_for_event":
      return {
        inputs: { input: { type: "string" } },
        outputs: {
          output: { type: "string" },
          event_payload: { type: "structured" },
          event_name: { type: "string" },
        },
      };
    case "wait_until":
      return {
        inputs: { input: { type: "string" } },
        outputs: { output: { type: "string" } },
      };
    case "human_approval":
      return {
        inputs: { request: { type: "string" } },
        outputs: { request: { type: "string" } },
      };
    default:
      return {};
  }
}

function buildSyntheticNode(
  {
    run,
    nodeId,
    step,
    checkpoint,
    index,
    total,
  }: {
    run: WorkflowRun;
    nodeId: string;
    step: WorkflowRunStep | null;
    checkpoint: WorkflowRunCheckpoint | null;
    index: number;
    total: number;
  },
): ManifestNode {
  const type = inferSyntheticNodeType({ run, nodeId, step, checkpoint, index, total });
  const ports = syntheticNodePorts(type);
  const node: ManifestNode = {
    id: nodeId,
    type,
    name: nodeId,
    ...ports,
  };
  if (type === "note") {
    node.text = step?.detail || "Reconstructed from recorded run history.";
  }
  if (type === "agent") {
    node.model = step?.model ?? "inherit";
    if (step?.detail) {
      node.instructions = { type: "inline", text: step.detail };
    }
  }
  const checkpointState = checkpoint && isRecord(checkpoint.state_blob) ? checkpoint.state_blob : null;
  if (step?.input_by_port) {
    node.inputs = Object.fromEntries(
      Object.entries(step.input_by_port)
        .filter(([, value]) => value !== undefined)
        .map(([port, value]) => [
          port,
          isPortSpec(value) ? value : inferSyntheticPortSpec(value),
        ]),
    );
  } else if (checkpointState && isRecord(checkpointState.input_by_port)) {
    node.inputs = Object.fromEntries(
      Object.entries(checkpointState.input_by_port)
        .filter(([, value]) => value !== undefined)
        .map(([port, value]) => [port, inferSyntheticPortSpec(value)]),
    );
  }
  if (step?.output_by_port) {
    node.outputs = Object.fromEntries(
      Object.entries(step.output_by_port)
        .filter(([, value]) => value !== undefined)
        .map(([port, value]) => [
          port,
          isPortSpec(value) ? value : inferSyntheticPortSpec(value),
        ]),
    );
  } else if (checkpointState && checkpointState.output !== undefined) {
    node.outputs = {
      ...(node.outputs ?? {}),
      output: inferSyntheticPortSpec(checkpointState.output),
    };
  }
  return node;
}

export function buildSyntheticWorkflowRunManifest(
  run: WorkflowRun | null | undefined,
  checkpoints: WorkflowRunCheckpoint[] = [],
): WorkflowManifest | null {
  if (!run) return null;
  const steps = workflowRunSummarySteps(run);
  const stepMap = new Map<string, WorkflowRunStep>();
  for (const step of steps) {
    if (!stepMap.has(step.node_id)) {
      stepMap.set(step.node_id, step);
    }
  }
  const checkpointPath = checkpointNodePath(checkpoints);
  const checkpointMap = new Map<string, WorkflowRunCheckpoint>();
  for (const checkpoint of checkpoints) {
    const nodeId = readCheckpointNodeId(checkpoint);
    if (nodeId && !checkpointMap.has(nodeId)) {
      checkpointMap.set(nodeId, checkpoint);
    }
  }

  const orderedNodeIds: string[] = [];
  const seen = new Set<string>();
  for (const nodeId of workflowRunNodePath(run)) {
    if (seen.has(nodeId)) continue;
    orderedNodeIds.push(nodeId);
    seen.add(nodeId);
  }
  for (const step of steps) {
    if (seen.has(step.node_id)) continue;
    orderedNodeIds.push(step.node_id);
    seen.add(step.node_id);
  }
  const currentNodeId = readString(run.current_node_id);
  if (currentNodeId && !seen.has(currentNodeId)) {
    orderedNodeIds.push(currentNodeId);
    seen.add(currentNodeId);
  }
  if (orderedNodeIds.length === 0) {
    if (checkpointPath.length > 0 && checkpointPath[0] !== "start") {
      orderedNodeIds.push("start");
      seen.add("start");
    }
    for (const nodeId of checkpointPath) {
      if (seen.has(nodeId)) continue;
      orderedNodeIds.push(nodeId);
      seen.add(nodeId);
    }
  }
  if (
    orderedNodeIds.length === 1 &&
    orderedNodeIds[0] !== "start" &&
    steps.length === 0 &&
    workflowRunNodePath(run).length === 0
  ) {
    orderedNodeIds.unshift("start");
  }
  if (orderedNodeIds.length === 0) return null;

  const nodes = Object.fromEntries(
    orderedNodeIds.map((nodeId, index) => [
      nodeId,
      buildSyntheticNode({
        run,
        nodeId,
        step: stepMap.get(nodeId) ?? null,
        checkpoint: checkpointMap.get(nodeId) ?? null,
        index,
        total: orderedNodeIds.length,
      }),
    ]),
  );
  const edges: ManifestEdge[] = orderedNodeIds.slice(1).map((nodeId, index) => ({
    id: `synthetic-${orderedNodeIds[index]}-${nodeId}`,
    from: orderedNodeIds[index]!,
    to: nodeId,
    map: {},
  }));

  return {
    schema_version: 1,
    workflow_id: run.workflow_id,
    name: `Recovered run ${run.workflow_run_id}`,
    nodes,
    edges,
  };
}

export function workflowRunRetryOf(run: WorkflowRun | null | undefined): string | null {
  return summaryString(run, "retry_of");
}

export function workflowRunRetryMode(run: WorkflowRun | null | undefined): string | null {
  return summaryString(run, "retry_mode");
}

export function workflowRunResumeCheckpointId(
  run: WorkflowRun | null | undefined,
): string | null {
  return summaryString(run, "resume_checkpoint_id");
}

export function workflowRunResumeCheckpointRunId(
  run: WorkflowRun | null | undefined,
): string | null {
  return summaryString(run, "resume_checkpoint_run_id");
}

export function workflowRunHasInheritedResumeCheckpoint(
  run: WorkflowRun | null | undefined,
): boolean {
  const checkpointId = workflowRunResumeCheckpointId(run);
  const sourceRunId = workflowRunResumeCheckpointRunId(run);
  return Boolean(
    run?.workflow_run_id &&
    checkpointId &&
    sourceRunId &&
    sourceRunId !== run.workflow_run_id,
  );
}

export function workflowRunRetryEntryLabel(
  run: WorkflowRun | null | undefined,
): string {
  if (workflowRunRetryMode(run) === "checkpoint" && workflowRunResumeCheckpointId(run)) {
    return "Checkpoint retry";
  }
  if (workflowRunRetryOf(run) || run?.parent_run_id) {
    return "Fresh retry";
  }
  return "Initial attempt";
}

export function workflowRunRetryEntryDetail(
  run: WorkflowRun | null | undefined,
): string {
  const checkpointId = workflowRunResumeCheckpointId(run);
  const sourceRunId =
    workflowRunResumeCheckpointRunId(run) ?? workflowRunRetryOf(run) ?? run?.parent_run_id ?? null;
  if (workflowRunRetryMode(run) === "checkpoint" && checkpointId) {
    return sourceRunId
      ? `Resumed from checkpoint ${checkpointId} on ${sourceRunId}.`
      : `Resumed from checkpoint ${checkpointId}.`;
  }
  if (workflowRunRetryOf(run) || run?.parent_run_id) {
    return sourceRunId
      ? `Fresh retry of ${sourceRunId}.`
      : "Fresh retry queued from a prior attempt.";
  }
  return "Initial attempt in this retry lineage.";
}

export function workflowRunRetryLineageDetail(
  run: WorkflowRun | null | undefined,
): string {
  const checkpointId = workflowRunResumeCheckpointId(run);
  const sourceRunId =
    workflowRunResumeCheckpointRunId(run) ?? workflowRunRetryOf(run) ?? run?.parent_run_id ?? null;
  if (workflowRunRetryMode(run) === "checkpoint" && checkpointId) {
    return sourceRunId
      ? `checkpoint retry via ${checkpointId} from ${sourceRunId}`
      : `checkpoint retry via ${checkpointId}`;
  }
  if (sourceRunId) {
    return `retry of ${sourceRunId}`;
  }
  return "initial attempt";
}

export function mergeWorkflowRunCheckpoints(
  checkpoints: WorkflowRunCheckpoint[],
  resumeSourceCheckpoint: WorkflowRunCheckpoint | null | undefined,
): WorkflowRunCheckpoint[] {
  const next: WorkflowRunCheckpoint[] = [];
  const seen = new Set<string>();
  if (resumeSourceCheckpoint) {
    next.push(resumeSourceCheckpoint);
    seen.add(resumeSourceCheckpoint.checkpoint_id);
  }
  for (const checkpoint of checkpoints) {
    if (seen.has(checkpoint.checkpoint_id)) continue;
    next.push(checkpoint);
    seen.add(checkpoint.checkpoint_id);
  }
  return next;
}

export function resolveWorkflowRunActiveCheckpoint(
  run: WorkflowRun | null | undefined,
  checkpoints: WorkflowRunCheckpoint[],
): WorkflowRunCheckpoint | null {
  const checkpointId = workflowRunResumeCheckpointId(run);
  if (checkpointId) {
    const matched = checkpoints.find(
      (checkpoint) => checkpoint.checkpoint_id === checkpointId,
    );
    if (matched) return matched;
  }
  return [...checkpoints].sort((left, right) => right.sequence - left.sequence)[0] ?? null;
}
