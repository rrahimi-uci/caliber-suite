/**
 * Per-step preview — Lakeflow Designer "see what changed at every step" pattern.
 *
 * Presentational. Given a single `PreviewStep` from a preview-run (plus the
 * outputs of its upstream nodes as "input"), it shows the step's status, a
 * one-line "what changed", the upstream input(s), and the step output — so a
 * user can inspect each operator before moving on.
 */

import type { PreviewStep } from "@/api/workflowTypes";

const STATUS_STYLE: Record<string, string> = {
  ok: "bg-emerald-50 text-emerald-700 border-emerald-200 dark:bg-emerald-950/40 dark:text-emerald-300 dark:border-emerald-800/70",
  blocked:
    "bg-red-50 text-red-700 border-red-200 dark:bg-red-950/40 dark:text-red-300 dark:border-red-800/70",
  error:
    "bg-red-50 text-red-700 border-red-200 dark:bg-red-950/40 dark:text-red-300 dark:border-red-800/70",
  skipped:
    "bg-zinc-100 text-zinc-500 border-zinc-200 dark:bg-zinc-900 dark:text-zinc-400 dark:border-zinc-800",
};

/** Tailwind classes for a step-status chip. */
export function stepStatusStyle(status: string): string {
  return STATUS_STYLE[status] ?? "bg-zinc-100 text-zinc-600 border-zinc-200";
}

/** One-line summary of what an operator did this step (Lakeflow "what changed"). */
function humanizeStepDetail(step: PreviewStep): string | null {
  const detail = typeof step.detail === "string" ? step.detail.trim() : "";
  if (!detail) return null;
  if (detail.startsWith("waiting_event:")) {
    if (step.node_type === "wait_until") {
      return "paused until the scheduled resume time";
    }
    if (step.node_type === "wait_for_event" || step.node_type === "wait_event") {
      return "waiting for a resume event";
    }
    return "waiting for an external resume event";
  }
  if (detail.startsWith("waiting_approval:")) {
    if (step.node_type === "human_approval") return "awaiting human approval";
    return "awaiting runtime approval";
  }
  return detail;
}

function describeStorageChange(storageNode: StorageNodeDiagnostics): string | null {
  const countLabel = storageCountLabel(storageNode.nodeType, storageNode.count);
  const skippedLabel =
    storageNode.skippedCount && storageNode.skippedCount > 0
      ? ` (${storageNode.skippedCount} skipped)`
      : "";
  const location = storageNode.path
    ? storageNode.path
    : storageNode.bucket
      ? [
          storageNode.bucket,
          storageNode.prefix ? storageNode.prefix.replace(/^\/+/, "") : null,
        ]
          .filter(Boolean)
          .join("/")
      : null;
  if (storageNode.nodeType === "file_input" && storageNode.path) {
    return `read file ${pathLeaf(storageNode.path)}`;
  }
  const verb = storageNode.direction === "input" ? "loaded" : "wrote";
  if (countLabel && location) {
    return `${verb} ${countLabel} ${storageNode.direction === "input" ? "from" : "to"} ${location}${skippedLabel}`;
  }
  if (countLabel) return `${verb} ${countLabel}${skippedLabel}`;
  if (location) {
    return `${verb} ${storageItemUnit(storageNode.nodeType)}s ${storageNode.direction === "input" ? "from" : "to"} ${location}${skippedLabel}`;
  }
  return null;
}

function storageDiagnosticDetailSuffix(
  storageNode: StorageNodeDiagnostics | null,
): string | null {
  if (!storageNode) return null;
  const notices: string[] = [];
  if (storageNode.skippedCount !== null && storageNode.skippedCount > 0) {
    notices.push(`${storageNode.skippedCount} skipped`);
  }
  if (storageNode.truncatedList) {
    notices.push("listing truncated");
  }
  if (notices.length === 0) return null;
  return ` (${notices.join(", ")})`;
}

function describeKnowledgeQueryChange(
  knowledgeQuery: KnowledgeQueryDiagnostics,
): string | null {
  const retrievalLabel = retrievalModeLabel(knowledgeQuery.retrievalMode);
  const chunkCount =
    knowledgeQuery.chunks.length ||
    knowledgeQuery.citations.length ||
    knowledgeQuery.resultCount;
  if (retrievalLabel && chunkCount > 0) {
    return `retrieved ${chunkCount} chunk${chunkCount === 1 ? "" : "s"} via ${retrievalLabel}`;
  }
  if (retrievalLabel) return `queried the knowledge base via ${retrievalLabel}`;
  if (chunkCount > 0) {
    return `retrieved ${chunkCount} knowledge chunk${chunkCount === 1 ? "" : "s"}`;
  }
  return null;
}

function describeKnowledgeBuildChange(
  knowledgeBuild: KnowledgeBuildDiagnostics,
): string | null {
  const target = knowledgeBuild.knowledgeBaseId
    ? `knowledge base ${knowledgeBuild.knowledgeBaseId}`
    : "knowledge base";
  const versionLabel =
    knowledgeBuild.versionNumber !== null
      ? ` v${knowledgeBuild.versionNumber}`
      : knowledgeBuild.versionId
        ? ` ${knowledgeBuild.versionId}`
        : "";
  if (knowledgeBuild.previewSkipped) return `previewed ${target} build`;
  switch (knowledgeBuild.status) {
    case "completed":
      return `built ${target}${versionLabel}`;
    case "queued":
      return `queued ${target} build`;
    case "processing":
      return `building ${target}`;
    case "failed":
      return `failed ${target} build`;
    default:
      break;
  }
  if (knowledgeBuild.versionId) return `prepared ${target}${versionLabel}`;
  return null;
}

function describeForEachChange(
  step: PreviewStep,
  forEachNode: ForEachDiagnostics,
): string | null {
  if (forEachNode.count <= 0) return null;
  const noun = step.node_type === "loop" ? "iteration" : "item";
  const action = step.node_type === "loop" ? "completed" : "processed";
  const target = forEachNode.targetNodeId ? ` via ${forEachNode.targetNodeId}` : "";
  const failures =
    forEachNode.failed > 0
      ? ` (${forEachNode.failed} failed)`
      : "";
  return `${action} ${forEachNode.count} ${noun}${forEachNode.count === 1 ? "" : "s"}${target}${failures}`;
}

function describeJoinChange(joinNode: JoinDiagnostics): string | null {
  if (joinNode.branchCount <= 0) return null;
  return `merged ${joinNode.branchCount} branch${joinNode.branchCount === 1 ? "" : "es"}`;
}

function describeErrorBoundaryChange(
  errorBoundary: ErrorBoundaryDiagnostics,
): string | null {
  if (errorBoundary.message) return `handled error: ${errorBoundary.message}`;
  if (errorBoundary.compensationNodeId) {
    return `ran compensation ${errorBoundary.compensationNodeId}`;
  }
  return null;
}

function describeSubworkflowChange(
  subworkflowNode: SubworkflowDiagnostics,
): string | null {
  if (!subworkflowNode.workflowId) return null;
  const target = `${subworkflowNode.workflowId}${subworkflowNode.alias ? `@${subworkflowNode.alias}` : ""}`;
  switch (subworkflowNode.childStatus) {
    case "completed":
      return `completed child workflow ${target}`;
    case "blocked":
      return `child workflow ${target} blocked`;
    case "error":
      return `child workflow ${target} failed`;
    default:
      return `ran child workflow ${target}`;
  }
}

interface StepSummaryDiagnostics {
  storageNode?: StorageNodeDiagnostics | null;
  knowledgeQuery?: KnowledgeQueryDiagnostics | null;
  knowledgeBuild?: KnowledgeBuildDiagnostics | null;
  toolNode?: ToolNodeDiagnostics | null;
  forEachNode?: ForEachDiagnostics | null;
  joinNode?: JoinDiagnostics | null;
  errorBoundary?: ErrorBoundaryDiagnostics | null;
  subworkflowNode?: SubworkflowDiagnostics | null;
}

export function describeStepChange(
  step: PreviewStep,
  diagnostics: StepSummaryDiagnostics = {},
): string {
  const storageNode =
    diagnostics.storageNode ?? extractStorageNodeDiagnostics(step);
  const detail = humanizeStepDetail(step);
  if (detail) {
    return `${detail}${storageDiagnosticDetailSuffix(storageNode) ?? ""}`;
  }
  if (step.handoff_target) return `→ ${step.handoff_target}`; // router / handoff

  const storageSummary = storageNode ? describeStorageChange(storageNode) : null;
  if (storageSummary) return storageSummary;

  const knowledgeQuery =
    diagnostics.knowledgeQuery ?? extractKnowledgeQueryDiagnostics(step);
  const knowledgeQuerySummary = knowledgeQuery
    ? describeKnowledgeQueryChange(knowledgeQuery)
    : null;
  if (knowledgeQuerySummary) return knowledgeQuerySummary;

  const knowledgeBuild =
    diagnostics.knowledgeBuild ?? extractKnowledgeBuildDiagnostics(step);
  const knowledgeBuildSummary = knowledgeBuild
    ? describeKnowledgeBuildChange(knowledgeBuild)
    : null;
  if (knowledgeBuildSummary) return knowledgeBuildSummary;

  const forEachNode =
    diagnostics.forEachNode ?? extractForEachDiagnostics(step);
  const loopSummary = forEachNode ? describeForEachChange(step, forEachNode) : null;
  if (loopSummary) return loopSummary;

  const joinNode = diagnostics.joinNode ?? extractJoinDiagnostics(step);
  const joinSummary = joinNode ? describeJoinChange(joinNode) : null;
  if (joinSummary) return joinSummary;

  const errorBoundary =
    diagnostics.errorBoundary ?? extractErrorBoundaryDiagnostics(step);
  const errorSummary = errorBoundary
    ? describeErrorBoundaryChange(errorBoundary)
    : null;
  if (errorSummary) return errorSummary;

  const subworkflowNode =
    diagnostics.subworkflowNode ?? extractSubworkflowDiagnostics(step);
  const subworkflowSummary = subworkflowNode
    ? describeSubworkflowChange(subworkflowNode)
    : null;
  if (subworkflowSummary) return subworkflowSummary;

  const toolNode = diagnostics.toolNode ?? extractToolNodeDiagnostics(step);
  if (toolNode?.localName) return `ran tool ${toolNode.localName}`;

  if (step.tool_calls && step.tool_calls.length > 0) {
    return `used ${step.tool_calls.length} tool${step.tool_calls.length === 1 ? "" : "s"}`;
  }
  return step.status;
}

export interface UpstreamOutput {
  nodeId: string;
  output: string;
}

interface StepPreviewProps {
  step: PreviewStep;
  upstream?: UpstreamOutput[];
}

interface KnowledgeQueryCitation {
  chunk_id: string;
  label: string;
}

interface KnowledgeQueryChunk {
  chunk_id: string;
  source_name: string;
  source_key: string;
  score: number | null;
  content: string;
  matched_entity_labels: string[];
}

export interface KnowledgeQueryDiagnostics {
  retrievalMode: string | null;
  resultCount: number;
  citations: KnowledgeQueryCitation[];
  chunks: KnowledgeQueryChunk[];
  matchedEntities: string[];
  expandedEntities: string[];
  ageGraphName: string | null;
  ageStatus: string | null;
  ageSeedStrategy: string | null;
  ageMatchedChunkCount: number | null;
  ageTraversalHops: number | null;
  ageCandidatePoolSize: number | null;
  ageDenseRerankWeight: number | null;
  retrievalStrength: string | null;
  minimumRelationshipWeight: number | null;
  fallbackReason: string | null;
  ageFallbackReason: string | null;
  fallbackRetrievalMode: string | null;
  strictAgeRetrieval: boolean;
  queryOverrideActive: boolean;
}

export interface KnowledgeBuildDiagnostics {
  knowledgeBaseId: string | null;
  activeVersionId: string | null;
  versionId: string | null;
  versionNumber: number | null;
  status: string | null;
  runId: string | null;
  runStatus: string | null;
  chunkingStrategy: string | null;
  embeddingModel: string | null;
  waitRequested: boolean;
  waitStatus: string | null;
  waitTimeoutSeconds: number | null;
  activationRequested: boolean;
  activationStatus: string | null;
  activationActiveVersionId: string | null;
  graphTarget: string | null;
  graphExtractor: string | null;
  defaultRetrievalMode: string | null;
  retrievalStrength: string | null;
  ageSyncStatus: string | null;
  previewSkipped: boolean;
}

export interface ToolNodeDiagnostics {
  localName: string | null;
  registryRef: string | null;
  bindingType: string | null;
  callCount: number;
  requiresApproval: boolean;
  sideEffectLevel: string | null;
  modulePath: string | null;
  callableName: string | null;
  serverId: string | null;
  remoteToolName: string | null;
  argumentKeys: string[];
  resultPreview: string | null;
}

interface ForEachResultPreview {
  itemLabel: string;
  status: string | null;
  error: string | null;
  outputPreview: string | null;
  toolCallCount: number;
  artifactCount: number;
}

export interface ForEachDiagnostics {
  count: number;
  failed: number;
  targetNodeId: string | null;
  targetNodeType: string | null;
  artifactCount: number;
  results: ForEachResultPreview[];
}

export interface JoinDiagnostics {
  branchCount: number;
  mergedKeys: string[];
  outputPreview: string | null;
}

export interface ErrorBoundaryDiagnostics {
  message: string | null;
  targetNodeId: string | null;
  targetNodeType: string | null;
  compensationNodeId: string | null;
  compensationNodeType: string | null;
  compensationOutputPreview: string | null;
  artifactCount: number;
}

interface StorageEntryPreview {
  label: string;
  secondary: string | null;
  bytes: number | null;
  truncated: boolean;
}

export interface StorageNodeDiagnostics {
  nodeType: string;
  direction: "input" | "output";
  path: string | null;
  bucket: string | null;
  prefix: string | null;
  pattern: string | null;
  recursive: boolean;
  encoding: string | null;
  count: number | null;
  matchedCount: number | null;
  skippedCount: number | null;
  truncatedList: boolean;
  entries: StorageEntryPreview[];
}

export interface SubworkflowDiagnostics {
  childStatus: string | null;
  workflowId: string | null;
  alias: string | null;
  workflowVersionId: string | null;
  workflowVersionNumber: number | null;
  tokens: number | null;
  steps: string[];
  stepCount: number;
  outputPreview: string | null;
  error: string | null;
}

export interface StepTelemetryDiagnostics {
  tokens: number | null;
  promptTokens: number | null;
  completionTokens: number | null;
  cachedPromptTokens: number | null;
  costUsd: number | null;
  model: string | null;
  promptVersion: string | null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function readString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function readNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

export function formatUsdEstimate(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "$0.00";
  if (value < 0.000001) return "<$0.000001";
  const digits = value >= 1 ? 2 : value >= 0.01 ? 4 : 6;
  return `$${value.toFixed(digits)}`;
}

function readBoolean(value: unknown): boolean {
  return value === true;
}

function recordArray(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value
        .map((item) => (typeof item === "string" ? item.trim() : ""))
        .filter(Boolean)
    : [];
}

function compactJson(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value === "string" && value.trim()) return value.trim();
  try {
    const text = JSON.stringify(value);
    return typeof text === "string" && text !== "null" ? text : null;
  } catch {
    return String(value);
  }
}

export function retrievalModeLabel(mode: string | null): string | null {
  if (!mode) return null;
  if (mode === "age_graph") return "Apache AGE graph";
  if (mode === "graph_hybrid") return "GraphRAG hybrid";
  if (mode === "dense") return "Dense chunks";
  return mode;
}

export function ageSeedStrategyLabel(strategy: string | null): string | null {
  if (strategy === "query_text") return "Seeded from question text";
  if (strategy === "query_entities") return "Seeded from extracted entities";
  if (strategy === "query_entities_and_text")
    return "Seeded from entities + question text";
  return null;
}

export function knowledgeGraphTargetLabel(target: string | null): string | null {
  if (!target) return null;
  if (target === "object_store_and_age") return "Object store + AGE";
  if (target === "object_store") return "Object store";
  return target.replaceAll("_", " ");
}

export function knowledgeBuildStatusLabel(
  status: string | null,
  previewSkipped = false,
): string | null {
  if (previewSkipped || status === "preview_skipped") return "Preview skipped";
  if (!status) return null;
  if (status === "queued") return "Build queued";
  if (status === "processing") return "Build processing";
  if (status === "completed") return "Build completed";
  if (status === "failed") return "Build failed";
  return `Build ${status.replaceAll("_", " ")}`;
}

export function knowledgeBuildWaitStatusLabel(
  status: string | null,
  requested: boolean,
): string | null {
  if (!requested && !status) return null;
  if (status === "completed") return "Waited for completion";
  if (status === "timeout") return "Wait timed out";
  if (status === "not_requested") return "Did not wait";
  if (requested && !status) return "Waiting requested";
  return status ? `Wait ${status.replaceAll("_", " ")}` : null;
}

export function knowledgeBuildActivationStatusLabel(
  status: string | null,
  activeVersionId: string | null,
  requested: boolean,
): string | null {
  if (!requested && !status) return null;
  if (status === "activated") {
    return activeVersionId ? `Activated ${activeVersionId}` : "Activated";
  }
  if (status === "pending") return "Activation deferred";
  if (status === "skipped") return "Activation skipped";
  if (requested && !status) return "Activation requested";
  return status ? `Activation ${status.replaceAll("_", " ")}` : null;
}

export function workflowNodeTypeLabel(nodeType: string | null): string | null {
  if (!nodeType) return null;
  if (nodeType === "start") return "Start";
  if (nodeType === "agent") return "Agent";
  if (nodeType === "file_input") return "File input";
  if (nodeType === "folder_input") return "Folder input";
  if (nodeType === "input_bucket") return "Input bucket";
  if (nodeType === "output_bucket") return "Output bucket";
  if (nodeType === "output_folder") return "Output folder";
  if (nodeType === "output") return "Output";
  if (nodeType === "wait_until") return "Wait until";
  if (nodeType === "wait_for_event") return "Wait for event";
  if (nodeType === "wait_event") return "Wait for event";
  if (nodeType === "parallel") return "Parallel";
  if (nodeType === "join") return "Join";
  if (nodeType === "for_each") return "For-each loop";
  if (nodeType === "loop") return "Loop";
  if (nodeType === "error_boundary") return "Error boundary";
  if (nodeType === "tool") return "Tool";
  if (nodeType === "knowledge_query") return "Knowledge query";
  if (nodeType === "knowledge_build") return "Knowledge build";
  if (nodeType === "template") return "Template";
  if (nodeType === "guardrail") return "Guardrail";
  if (nodeType === "router") return "Router";
  if (nodeType === "human_approval") return "Human approval";
  if (nodeType === "mcp_resource") return "MCP resource";
  if (nodeType === "python_code") return "Python code";
  if (nodeType === "subworkflow") return "Subworkflow";
  if (nodeType === "external_app") return "External app";
  if (nodeType === "note") return "Note";
  return nodeType.replaceAll("_", " ");
}

export function toolBindingTypeLabel(
  bindingType: string | null,
): string | null {
  if (!bindingType) return null;
  if (bindingType === "registered_function") return "Registered function";
  if (bindingType === "mcp_tool") return "MCP tool";
  return bindingType.replaceAll("_", " ");
}

export function toolSideEffectLabel(
  sideEffectLevel: string | null,
): string | null {
  if (!sideEffectLevel) return null;
  if (sideEffectLevel === "external_action") return "external action";
  return sideEffectLevel.replaceAll("_", " ");
}

export function toolArgumentSummary(argumentKeys: string[]): string {
  if (argumentKeys.length === 0) return "No explicit arguments recorded";
  const suffix = argumentKeys.length === 1 ? "key" : "keys";
  return `${argumentKeys.length} ${suffix}: ${argumentKeys.join(", ")}`;
}

export function toolBindingTargetLabel(
  toolNode: ToolNodeDiagnostics | null,
): string | null {
  if (!toolNode) return null;
  if (toolNode.modulePath && toolNode.callableName) {
    return `${toolNode.modulePath}:${toolNode.callableName}`;
  }
  if (toolNode.serverId && toolNode.remoteToolName) {
    return `${toolNode.serverId}/${toolNode.remoteToolName}`;
  }
  if (toolNode.serverId) return toolNode.serverId;
  return null;
}

function chunkPreview(content: string): string {
  const compact = content.replace(/\s+/g, " ").trim();
  if (compact.length <= 180) return compact;
  return `${compact.slice(0, 177).trimEnd()}...`;
}

function pathLeaf(value: string): string {
  const normalized = value.replaceAll("\\", "/");
  const parts = normalized.split("/").filter(Boolean);
  return parts[parts.length - 1] ?? value;
}

function toolResultPreview(value: unknown): string | null {
  if (isRecord(value)) {
    for (const key of [
      "text",
      "output",
      "message",
      "content",
      "answer",
      "result",
    ]) {
      const candidate = value[key];
      if (typeof candidate === "string" && candidate.trim()) {
        return chunkPreview(candidate);
      }
    }
  }
  const serialized = compactJson(value);
  return serialized ? chunkPreview(serialized) : null;
}

function orchestrationItemLabel(value: unknown): string {
  if (typeof value === "string" && value.trim()) return chunkPreview(value);
  if (typeof value === "number" || typeof value === "boolean")
    return String(value);
  if (Array.isArray(value)) return `list(${value.length})`;
  if (isRecord(value)) {
    for (const key of [
      "label",
      "name",
      "title",
      "id",
      "path",
      "object_key",
      "key",
    ]) {
      const candidate = readString(value[key]);
      if (candidate) return chunkPreview(candidate);
    }
  }
  return chunkPreview(compactJson(value) ?? "item");
}

export function extractKnowledgeQueryDiagnostics(
  step: PreviewStep,
): KnowledgeQueryDiagnostics | null {
  if (step.node_type !== "knowledge_query") return null;
  const ports = isRecord(step.output_by_port) ? step.output_by_port : null;
  if (!ports) return null;

  const result = isRecord(ports.result) ? ports.result : null;
  const versions = Array.isArray(result?.versions)
    ? result.versions.filter(isRecord)
    : [];
  const primary = versions[0] ?? null;
  const graphContext = isRecord(ports.graph_context)
    ? ports.graph_context
    : primary && isRecord(primary.graph_context)
      ? primary.graph_context
      : null;
  const citations =
    recordArray(ports.citations).length > 0
      ? recordArray(ports.citations)
      : primary
        ? recordArray(primary.citations)
        : [];
  const chunks =
    recordArray(ports.chunks).length > 0
      ? recordArray(ports.chunks)
      : primary
        ? recordArray(primary.retrieved_chunks)
        : [];
  const retrievalMode = readString(primary?.retrieval_mode);

  if (
    !graphContext &&
    citations.length === 0 &&
    chunks.length === 0 &&
    !retrievalMode &&
    versions.length === 0
  ) {
    return null;
  }

  return {
    retrievalMode,
    resultCount: versions.length,
    citations: citations.map((item, index) => ({
      chunk_id: readString(item.chunk_id) ?? `citation-${index}`,
      label:
        readString(item.label) ??
        readString(item.source_key) ??
        `Citation ${index + 1}`,
    })),
    chunks: chunks.map((item, index) => ({
      chunk_id: readString(item.chunk_id) ?? `chunk-${index}`,
      source_name:
        readString(item.source_name) ??
        readString(item.source_key) ??
        `Chunk ${index + 1}`,
      source_key: readString(item.source_key) ?? "",
      score: readNumber(item.score),
      content: readString(item.content) ?? "",
      matched_entity_labels: stringList(item.matched_entity_labels),
    })),
    matchedEntities: stringList(graphContext?.matched_entities),
    expandedEntities: stringList(graphContext?.expanded_entities),
    ageGraphName: readString(graphContext?.age_graph_name),
    ageStatus: readString(graphContext?.age_status),
    ageSeedStrategy: readString(graphContext?.age_seed_strategy),
    ageMatchedChunkCount: readNumber(graphContext?.age_matched_chunk_count),
    ageTraversalHops: readNumber(graphContext?.age_traversal_hops),
    ageCandidatePoolSize: readNumber(graphContext?.age_candidate_pool_size),
    ageDenseRerankWeight: readNumber(graphContext?.age_dense_rerank_weight),
    retrievalStrength: readString(graphContext?.retrieval_strength),
    minimumRelationshipWeight: readNumber(
      graphContext?.minimum_relationship_weight,
    ),
    fallbackReason: readString(graphContext?.fallback_reason),
    ageFallbackReason: readString(graphContext?.age_fallback_reason),
    fallbackRetrievalMode: readString(graphContext?.fallback_retrieval_mode),
    strictAgeRetrieval: readBoolean(graphContext?.strict_age_retrieval),
    queryOverrideActive: readBoolean(graphContext?.query_override_active),
  };
}

export function extractKnowledgeBuildDiagnostics(
  step: PreviewStep,
): KnowledgeBuildDiagnostics | null {
  if (step.node_type !== "knowledge_build") return null;
  const ports = isRecord(step.output_by_port) ? step.output_by_port : null;
  if (!ports) return null;

  const result = isRecord(ports.result) ? ports.result : null;
  const knowledgeBase =
    isRecord(ports.knowledge_base)
      ? ports.knowledge_base
      : isRecord(result?.knowledge_base)
        ? result.knowledge_base
        : null;
  const version =
    isRecord(ports.version)
      ? ports.version
      : isRecord(result?.version)
        ? result.version
        : null;
  const run =
    isRecord(ports.run)
      ? ports.run
      : isRecord(result?.run)
        ? result.run
        : null;
  const awaitCompletion = isRecord(result?.await_completion)
    ? result.await_completion
    : null;
  const activation = isRecord(result?.activation) ? result.activation : null;
  const versionSummary = isRecord(version?.summary) ? version.summary : null;
  const graphConfig = isRecord(version?.graph_config) ? version.graph_config : null;
  const status =
    readString(ports.status) ??
    readString(result?.status) ??
    readString(version?.status);
  const previewSkipped = status === "preview_skipped" || readBoolean(result?.preview);
  const diagnostics: KnowledgeBuildDiagnostics = {
    knowledgeBaseId:
      readString(knowledgeBase?.knowledge_base_id) ??
      readString(result?.knowledge_base_id),
    activeVersionId:
      readString(activation?.active_version_id) ??
      readString(knowledgeBase?.active_version_id),
    versionId:
      readString(ports.version_id) ??
      readString(version?.knowledge_base_version_id),
    versionNumber: readNumber(version?.version_number),
    status,
    runId: readString(ports.run_id) ?? readString(run?.knowledge_base_run_id),
    runStatus: readString(run?.status),
    chunkingStrategy: readString(version?.chunking_strategy),
    embeddingModel: readString(version?.embedding_model),
    waitRequested: readBoolean(awaitCompletion?.requested),
    waitStatus: readString(awaitCompletion?.status),
    waitTimeoutSeconds: readNumber(awaitCompletion?.timeout_seconds),
    activationRequested: readBoolean(activation?.requested),
    activationStatus: readString(activation?.status),
    activationActiveVersionId: readString(activation?.active_version_id),
    graphTarget: readString(graphConfig?.output_target),
    graphExtractor: readString(graphConfig?.extractor_backend),
    defaultRetrievalMode: readString(graphConfig?.default_retrieval_mode),
    retrievalStrength: readString(graphConfig?.retrieval_strength),
    ageSyncStatus: readString(versionSummary?.age_sync_status),
    previewSkipped,
  };

  if (
    !diagnostics.knowledgeBaseId &&
    !diagnostics.activeVersionId &&
    !diagnostics.versionId &&
    diagnostics.versionNumber === null &&
    !diagnostics.status &&
    !diagnostics.runId &&
    !diagnostics.runStatus &&
    !diagnostics.chunkingStrategy &&
    !diagnostics.embeddingModel &&
    !diagnostics.waitRequested &&
    !diagnostics.waitStatus &&
    diagnostics.waitTimeoutSeconds === null &&
    !diagnostics.activationRequested &&
    !diagnostics.activationStatus &&
    !diagnostics.activationActiveVersionId &&
    !diagnostics.graphTarget &&
    !diagnostics.graphExtractor &&
    !diagnostics.defaultRetrievalMode &&
    !diagnostics.retrievalStrength &&
    !diagnostics.ageSyncStatus &&
    !diagnostics.previewSkipped
  ) {
    return null;
  }
  return diagnostics;
}

export function extractStepTelemetry(
  step: PreviewStep,
): StepTelemetryDiagnostics | null {
  const telemetry: StepTelemetryDiagnostics = {
    tokens: readNumber(step.tokens),
    promptTokens: readNumber(step.prompt_tokens),
    completionTokens: readNumber(step.completion_tokens),
    cachedPromptTokens: readNumber(step.cached_prompt_tokens),
    costUsd: readNumber(step.cost_usd),
    model: readString(step.model),
    promptVersion: readString(step.prompt_version),
  };

  if (
    telemetry.tokens === null &&
    telemetry.promptTokens === null &&
    telemetry.completionTokens === null &&
    telemetry.cachedPromptTokens === null &&
    telemetry.costUsd === null &&
    !telemetry.model &&
    !telemetry.promptVersion
  ) {
    return null;
  }
  return telemetry;
}

export function extractToolNodeDiagnostics(
  step: PreviewStep,
): ToolNodeDiagnostics | null {
  if (step.node_type !== "tool") return null;
  const ports = isRecord(step.output_by_port) ? step.output_by_port : null;
  const metadata = isRecord(ports?.metadata) ? ports.metadata : null;
  const portToolCalls = recordArray(ports?.tool_calls);
  const stepToolCalls = recordArray(step.tool_calls);
  const toolCalls = portToolCalls.length > 0 ? portToolCalls : stepToolCalls;
  const primaryCall = toolCalls[0] ?? null;
  const argumentsValue = isRecord(metadata?.arguments)
    ? metadata.arguments
    : primaryCall && isRecord(primaryCall.arguments)
      ? primaryCall.arguments
      : null;
  const diagnostics: ToolNodeDiagnostics = {
    localName:
      readString(metadata?.tool_name) ??
      (primaryCall ? readString(primaryCall.tool) : null),
    registryRef:
      readString(metadata?.registry_ref) ??
      (primaryCall ? readString(primaryCall.registry_ref) : null),
    bindingType:
      readString(metadata?.binding_type) ??
      (primaryCall ? readString(primaryCall.binding_type) : null),
    callCount: toolCalls.length,
    requiresApproval: readBoolean(metadata?.requires_approval),
    sideEffectLevel: readString(metadata?.side_effect_level),
    modulePath: readString(metadata?.module_path),
    callableName: readString(metadata?.callable_name),
    serverId: readString(metadata?.server_id),
    remoteToolName: readString(metadata?.remote_tool_name),
    argumentKeys: argumentsValue ? Object.keys(argumentsValue) : [],
    resultPreview: toolResultPreview(
      ports?.text ?? ports?.result ?? primaryCall?.result ?? step.output,
    ),
  };

  if (
    !diagnostics.localName &&
    !diagnostics.registryRef &&
    !diagnostics.bindingType &&
    diagnostics.argumentKeys.length === 0 &&
    !diagnostics.resultPreview
  ) {
    return null;
  }
  return diagnostics;
}

export function extractForEachDiagnostics(
  step: PreviewStep,
): ForEachDiagnostics | null {
  if (step.node_type !== "for_each" && step.node_type !== "loop") return null;
  const ports = isRecord(step.output_by_port) ? step.output_by_port : null;
  if (!ports) return null;

  const metadata = isRecord(ports.metadata) ? ports.metadata : null;
  const results = recordArray(ports.results);
  const artifactMap = isRecord(metadata?.artifacts) ? metadata.artifacts : null;
  const count = readNumber(metadata?.count) ?? results.length;
  const failed =
    readNumber(metadata?.failed) ??
    results.filter((item) => readString(item.error)).length;
  const diagnostics: ForEachDiagnostics = {
    count,
    failed,
    targetNodeId: readString(metadata?.target_node_id),
    targetNodeType: readString(metadata?.target_node_type),
    artifactCount: artifactMap ? Object.keys(artifactMap).length : 0,
    results: results.slice(0, 3).map((item) => ({
      itemLabel: orchestrationItemLabel(item.item),
      status:
        readString(item.status) ?? (readString(item.error) ? "error" : "ok"),
      error: readString(item.error),
      outputPreview: toolResultPreview(item.output),
      toolCallCount: recordArray(item.tool_calls).length,
      artifactCount: Array.isArray(item.artifacts) ? item.artifacts.length : 0,
    })),
  };

  if (
    diagnostics.count === 0 &&
    diagnostics.failed === 0 &&
    !diagnostics.targetNodeId &&
    !diagnostics.targetNodeType &&
    diagnostics.artifactCount === 0 &&
    diagnostics.results.length === 0
  ) {
    return null;
  }
  return diagnostics;
}

export function extractJoinDiagnostics(
  step: PreviewStep,
): JoinDiagnostics | null {
  if (step.node_type !== "join") return null;
  const ports = isRecord(step.output_by_port) ? step.output_by_port : null;
  if (!ports) return null;
  const merged = isRecord(ports.merged) ? ports.merged : null;
  const mergedKeys = merged ? Object.keys(merged).sort() : [];
  const diagnostics: JoinDiagnostics = {
    branchCount: mergedKeys.length,
    mergedKeys,
    outputPreview: toolResultPreview(ports.output ?? step.output),
  };
  if (diagnostics.branchCount === 0 && !diagnostics.outputPreview) {
    return null;
  }
  return diagnostics;
}

export function extractErrorBoundaryDiagnostics(
  step: PreviewStep,
): ErrorBoundaryDiagnostics | null {
  if (step.node_type !== "error_boundary") return null;
  const ports = isRecord(step.output_by_port) ? step.output_by_port : null;
  if (!ports) return null;
  const errorPayload = isRecord(ports.error) ? ports.error : null;
  const compensationOutputs = isRecord(errorPayload?.compensation_outputs)
    ? errorPayload.compensation_outputs
    : null;
  const artifactMap = isRecord(errorPayload?.artifacts)
    ? errorPayload.artifacts
    : null;
  const diagnostics: ErrorBoundaryDiagnostics = {
    message: readString(errorPayload?.message),
    targetNodeId: readString(errorPayload?.target_node_id),
    targetNodeType: readString(errorPayload?.target_node_type),
    compensationNodeId: readString(errorPayload?.compensation_node_id),
    compensationNodeType: readString(errorPayload?.compensation_node_type),
    compensationOutputPreview: toolResultPreview(
      compensationOutputs ?? ports.output,
    ),
    artifactCount: artifactMap ? Object.keys(artifactMap).length : 0,
  };

  if (
    !diagnostics.message &&
    !diagnostics.targetNodeId &&
    !diagnostics.targetNodeType &&
    !diagnostics.compensationNodeId &&
    !diagnostics.compensationNodeType &&
    diagnostics.artifactCount === 0
  ) {
    return null;
  }
  return diagnostics;
}

function storageNodeCountValue(
  value: unknown,
  fallback: number,
): number | null {
  const parsed = readNumber(value);
  if (parsed !== null) return parsed;
  return fallback > 0 ? fallback : null;
}

function storageEntriesFromRecords(
  entries: Record<string, unknown>[],
): StorageEntryPreview[] {
  return entries.slice(0, 4).map((item, index) => {
    const secondary =
      readString(item.path) ?? readString(item.key) ?? readString(item.relative_path);
    const label =
      readString(item.relative_path) ??
      readString(item.key) ??
      readString(item.path) ??
      `Entry ${index + 1}`;
    return {
      label,
      secondary: secondary && secondary !== label ? secondary : null,
      bytes: readNumber(item.bytes),
      truncated: readBoolean(item.truncated),
    };
  });
}

function storageEntriesFromStrings(entries: string[]): StorageEntryPreview[] {
  return entries.slice(0, 4).map((entry) => ({
    label: pathLeaf(entry),
    secondary: entry,
    bytes: null,
    truncated: false,
  }));
}

export function storageItemUnit(nodeType: string): string {
  if (nodeType === "input_bucket" || nodeType === "output_bucket") {
    return "object";
  }
  return "file";
}

export function storageCountLabel(
  nodeType: string,
  count: number | null,
): string | null {
  if (count === null) return null;
  const unit = storageItemUnit(nodeType);
  return `${count} ${unit}${count === 1 ? "" : "s"}`;
}

export function extractStorageNodeDiagnostics(
  step: PreviewStep,
): StorageNodeDiagnostics | null {
  const supported = new Set([
    "file_input",
    "folder_input",
    "input_bucket",
    "output_bucket",
    "output_folder",
  ]);
  if (!supported.has(step.node_type)) return null;
  const ports = isRecord(step.output_by_port) ? step.output_by_port : null;
  const metadata = isRecord(ports?.metadata) ? ports.metadata : null;

  if (step.node_type === "file_input") {
    const path = readString(ports?.path) ?? readString(metadata?.path);
    const bytes = readNumber(metadata?.bytes);
    const diagnostics: StorageNodeDiagnostics = {
      nodeType: step.node_type,
      direction: "input",
      path,
      bucket: null,
      prefix: null,
      pattern: null,
      recursive: false,
      encoding: readString(metadata?.encoding),
      count: 1,
      matchedCount: 1,
      skippedCount: null,
      truncatedList: false,
      entries: path
        ? [
            {
              label: pathLeaf(path),
              secondary: path,
              bytes,
              truncated: readBoolean(metadata?.truncated),
            },
          ]
        : [],
    };
    if (!diagnostics.path && diagnostics.entries.length === 0) return null;
    return diagnostics;
  }

  if (step.node_type === "folder_input") {
    const files = recordArray(ports?.files);
    const diagnostics: StorageNodeDiagnostics = {
      nodeType: step.node_type,
      direction: "input",
      path: readString(metadata?.path),
      bucket: null,
      prefix: null,
      pattern: readString(metadata?.pattern),
      recursive: readBoolean(metadata?.recursive),
      encoding: readString(metadata?.encoding),
      count: storageNodeCountValue(metadata?.file_count, files.length),
      matchedCount: readNumber(metadata?.matched_count),
      skippedCount: null,
      truncatedList: readBoolean(metadata?.truncated_file_list),
      entries: storageEntriesFromRecords(files),
    };
    if (!diagnostics.path && diagnostics.entries.length === 0) return null;
    return diagnostics;
  }

  if (step.node_type === "input_bucket") {
    const files = recordArray(ports?.files);
    const diagnostics: StorageNodeDiagnostics = {
      nodeType: step.node_type,
      direction: "input",
      path: null,
      bucket: readString(metadata?.bucket),
      prefix: readString(metadata?.prefix),
      pattern: null,
      recursive: readBoolean(metadata?.recursive),
      encoding: readString(metadata?.encoding),
      count: storageNodeCountValue(metadata?.object_count, files.length),
      matchedCount: null,
      skippedCount: readNumber(metadata?.skipped_object_count),
      truncatedList: readBoolean(metadata?.truncated_file_list),
      entries: storageEntriesFromRecords(files),
    };
    if (!diagnostics.bucket && diagnostics.entries.length === 0) return null;
    return diagnostics;
  }

  if (step.node_type === "output_bucket") {
    const keys = stringList(ports?.keys).length
      ? stringList(ports?.keys)
      : stringList(metadata?.keys);
    const diagnostics: StorageNodeDiagnostics = {
      nodeType: step.node_type,
      direction: "output",
      path: null,
      bucket: readString(metadata?.bucket),
      prefix: readString(metadata?.prefix),
      pattern: null,
      recursive: false,
      encoding: null,
      count: storageNodeCountValue(metadata?.object_count, keys.length),
      matchedCount: null,
      skippedCount: null,
      truncatedList: false,
      entries: storageEntriesFromStrings(keys),
    };
    if (!diagnostics.bucket && diagnostics.entries.length === 0) return null;
    return diagnostics;
  }

  const files = stringList(ports?.files).length
    ? stringList(ports?.files)
    : stringList(metadata?.files);
  const diagnostics: StorageNodeDiagnostics = {
    nodeType: step.node_type,
    direction: "output",
    path: readString(metadata?.path),
    bucket: null,
    prefix: null,
    pattern: null,
    recursive: false,
    encoding: null,
    count: storageNodeCountValue(metadata?.file_count, files.length),
    matchedCount: null,
    skippedCount: null,
    truncatedList: false,
    entries: storageEntriesFromStrings(files),
  };
  if (!diagnostics.path && diagnostics.entries.length === 0) return null;
  return diagnostics;
}

export function subworkflowStatusLabel(status: string | null): string | null {
  if (!status) return null;
  if (status === "completed") return "Child completed";
  if (status === "blocked") return "Child blocked";
  if (status === "error") return "Child failed";
  return `Child ${status.replaceAll("_", " ")}`;
}

export function subworkflowStatusTone(status: string | null): string {
  if (status === "completed") {
    return "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-800/70 dark:bg-emerald-950/30 dark:text-emerald-200";
  }
  if (status === "blocked") {
    return "border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-800/70 dark:bg-amber-950/30 dark:text-amber-200";
  }
  if (status) {
    return "border-rose-200 bg-rose-50 text-rose-700 dark:border-rose-800/70 dark:bg-rose-950/30 dark:text-rose-200";
  }
  return "border-zinc-200 bg-zinc-50 text-zinc-600 dark:border-zinc-800 dark:bg-zinc-950/70 dark:text-zinc-300";
}

export function extractSubworkflowDiagnostics(
  step: PreviewStep,
): SubworkflowDiagnostics | null {
  if (step.node_type !== "subworkflow") return null;
  const ports = isRecord(step.output_by_port) ? step.output_by_port : null;
  const result = isRecord(ports?.result) ? ports.result : null;
  const steps = stringList(result?.steps);
  const diagnostics: SubworkflowDiagnostics = {
    childStatus: readString(result?.status),
    workflowId: readString(result?.workflow_id),
    alias: readString(result?.alias),
    workflowVersionId:
      readString(result?.workflow_version_id) ?? readString(result?.version_id),
    workflowVersionNumber:
      readNumber(result?.workflow_version_number) ??
      readNumber(result?.version_number),
    tokens: readNumber(result?.tokens),
    steps,
    stepCount: steps.length,
    outputPreview: toolResultPreview(
      result?.output ?? ports?.output ?? step.output,
    ),
    error: readString(result?.error),
  };

  if (
    !diagnostics.childStatus &&
    !diagnostics.workflowId &&
    !diagnostics.alias &&
    !diagnostics.workflowVersionId &&
    diagnostics.workflowVersionNumber === null &&
    diagnostics.tokens === null &&
    diagnostics.steps.length === 0 &&
    !diagnostics.outputPreview &&
    !diagnostics.error
  ) {
    return null;
  }
  return diagnostics;
}

function toneForKnowledgeMode(mode: string | null): string {
  if (mode === "age_graph") {
    return "border-emerald-200 bg-emerald-50/70 text-emerald-900 dark:border-emerald-800/70 dark:bg-emerald-950/30 dark:text-emerald-100";
  }
  if (mode === "graph_hybrid") {
    return "border-sky-200 bg-sky-50/70 text-sky-900 dark:border-sky-800/70 dark:bg-sky-950/30 dark:text-sky-100";
  }
  return "border-zinc-200 bg-zinc-50/80 text-zinc-800 dark:border-zinc-800 dark:bg-zinc-900/70 dark:text-zinc-100";
}

function toneForKnowledgeBuildStatus(
  status: string | null,
  previewSkipped: boolean,
): string {
  if (previewSkipped || status === "preview_skipped") {
    return "border-zinc-200 bg-zinc-50/80 text-zinc-800 dark:border-zinc-800 dark:bg-zinc-900/70 dark:text-zinc-100";
  }
  if (status === "completed") {
    return "border-emerald-200 bg-emerald-50/70 text-emerald-900 dark:border-emerald-800/70 dark:bg-emerald-950/30 dark:text-emerald-100";
  }
  if (status === "queued" || status === "processing") {
    return "border-sky-200 bg-sky-50/70 text-sky-900 dark:border-sky-800/70 dark:bg-sky-950/30 dark:text-sky-100";
  }
  if (status === "failed") {
    return "border-rose-200 bg-rose-50/70 text-rose-900 dark:border-rose-800/70 dark:bg-rose-950/30 dark:text-rose-100";
  }
  return "border-zinc-200 bg-zinc-50/80 text-zinc-800 dark:border-zinc-800 dark:bg-zinc-900/70 dark:text-zinc-100";
}

export function StepPreview({
  step,
  upstream = [],
}: StepPreviewProps): JSX.Element {
  const storageNode = extractStorageNodeDiagnostics(step);
  const knowledgeQuery = extractKnowledgeQueryDiagnostics(step);
  const knowledgeBuild = extractKnowledgeBuildDiagnostics(step);
  const stepTelemetry = extractStepTelemetry(step);
  const toolNode = extractToolNodeDiagnostics(step);
  const forEachNode = extractForEachDiagnostics(step);
  const joinNode = extractJoinDiagnostics(step);
  const errorBoundary = extractErrorBoundaryDiagnostics(step);
  const subworkflowNode = extractSubworkflowDiagnostics(step);
  const knowledgeModeLabel = retrievalModeLabel(
    knowledgeQuery?.retrievalMode ?? null,
  );
  const knowledgeBuildStatus = knowledgeBuildStatusLabel(
    knowledgeBuild?.status ?? null,
    knowledgeBuild?.previewSkipped ?? false,
  );
  const knowledgeBuildWaitLabel = knowledgeBuild
    ? knowledgeBuildWaitStatusLabel(
        knowledgeBuild.waitStatus,
        knowledgeBuild.waitRequested,
      )
    : null;
  const knowledgeBuildActivationLabel = knowledgeBuild
    ? knowledgeBuildActivationStatusLabel(
        knowledgeBuild.activationStatus,
        knowledgeBuild.activationActiveVersionId,
        knowledgeBuild.activationRequested,
      )
    : null;
  const knowledgeBuildGraphTargetLabel = knowledgeGraphTargetLabel(
    knowledgeBuild?.graphTarget ?? null,
  );
  const knowledgeBuildDefaultRetrievalLabel = retrievalModeLabel(
    knowledgeBuild?.defaultRetrievalMode ?? null,
  );
  const seedStrategyLabel = ageSeedStrategyLabel(
    knowledgeQuery?.ageSeedStrategy ?? null,
  );
  const toolBindingLabel = toolBindingTypeLabel(toolNode?.bindingType ?? null);
  const toolBindingTarget = toolBindingTargetLabel(toolNode);
  const toolEffectLabel = toolSideEffectLabel(
    toolNode?.sideEffectLevel ?? null,
  );
  const forEachTargetLabel = forEachNode
    ? [
        forEachNode.targetNodeId,
        workflowNodeTypeLabel(forEachNode.targetNodeType),
      ]
        .filter(Boolean)
        .join(" · ")
    : null;
  const errorBoundaryTargetLabel = errorBoundary
    ? [
        errorBoundary.targetNodeId,
        workflowNodeTypeLabel(errorBoundary.targetNodeType),
      ]
        .filter(Boolean)
        .join(" · ")
    : null;
  const compensationLabel = errorBoundary
    ? [
        errorBoundary.compensationNodeId,
        workflowNodeTypeLabel(errorBoundary.compensationNodeType),
      ]
        .filter(Boolean)
        .join(" · ")
    : null;
  const storageCount = storageCountLabel(
    storageNode?.nodeType ?? "",
    storageNode?.count ?? null,
  );
  const storageLocation = storageNode?.path
    ? storageNode.path
    : storageNode?.bucket
      ? [
          storageNode.bucket,
          storageNode.prefix ? storageNode.prefix.replace(/^\/+/, "") : null,
        ]
          .filter(Boolean)
          .join("/")
      : null;
  const subworkflowStatus = subworkflowStatusLabel(
    subworkflowNode?.childStatus ?? null,
  );
  const subworkflowPath =
    subworkflowNode && subworkflowNode.steps.length > 0
      ? subworkflowNode.steps.join(" -> ")
      : null;

  return (
    <div data-testid="step-preview" className="space-y-2 text-xs">
      <div className="flex items-center justify-between gap-2">
        <span className="truncate font-mono font-semibold text-zinc-800 dark:text-zinc-100">
          {step.node_id}
        </span>
        <span
          data-testid="step-preview-status"
          className={`shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-semibold ${stepStatusStyle(step.status)}`}
        >
          {step.status}
        </span>
      </div>

      <div
        data-testid="step-preview-change"
        className="rounded-lg border border-amber-100 bg-amber-50/60 px-2.5 py-1.5 text-amber-800 dark:border-amber-800/70 dark:bg-amber-950/30 dark:text-amber-200"
      >
        <span className="font-semibold">What changed: </span>
        {describeStepChange(step, {
          storageNode,
          knowledgeQuery,
          knowledgeBuild,
          toolNode,
          forEachNode,
          joinNode,
          errorBoundary,
          subworkflowNode,
        })}
      </div>

      {upstream.length > 0 && (
        <details data-testid="step-preview-input">
          <summary className="cursor-pointer text-zinc-500 dark:text-zinc-400">
            Input ({upstream.length})
          </summary>
          <div className="mt-1 space-y-1">
            {upstream.map((u) => (
              <pre
                key={u.nodeId}
                className="max-h-24 overflow-auto whitespace-pre-wrap rounded border border-zinc-100 bg-zinc-50 p-2 font-mono text-[11px] text-zinc-600 dark:border-zinc-800 dark:bg-zinc-950/70 dark:text-zinc-300"
              >
                <span className="text-zinc-400 dark:text-zinc-500">
                  {u.nodeId}:{" "}
                </span>
                {u.output || "—"}
              </pre>
            ))}
          </div>
        </details>
      )}

      {toolNode && (
        <div
          data-testid="step-preview-tool-node"
          className="rounded-lg border border-violet-200 bg-violet-50/70 px-3 py-3 text-violet-950 dark:border-violet-800/70 dark:bg-violet-950/30 dark:text-violet-100"
        >
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div>
              <div className="text-[10px] font-semibold uppercase tracking-[0.16em] opacity-70">
                Tool Execution
              </div>
              <div className="mt-1 text-[11px] font-semibold">
                {toolNode.localName ?? "Direct tool node"}
              </div>
            </div>
            {toolNode.callCount > 1 && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 text-[10px] font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
                {toolNode.callCount} calls
              </span>
            )}
          </div>

          <div className="mt-2 flex flex-wrap gap-1.5 text-[10px]">
            {toolBindingLabel && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
                {toolBindingLabel}
              </span>
            )}
            {toolNode.registryRef && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-mono font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
                {toolNode.registryRef}
              </span>
            )}
            {toolEffectLabel && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-semibold capitalize dark:border-zinc-800 dark:bg-zinc-950/80">
                {toolEffectLabel}
              </span>
            )}
            {toolNode.requiresApproval && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
                Approval required
              </span>
            )}
          </div>

          <div className="mt-3 grid gap-2 sm:grid-cols-2">
            <div>
              <div className="text-[10px] font-semibold uppercase tracking-[0.14em] opacity-70">
                Binding
              </div>
              <div className="mt-1 text-[11px] leading-relaxed opacity-90">
                {toolBindingTarget ?? "Runtime-managed binding"}
              </div>
            </div>
            <div>
              <div className="text-[10px] font-semibold uppercase tracking-[0.14em] opacity-70">
                Arguments
              </div>
              <div className="mt-1 text-[11px] leading-relaxed opacity-90">
                {toolArgumentSummary(toolNode.argumentKeys)}
              </div>
            </div>
          </div>

          {toolNode.resultPreview && (
            <div className="mt-3">
              <div className="text-[10px] font-semibold uppercase tracking-[0.14em] opacity-70">
                Result preview
              </div>
              <div className="mt-1 text-[11px] leading-relaxed opacity-90">
                {toolNode.resultPreview}
              </div>
            </div>
          )}
        </div>
      )}

      {storageNode && (
        <div
          data-testid="step-preview-storage-node"
          className="rounded-lg border border-sky-200 bg-sky-50/70 px-3 py-3 text-sky-950 dark:border-sky-800/70 dark:bg-sky-950/30 dark:text-sky-100"
        >
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div>
              <div className="text-[10px] font-semibold uppercase tracking-[0.16em] opacity-70">
                Storage I/O
              </div>
              <div className="mt-1 text-[11px] font-semibold">
                {workflowNodeTypeLabel(step.node_type) ?? "Storage node"}
              </div>
            </div>
            {storageCount && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 text-[10px] font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
                {storageCount}
              </span>
            )}
          </div>

          <div className="mt-2 flex flex-wrap gap-1.5 text-[10px]">
            {storageNode.bucket && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
                Bucket {storageNode.bucket}
              </span>
            )}
            {storageNode.prefix && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
                Prefix {storageNode.prefix}
              </span>
            )}
            {storageNode.pattern && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
                Pattern {storageNode.pattern}
              </span>
            )}
            {storageNode.recursive && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
                Recursive
              </span>
            )}
            {storageNode.encoding && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
                {storageNode.encoding}
              </span>
            )}
            {storageNode.truncatedList && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
                Listing truncated
              </span>
            )}
            {storageNode.skippedCount !== null && storageNode.skippedCount > 0 && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
                Skipped {storageNode.skippedCount}
              </span>
            )}
          </div>

          {storageLocation && (
            <div className="mt-3 text-[11px] leading-relaxed opacity-90">
              <span className="font-semibold">Location:</span> {storageLocation}
            </div>
          )}

          {storageNode.matchedCount !== null &&
            storageNode.count !== null &&
            storageNode.matchedCount !== storageNode.count && (
              <div className="mt-2 text-[11px] leading-relaxed opacity-90">
                Matched {storageNode.matchedCount} total{" "}
                {storageItemUnit(storageNode.nodeType)}
                {storageNode.matchedCount === 1 ? "" : "s"}.
              </div>
            )}

          {storageNode.skippedCount !== null && storageNode.skippedCount > 0 && (
            <div className="mt-2 text-[11px] leading-relaxed opacity-90">
              Skipped {storageNode.skippedCount} unreadable{" "}
              {storageItemUnit(storageNode.nodeType)}
              {storageNode.skippedCount === 1 ? "" : "s"} while preserving the
              readable entries.
            </div>
          )}

          {storageNode.entries.length > 0 && (
            <div className="mt-3 space-y-2">
              {storageNode.entries.map((entry, index) => (
                <div
                  key={`${entry.label}-${index}`}
                  className="rounded-lg border border-white/70 bg-white/80 px-2.5 py-2 dark:border-zinc-800 dark:bg-zinc-950/80"
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="font-semibold">{entry.label}</div>
                    <div className="flex flex-wrap gap-1.5 text-[10px]">
                      {entry.bytes !== null && (
                        <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-semibold dark:border-zinc-700 dark:bg-zinc-900/90">
                          {entry.bytes} bytes
                        </span>
                      )}
                      {entry.truncated && (
                        <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-semibold dark:border-zinc-700 dark:bg-zinc-900/90">
                          Truncated
                        </span>
                      )}
                    </div>
                  </div>
                  {entry.secondary && (
                    <div className="mt-1 font-mono text-[10px] opacity-70">
                      {entry.secondary}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {knowledgeQuery && (
        <div
          data-testid="step-preview-knowledge-query"
          className={`rounded-lg border px-3 py-3 ${toneForKnowledgeMode(knowledgeQuery.retrievalMode)}`}
        >
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div>
              <div className="text-[10px] font-semibold uppercase tracking-[0.16em] opacity-70">
                Knowledge Retrieval
              </div>
              <div className="mt-1 text-[11px] font-semibold">
                {knowledgeModeLabel ?? "Workflow knowledge query"}
              </div>
            </div>
            {knowledgeQuery.resultCount > 1 && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 text-[10px] font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
                {knowledgeQuery.resultCount} results
              </span>
            )}
          </div>

          <div className="mt-2 flex flex-wrap gap-1.5 text-[10px]">
            {knowledgeModeLabel && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
                {knowledgeModeLabel}
              </span>
            )}
            {knowledgeQuery.ageGraphName && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-mono font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
                {knowledgeQuery.ageGraphName}
              </span>
            )}
            {knowledgeQuery.ageStatus && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-semibold uppercase dark:border-zinc-800 dark:bg-zinc-950/80">
                AGE {knowledgeQuery.ageStatus}
              </span>
            )}
            {knowledgeQuery.fallbackRetrievalMode && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
                Fallback{" "}
                {retrievalModeLabel(knowledgeQuery.fallbackRetrievalMode) ??
                  knowledgeQuery.fallbackRetrievalMode}
              </span>
            )}
            {knowledgeQuery.strictAgeRetrieval && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
                Strict AGE
              </span>
            )}
            {seedStrategyLabel && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
                {seedStrategyLabel}
              </span>
            )}
          </div>

          {(knowledgeQuery.matchedEntities.length > 0 ||
            knowledgeQuery.expandedEntities.length > 0) && (
            <div className="mt-3 grid gap-2 sm:grid-cols-2">
              <div>
                <div className="text-[10px] font-semibold uppercase tracking-[0.14em] opacity-70">
                  Matched entities
                </div>
                <div className="mt-1 flex flex-wrap gap-1.5">
                  {knowledgeQuery.matchedEntities.length > 0 ? (
                    knowledgeQuery.matchedEntities.map((label) => (
                      <span
                        key={`matched-${label}`}
                        className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 text-[10px] font-semibold dark:border-zinc-800 dark:bg-zinc-950/80"
                      >
                        {label}
                      </span>
                    ))
                  ) : (
                    <span className="text-[11px] opacity-80">
                      No direct matches
                    </span>
                  )}
                </div>
              </div>
              <div>
                <div className="text-[10px] font-semibold uppercase tracking-[0.14em] opacity-70">
                  Expanded neighborhood
                </div>
                <div className="mt-1 flex flex-wrap gap-1.5">
                  {knowledgeQuery.expandedEntities.length > 0 ? (
                    knowledgeQuery.expandedEntities.map((label) => (
                      <span
                        key={`expanded-${label}`}
                        className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 text-[10px] font-semibold dark:border-zinc-800 dark:bg-zinc-950/80"
                      >
                        {label}
                      </span>
                    ))
                  ) : (
                    <span className="text-[11px] opacity-80">
                      No expanded entities
                    </span>
                  )}
                </div>
              </div>
            </div>
          )}

          {(knowledgeQuery.queryOverrideActive ||
            knowledgeQuery.retrievalStrength ||
            knowledgeQuery.ageTraversalHops !== null ||
            knowledgeQuery.ageCandidatePoolSize !== null ||
            knowledgeQuery.ageDenseRerankWeight !== null ||
            knowledgeQuery.minimumRelationshipWeight !== null) && (
            <div className="mt-3 text-[11px] opacity-90">
              <span className="font-semibold">Graph tuning:</span>
              {knowledgeQuery.queryOverrideActive
                ? " query override"
                : " build default"}
              {knowledgeQuery.retrievalStrength
                ? ` · ${knowledgeQuery.retrievalStrength}`
                : ""}
              {knowledgeQuery.ageTraversalHops !== null
                ? ` · ${knowledgeQuery.ageTraversalHops} hop`
                : ""}
              {knowledgeQuery.ageCandidatePoolSize !== null
                ? ` · pool ${knowledgeQuery.ageCandidatePoolSize}`
                : ""}
              {knowledgeQuery.ageDenseRerankWeight !== null
                ? ` · dense x${knowledgeQuery.ageDenseRerankWeight.toFixed(2)}`
                : ""}
              {knowledgeQuery.minimumRelationshipWeight !== null
                ? ` · min weight ${knowledgeQuery.minimumRelationshipWeight}`
                : ""}
            </div>
          )}

          {(knowledgeQuery.fallbackReason ||
            knowledgeQuery.ageFallbackReason) && (
            <div className="mt-3 rounded-lg border border-amber-200/80 bg-amber-50/80 px-2.5 py-2 text-[11px] text-amber-800 dark:border-amber-800/70 dark:bg-amber-950/30 dark:text-amber-200">
              {knowledgeQuery.ageFallbackReason ??
                knowledgeQuery.fallbackReason}
            </div>
          )}

          {knowledgeQuery.citations.length > 0 && (
            <div
              data-testid="step-preview-knowledge-citations"
              className="mt-3"
            >
              <div className="text-[10px] font-semibold uppercase tracking-[0.14em] opacity-70">
                Citations
              </div>
              <div className="mt-1 flex flex-wrap gap-1.5">
                {knowledgeQuery.citations.slice(0, 4).map((citation) => (
                  <span
                    key={citation.chunk_id}
                    className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 text-[10px] font-semibold dark:border-zinc-800 dark:bg-zinc-950/80"
                  >
                    {citation.label}
                  </span>
                ))}
              </div>
            </div>
          )}

          {knowledgeQuery.chunks.length > 0 && (
            <div
              data-testid="step-preview-knowledge-chunks"
              className="mt-3 space-y-2"
            >
              <div className="flex flex-wrap items-center justify-between gap-2 text-[10px] font-semibold uppercase tracking-[0.14em] opacity-70">
                <span>Retrieved chunks</span>
                {knowledgeQuery.ageMatchedChunkCount !== null && (
                  <span>
                    Matched {knowledgeQuery.ageMatchedChunkCount} before rerank
                  </span>
                )}
              </div>
              {knowledgeQuery.chunks.slice(0, 2).map((chunk) => (
                <div
                  key={chunk.chunk_id}
                  className="rounded-lg border border-white/70 bg-white/80 px-2.5 py-2 dark:border-zinc-800 dark:bg-zinc-950/80"
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="font-semibold">{chunk.source_name}</div>
                    {chunk.score !== null && (
                      <span className="text-[10px] font-semibold opacity-80">
                        score {chunk.score.toFixed(3)}
                      </span>
                    )}
                  </div>
                  {chunk.source_key && (
                    <div className="mt-1 font-mono text-[10px] opacity-70">
                      {chunk.source_key}
                    </div>
                  )}
                  {chunk.matched_entity_labels.length > 0 && (
                    <div className="mt-1 flex flex-wrap gap-1.5">
                      {chunk.matched_entity_labels.slice(0, 4).map((label) => (
                        <span
                          key={`${chunk.chunk_id}-${label}`}
                          className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 text-[10px] font-semibold dark:border-zinc-800 dark:bg-zinc-900/90"
                        >
                          {label}
                        </span>
                      ))}
                    </div>
                  )}
                  {chunk.content && (
                    <div className="mt-2 text-[11px] leading-relaxed opacity-90">
                      {chunkPreview(chunk.content)}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {knowledgeBuild && (
        <div
          data-testid="step-preview-knowledge-build"
          className={`rounded-lg border px-3 py-3 ${toneForKnowledgeBuildStatus(
            knowledgeBuild.status,
            knowledgeBuild.previewSkipped,
          )}`}
        >
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div>
              <div className="text-[10px] font-semibold uppercase tracking-[0.16em] opacity-70">
                Knowledge Build
              </div>
              <div className="mt-1 text-[11px] font-semibold">
                {knowledgeBuildStatus ?? "Workflow knowledge build"}
              </div>
            </div>
            {knowledgeBuild.versionNumber !== null && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 text-[10px] font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
                v{knowledgeBuild.versionNumber}
              </span>
            )}
          </div>

          <div className="mt-2 flex flex-wrap gap-1.5 text-[10px]">
            {knowledgeBuildStatus && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
                {knowledgeBuildStatus}
              </span>
            )}
            {knowledgeBuild.knowledgeBaseId && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
                KB {knowledgeBuild.knowledgeBaseId}
              </span>
            )}
            {knowledgeBuild.versionId && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-mono font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
                {knowledgeBuild.versionId}
              </span>
            )}
            {knowledgeBuild.runId && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-mono font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
                {knowledgeBuild.runId}
              </span>
            )}
            {knowledgeBuildWaitLabel && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
                {knowledgeBuildWaitLabel}
              </span>
            )}
            {knowledgeBuildActivationLabel && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
                {knowledgeBuildActivationLabel}
              </span>
            )}
            {knowledgeBuildGraphTargetLabel && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
                {knowledgeBuildGraphTargetLabel}
              </span>
            )}
            {knowledgeBuildDefaultRetrievalLabel && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
                {knowledgeBuildDefaultRetrievalLabel}
              </span>
            )}
            {knowledgeBuild.ageSyncStatus && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-semibold uppercase dark:border-zinc-800 dark:bg-zinc-950/80">
                AGE {knowledgeBuild.ageSyncStatus}
              </span>
            )}
          </div>

          {(knowledgeBuild.chunkingStrategy || knowledgeBuild.embeddingModel) && (
            <div className="mt-3 text-[11px] opacity-90">
              <span className="font-semibold">Build profile:</span>{" "}
              {[knowledgeBuild.chunkingStrategy, knowledgeBuild.embeddingModel]
                .filter(Boolean)
                .join(" · ")}
            </div>
          )}

          {(knowledgeBuild.graphExtractor ||
            knowledgeBuildGraphTargetLabel ||
            knowledgeBuildDefaultRetrievalLabel ||
            knowledgeBuild.retrievalStrength) && (
            <div className="mt-2 text-[11px] opacity-90">
              <span className="font-semibold">Graph profile:</span>{" "}
              {[
                knowledgeBuild.graphExtractor,
                knowledgeBuildGraphTargetLabel,
                knowledgeBuildDefaultRetrievalLabel,
                knowledgeBuild.retrievalStrength,
              ]
                .filter(Boolean)
                .join(" · ")}
            </div>
          )}

          {(knowledgeBuildWaitLabel || knowledgeBuild.waitTimeoutSeconds !== null) && (
            <div className="mt-2 text-[11px] opacity-90">
              <span className="font-semibold">Wait policy:</span>{" "}
              {[knowledgeBuildWaitLabel, knowledgeBuild.waitTimeoutSeconds !== null
                ? `${knowledgeBuild.waitTimeoutSeconds}s timeout`
                : null]
                .filter(Boolean)
                .join(" · ")}
            </div>
          )}

          {(knowledgeBuildActivationLabel || knowledgeBuild.activeVersionId) && (
            <div className="mt-2 text-[11px] opacity-90">
              <span className="font-semibold">Activation:</span>{" "}
              {[knowledgeBuildActivationLabel, knowledgeBuild.activeVersionId
                ? `Active ${knowledgeBuild.activeVersionId}`
                : null]
                .filter(Boolean)
                .join(" · ")}
            </div>
          )}
        </div>
      )}

      {forEachNode && (
        <div
          data-testid="step-preview-for-each"
          className="rounded-lg border border-teal-200 bg-teal-50/70 px-3 py-3 text-teal-950 dark:border-teal-800/70 dark:bg-teal-950/30 dark:text-teal-100"
        >
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div>
              <div className="text-[10px] font-semibold uppercase tracking-[0.16em] opacity-70">
                Loop Orchestration
              </div>
              <div className="mt-1 text-[11px] font-semibold">
                {workflowNodeTypeLabel(step.node_type) ?? "For-each loop"}
              </div>
            </div>
            <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 text-[10px] font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
              {forEachNode.count} item{forEachNode.count === 1 ? "" : "s"}
            </span>
          </div>

          <div className="mt-2 flex flex-wrap gap-1.5 text-[10px]">
            {forEachTargetLabel && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
                Target {forEachTargetLabel}
              </span>
            )}
            {forEachNode.failed > 0 && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
                {forEachNode.failed} failed
              </span>
            )}
            {forEachNode.artifactCount > 0 && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
                Artifact bundle {forEachNode.artifactCount}
              </span>
            )}
          </div>

          {forEachNode.results.length > 0 && (
            <div className="mt-3 space-y-2">
              {forEachNode.results.map((result, index) => (
                <div
                  key={`${result.itemLabel}-${index}`}
                  className="rounded-lg border border-white/70 bg-white/80 px-2.5 py-2 dark:border-zinc-800 dark:bg-zinc-950/80"
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="font-semibold">{result.itemLabel}</div>
                    <div className="flex flex-wrap gap-1.5 text-[10px]">
                      {result.status && (
                        <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-semibold capitalize dark:border-zinc-700 dark:bg-zinc-900/90">
                          {result.status}
                        </span>
                      )}
                      {result.toolCallCount > 0 && (
                        <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-semibold dark:border-zinc-700 dark:bg-zinc-900/90">
                          {result.toolCallCount} tool
                        </span>
                      )}
                      {result.artifactCount > 0 && (
                        <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-semibold dark:border-zinc-700 dark:bg-zinc-900/90">
                          {result.artifactCount} artifact
                        </span>
                      )}
                    </div>
                  </div>
                  {result.error && (
                    <div className="mt-1 text-[11px] leading-relaxed text-rose-700 dark:text-rose-200">
                      {result.error}
                    </div>
                  )}
                  {result.outputPreview && (
                    <div className="mt-1 text-[11px] leading-relaxed opacity-90">
                      {result.outputPreview}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {joinNode && (
        <div
          data-testid="step-preview-join"
          className="rounded-lg border border-indigo-200 bg-indigo-50/70 px-3 py-3 text-indigo-950 dark:border-indigo-800/70 dark:bg-indigo-950/30 dark:text-indigo-100"
        >
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div>
              <div className="text-[10px] font-semibold uppercase tracking-[0.16em] opacity-70">
                Branch Merge
              </div>
              <div className="mt-1 text-[11px] font-semibold">
                {workflowNodeTypeLabel(step.node_type) ?? "Join"}
              </div>
            </div>
            <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 text-[10px] font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
              {joinNode.branchCount} merged port
              {joinNode.branchCount === 1 ? "" : "s"}
            </span>
          </div>

          {joinNode.mergedKeys.length > 0 && (
            <div className="mt-3">
              <div className="text-[10px] font-semibold uppercase tracking-[0.14em] opacity-70">
                Merged keys
              </div>
              <div className="mt-1 flex flex-wrap gap-1.5">
                {joinNode.mergedKeys.map((key) => (
                  <span
                    key={key}
                    className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 text-[10px] font-semibold dark:border-zinc-800 dark:bg-zinc-950/80"
                  >
                    {key}
                  </span>
                ))}
              </div>
            </div>
          )}

          {joinNode.outputPreview && (
            <div className="mt-3 text-[11px] leading-relaxed opacity-90">
              {joinNode.outputPreview}
            </div>
          )}
        </div>
      )}

      {errorBoundary && (
        <div
          data-testid="step-preview-error-boundary"
          className="rounded-lg border border-amber-200 bg-amber-50/70 px-3 py-3 text-amber-950 dark:border-amber-800/70 dark:bg-amber-950/30 dark:text-amber-100"
        >
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div>
              <div className="text-[10px] font-semibold uppercase tracking-[0.16em] opacity-70">
                Failure Recovery
              </div>
              <div className="mt-1 text-[11px] font-semibold">
                {workflowNodeTypeLabel(step.node_type) ?? "Error boundary"}
              </div>
            </div>
            <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 text-[10px] font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
              Handled failure
            </span>
          </div>

          <div className="mt-2 flex flex-wrap gap-1.5 text-[10px]">
            {errorBoundaryTargetLabel && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
                Protected {errorBoundaryTargetLabel}
              </span>
            )}
            {compensationLabel && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
                Compensation {compensationLabel}
              </span>
            )}
            {errorBoundary.artifactCount > 0 && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
                Artifact bundle {errorBoundary.artifactCount}
              </span>
            )}
          </div>

          {errorBoundary.message && (
            <div className="mt-3 rounded-lg border border-white/70 bg-white/80 px-2.5 py-2 text-[11px] leading-relaxed dark:border-zinc-800 dark:bg-zinc-950/80">
              {errorBoundary.message}
            </div>
          )}

          {errorBoundary.compensationOutputPreview && (
            <div className="mt-3 text-[11px] leading-relaxed opacity-90">
              Recovery output: {errorBoundary.compensationOutputPreview}
            </div>
          )}
        </div>
      )}

      {subworkflowNode && (
        <div
          data-testid="step-preview-subworkflow"
          className="rounded-lg border border-cyan-200 bg-cyan-50/70 px-3 py-3 text-cyan-950 dark:border-cyan-800/70 dark:bg-cyan-950/30 dark:text-cyan-100"
        >
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div>
              <div className="text-[10px] font-semibold uppercase tracking-[0.16em] opacity-70">
                Child Workflow
              </div>
              <div className="mt-1 text-[11px] font-semibold">
                {workflowNodeTypeLabel(step.node_type) ?? "Subworkflow"}
              </div>
            </div>
            {subworkflowStatus && (
              <span
                className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold ${subworkflowStatusTone(subworkflowNode.childStatus)}`}
              >
                {subworkflowStatus}
              </span>
            )}
          </div>

          <div className="mt-2 flex flex-wrap gap-1.5 text-[10px]">
            {subworkflowNode.workflowId && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
                {subworkflowNode.workflowId}
              </span>
            )}
            {subworkflowNode.alias && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
                Alias {subworkflowNode.alias}
              </span>
            )}
            {subworkflowNode.stepCount > 0 && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
                {subworkflowNode.stepCount} child step
                {subworkflowNode.stepCount === 1 ? "" : "s"}
              </span>
            )}
            {subworkflowNode.tokens !== null && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
                {subworkflowNode.tokens} tokens
              </span>
            )}
          </div>

          {(subworkflowNode.workflowVersionId ||
            subworkflowNode.workflowVersionNumber !== null) && (
            <div className="mt-3 text-[11px] leading-relaxed opacity-90">
              <span className="font-semibold">Version:</span>{" "}
              {subworkflowNode.workflowVersionId ?? "n/a"}
              {subworkflowNode.workflowVersionNumber !== null
                ? ` · v${subworkflowNode.workflowVersionNumber}`
                : ""}
            </div>
          )}

          {subworkflowPath && (
            <div className="mt-3 text-[11px] leading-relaxed opacity-90">
              <span className="font-semibold">Path:</span> {subworkflowPath}
            </div>
          )}

          {subworkflowNode.error && (
            <div className="mt-3 rounded-lg border border-rose-200/80 bg-rose-50/80 px-2.5 py-2 text-[11px] text-rose-800 dark:border-rose-800/70 dark:bg-rose-950/30 dark:text-rose-200">
              Failure: {subworkflowNode.error}
            </div>
          )}

          {subworkflowNode.outputPreview && (
            <div className="mt-3 text-[11px] leading-relaxed opacity-90">
              Child output: {subworkflowNode.outputPreview}
            </div>
          )}
        </div>
      )}

      {stepTelemetry && (
        <div
          data-testid="step-preview-telemetry"
          className="rounded-lg border border-slate-200 bg-slate-50/80 px-3 py-3 text-slate-900 dark:border-slate-800 dark:bg-slate-950/60 dark:text-slate-100"
        >
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div>
              <div className="text-[10px] font-semibold uppercase tracking-[0.16em] opacity-70">
                LLM Telemetry
              </div>
              <div className="mt-1 text-[11px] font-semibold">
                Prompt, model, and token usage
              </div>
            </div>
            {stepTelemetry.tokens !== null && (
              <span className="rounded-full border border-white/70 bg-white/80 px-2 py-0.5 text-[10px] font-semibold dark:border-zinc-800 dark:bg-zinc-950/80">
                {stepTelemetry.tokens} tokens
              </span>
            )}
          </div>

          <div className="mt-2 flex flex-wrap gap-1.5 text-[10px]">
            {stepTelemetry.model && (
              <span className="rounded-full border border-violet-200 bg-violet-50 px-2 py-0.5 font-semibold text-violet-700 dark:border-violet-800/70 dark:bg-violet-950/40 dark:text-violet-100">
                {stepTelemetry.model}
              </span>
            )}
            {stepTelemetry.promptVersion && (
              <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5 font-semibold text-slate-600 dark:border-zinc-800 dark:bg-zinc-950/80 dark:text-zinc-100">
                Prompt {stepTelemetry.promptVersion}
              </span>
            )}
            {stepTelemetry.promptTokens !== null && (
              <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5 font-semibold text-slate-600 dark:border-zinc-800 dark:bg-zinc-950/80 dark:text-zinc-100">
                {stepTelemetry.promptTokens} prompt
              </span>
            )}
            {stepTelemetry.completionTokens !== null && (
              <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5 font-semibold text-slate-600 dark:border-zinc-800 dark:bg-zinc-950/80 dark:text-zinc-100">
                {stepTelemetry.completionTokens} completion
              </span>
            )}
            {stepTelemetry.cachedPromptTokens !== null && (
              <span className="rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 font-semibold text-emerald-700 dark:border-emerald-800/70 dark:bg-emerald-950/40 dark:text-emerald-100">
                {stepTelemetry.cachedPromptTokens} cached prompt
              </span>
            )}
            {stepTelemetry.costUsd !== null && stepTelemetry.costUsd > 0 && (
              <span className="rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 font-semibold text-amber-700 dark:border-amber-800/70 dark:bg-amber-950/40 dark:text-amber-100">
                Est. {formatUsdEstimate(stepTelemetry.costUsd)}
              </span>
            )}
          </div>
        </div>
      )}

      <div>
        <div className="mb-0.5 text-[10px] uppercase tracking-wider text-zinc-400 dark:text-zinc-500">
          Output
        </div>
        <pre
          data-testid="step-preview-output"
          className="max-h-40 overflow-auto whitespace-pre-wrap rounded-lg border border-zinc-200 bg-zinc-50 p-2.5 font-mono text-[11px] text-zinc-700 dark:border-zinc-800 dark:bg-zinc-950/70 dark:text-zinc-200"
        >
          {step.output || "—"}
        </pre>
      </div>

      <div className="flex flex-wrap items-center gap-3 text-[10px] text-zinc-400 dark:text-zinc-500">
        {step.tool_calls && step.tool_calls.length > 0 && (
          <span data-testid="step-preview-tools">
            🛠 {step.tool_calls.length} tool call(s)
          </span>
        )}
        {typeof step.duration_ms === "number" && (
          <span>⏱ {step.duration_ms} ms</span>
        )}
        <span>{step.node_type}</span>
      </div>
    </div>
  );
}
