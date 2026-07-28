/**
 * Inspector panel — n8n-inspired component inspection panel.
 *
 * Monochromatic design with type-accented header, clean sectioned forms, and
 * data-type colored port indicators. Shows node configuration when a node is
 * selected, workflow-level settings otherwise.
 */

import { Plus, X } from "lucide-react";
import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import type {
  ManifestNode,
  McpServer,
  PortSpec,
  PreviewStep,
  StartTriggerConfig,
  StartTriggerMode,
  ToolDefinition,
  ValidationIssue,
  ValidationReport,
  WorkflowComponent,
  WorkflowComponentField,
  WorkflowManifest,
  WorkflowArtifactsConfig,
  WorkflowMcpToolBinding,
  Workflow,
  WorkflowCronPreview,
  WorkflowDeployGate,
  WorkflowDeployment,
  WorkflowEvalDatasetArtifact,
  WorkflowFileList,
  WorkflowNodeType,
  WorkflowPromptArtifact,
  WorkflowRegisteredFunctionToolBinding,
  WorkflowRuntimeConfig,
  WorkflowRuntimeOpenAIConfig,
  WorkflowSessionMode,
  WorkflowToolBinding,
  WorkflowVersion,
} from "@/api/workflowTypes";
import { caliberApi } from "@/api/caliberApi";
import { DEPLOYMENT_ALIASES } from "@/lib/environment";
import type {
  KnowledgeBase,
  KnowledgeBaseVersion,
  KnowledgeGraphQueryPreset,
  KnowledgeOptions,
  KnowledgeQueryGraphOverrides,
  KnowledgeRetrievalMode,
} from "@/api/knowledgeTypes";
import type { EvalDataset, PromptInfo, Skill } from "@/api/types";
import {
  BucketContentsField,
  BucketPrefixField,
  BucketSelect,
} from "@/components/workflows/BucketSelect";
import { AgentHandoffEditor } from "@/components/workflows/AgentHandoffEditor";
import { ChipMultiSelect } from "@/components/workflows/ChipMultiSelect";
import { GuardrailChecksEditor } from "@/components/workflows/GuardrailChecksEditor";
import { useApiQuery } from "@/hooks/useApiQuery";
import {
  RouterConditionBuilder,
  routerTargets,
  type RouterBranch,
} from "@/components/workflows/RouterConditionBuilder";
import {
  nodeColor,
  nodeFieldSetupChecks,
  nodeFieldValidationIssues,
  nodeGuide,
  nodeValidationIssues,
  type NodeGuideCheck,
} from "@/lib/workflowGraph";
import {
  buildGraphQueryPresetState,
  fallbackGraphQueryPresets,
  graphQueryOverridesFromState,
  matchGraphQueryPreset,
  resolveGraphQueryPresetState,
} from "@/lib/knowledgeGraphProfiles";
import { NodeIcon } from "@/components/workflows/NodeIcon";
import { WorkflowComponentSchemaSummary } from "@/components/workflows/WorkflowComponentSchemaSummary";

interface InspectorProps {
  manifest: WorkflowManifest;
  projectId?: string | null;
  selectedNodeId: string | null;
  focusFieldKey?: string | null;
  focusFieldSignal?: number;
  tools: ToolDefinition[];
  prompts?: PromptInfo[];
  skills?: Skill[];
  evalDatasets?: EvalDataset[];
  mcpServers?: McpServer[];
  componentSpec?: WorkflowComponent | null;
  validationReport?: ValidationReport | null;
  /** The selected node's most recent preview/run step, if any (Output section). */
  lastStep?: PreviewStep | null;
  onChangeNode: (nodeId: string, patch: Partial<ManifestNode>) => void;
  onChangeWorkflow: (patch: Partial<WorkflowManifest>) => void;
  onDeleteNode?: (nodeId: string) => void;
}

const SIDE_EFFECT_BADGE: Record<string, string> = {
  read: "🟢",
  write: "🟡",
  external_action: "🔴",
};

const SIDE_EFFECT_LABEL: Record<string, string> = {
  read: "Read-only",
  write: "Write",
  external_action: "External",
};

const inputClass =
  "w-full rounded-lg border border-zinc-200 bg-white px-3 py-1.5 text-sm text-zinc-900 transition-colors hover:border-zinc-300 focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900 placeholder:text-zinc-400";

const textareaClass =
  "w-full rounded-lg border border-zinc-200 bg-white px-3 py-2 text-sm font-mono leading-relaxed text-zinc-900 transition-colors hover:border-zinc-300 focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900";

const selectClass =
  "w-full rounded-lg border border-zinc-200 bg-white px-3 py-1.5 text-sm text-zinc-900 transition-colors hover:border-zinc-300 focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900";

function ManagedProjectFileSelect({
  projectId,
  value,
  onSelect,
}: {
  projectId: string;
  value: string;
  onSelect: (file: WorkflowFileList["items"][number] | null) => void;
}): JSX.Element {
  const projectFilesQuery = useApiQuery<WorkflowFileList>(
    ["project-files", projectId, "workflow-file-selector"],
    (signal) => caliberApi.listProjectFiles(projectId, signal),
  );
  const files = (projectFilesQuery.data?.items ?? []).filter((file) =>
    Boolean(file.immutable_ref),
  );
  return (
    <>
      <select
        data-testid="inspector-managed-file"
        className={selectClass}
        disabled={projectFilesQuery.isLoading}
        value={value}
        onChange={(event) =>
          onSelect(
            files.find((file) => file.file_id === event.target.value) ?? null,
          )
        }
      >
        <option value="">Select a content-pinned file…</option>
        {files.map((file) => (
          <option key={file.file_id} value={file.file_id}>
            {file.name} · {file.sha256?.slice(0, 12)}
          </option>
        ))}
      </select>
      {!projectFilesQuery.isLoading && files.length === 0 && (
        <p className="mt-1 text-[11px] text-zinc-400">
          Upload a project file or add one from Object Store first.
        </p>
      )}
    </>
  );
}

type TargetOption = {
  nodeId: string;
  unsupported: boolean;
};

const EXECUTABLE_TARGET_TYPES = new Set<WorkflowNodeType>([
  "agent",
  "subworkflow",
  "tool",
  "mcp_resource",
  "knowledge_query",
  "knowledge_build",
  "template",
  "python_code",
  "external_app",
]);
const FOR_EACH_TARGET_TYPES = EXECUTABLE_TARGET_TYPES;
const LOOP_TARGET_TYPES = EXECUTABLE_TARGET_TYPES;
const ERROR_BOUNDARY_TARGET_TYPES = EXECUTABLE_TARGET_TYPES;
// Alias autocomplete hints. In single-environment mode these collapse to the
// one live alias (see @/lib/environment); "manual" stays for subworkflows since
// it is a trigger mode, not a deployment stage.
const DEPLOY_GATE_ALIAS_HINTS = DEPLOYMENT_ALIASES;
const START_TRIGGER_ALIAS_HINTS = DEPLOYMENT_ALIASES;
const SUBWORKFLOW_ALIAS_HINTS = ["manual", ...DEPLOYMENT_ALIASES];
const COMPONENT_FIELD_CONTEXT = createContext<ReadonlyMap<
  string,
  WorkflowComponentField
> | null>(null);
const COMPONENT_FIELD_FEEDBACK_CONTEXT = createContext<ReadonlyMap<
  string,
  { issues: ValidationIssue[]; setupChecks: NodeGuideCheck[] }
> | null>(null);
const COMPONENT_FIELD_HIGHLIGHT_CONTEXT = createContext<string | null>(null);
// Whether advanced fields are revealed. Defaults to true so any field rendered
// outside the node inspector (and tests that don't wrap a provider) always show.
const COMPONENT_SHOW_ADVANCED_CONTEXT = createContext<boolean>(true);
const EMPTY_FIELD_FEEDBACK: {
  issues: ValidationIssue[];
  setupChecks: NodeGuideCheck[];
} = {
  issues: [],
  setupChecks: [],
};

function workflowSessionMode(manifest: WorkflowManifest): WorkflowSessionMode {
  const type = manifest.runtime?.session?.type;
  return type === "in_memory" || type === "persistent" ? type : "none";
}

function workflowRuntimeConfig(
  manifest: WorkflowManifest,
): WorkflowRuntimeConfig {
  return manifest.runtime ?? {};
}

function workflowRuntimeOpenAIConfig(
  runtime: WorkflowRuntimeConfig,
): WorkflowRuntimeOpenAIConfig {
  return runtime.openai ?? {};
}

function normalizeWorkflowRuntimeOpenAIConfig(
  value: WorkflowRuntimeOpenAIConfig,
): WorkflowRuntimeOpenAIConfig | undefined {
  const next: WorkflowRuntimeOpenAIConfig = {};
  if (typeof value.workflow_api === "string" && value.workflow_api) {
    next.workflow_api = value.workflow_api;
  }
  if (
    typeof value.parallel_tool_calls === "string" &&
    value.parallel_tool_calls
  ) {
    next.parallel_tool_calls = value.parallel_tool_calls;
  }
  if (typeof value.prompt_cache_mode === "string" && value.prompt_cache_mode) {
    next.prompt_cache_mode = value.prompt_cache_mode;
  }
  if (
    typeof value.prompt_cache_retention === "string" &&
    value.prompt_cache_retention
  ) {
    next.prompt_cache_retention = value.prompt_cache_retention;
  }
  return Object.keys(next).length > 0 ? next : undefined;
}

function knowledgeVersionAgeSyncStatus(
  version: Pick<KnowledgeBaseVersion, "summary"> | null | undefined,
): string {
  return String(version?.summary?.age_sync_status ?? "").toLowerCase();
}

function knowledgeVersionAgeReady(
  version:
    | Pick<KnowledgeBaseVersion, "graph_config" | "summary">
    | null
    | undefined,
): boolean {
  return Boolean(
    version &&
    version.graph_config.output_target === "object_store_and_age" &&
    knowledgeVersionAgeSyncStatus(version) === "synced",
  );
}

function knowledgeVersionAgeConfigured(
  version: Pick<KnowledgeBaseVersion, "graph_config"> | null | undefined,
): boolean {
  return Boolean(
    version && version.graph_config.output_target === "object_store_and_age",
  );
}

function knowledgeGraphTargetLabel(target: string | null | undefined): string {
  return target === "object_store_and_age"
    ? "Object store + AGE"
    : "Object store";
}

function knowledgeResolvedDefaultRetrievalMode(
  version:
    | Pick<KnowledgeBaseVersion, "graph_config" | "summary">
    | null
    | undefined,
  ageEnabled: boolean,
): KnowledgeRetrievalMode {
  const mode = version?.graph_config.default_retrieval_mode ?? "graph_hybrid";
  if (
    mode === "age_graph" &&
    (!ageEnabled || !knowledgeVersionAgeReady(version))
  ) {
    return "graph_hybrid";
  }
  return mode;
}

function knowledgeSummaryResolvedDefaultRetrievalMode(
  summary: KnowledgeBase["active_version_summary"] | null | undefined,
  ageEnabled: boolean,
): KnowledgeRetrievalMode {
  const mode = summary?.default_retrieval_mode ?? "graph_hybrid";
  if (mode === "age_graph" && (!ageEnabled || !summary?.age_ready)) {
    return "graph_hybrid";
  }
  return mode;
}

function knowledgeRetrievalModeLabel(
  mode: KnowledgeRetrievalMode,
  options: KnowledgeOptions | null | undefined,
): string {
  return (
    options?.retrieval_modes.find((item) => item.id === mode)?.name ??
    (mode === "dense"
      ? "Dense retrieval"
      : mode === "graph_hybrid"
        ? "GraphRAG hybrid"
        : "Apache AGE graph")
  );
}

function knowledgeAgeStatusLabel({
  ageEnabled,
  ageReady,
  ageConfigured,
  syncStatus,
}: {
  ageEnabled: boolean;
  ageReady: boolean;
  ageConfigured: boolean;
  syncStatus: string | null | undefined;
}): string {
  if (ageReady) return "AGE synced";
  if (!ageEnabled) return "AGE disabled";
  if (!ageConfigured) return "Object-store graph";
  if (syncStatus === "failed") return "AGE sync failed";
  if (
    syncStatus === "queued" ||
    syncStatus === "processing" ||
    syncStatus === "pending"
  ) {
    return "AGE syncing";
  }
  return "AGE pending";
}

function useComponentFieldMeta(
  fieldKey?: string,
): WorkflowComponentField | null {
  const fields = useContext(COMPONENT_FIELD_CONTEXT);
  if (!fieldKey || !fields) return null;
  return fields.get(fieldKey) ?? null;
}

function useComponentFieldFeedback(
  fieldKey?: string,
): Readonly<{ issues: ValidationIssue[]; setupChecks: NodeGuideCheck[] }> {
  const feedback = useContext(COMPONENT_FIELD_FEEDBACK_CONTEXT);
  if (!fieldKey || !feedback) return EMPTY_FIELD_FEEDBACK;
  return feedback.get(fieldKey) ?? EMPTY_FIELD_FEEDBACK;
}

function useHighlightedFieldKey(): string | null {
  return useContext(COMPONENT_FIELD_HIGHLIGHT_CONTEXT);
}

function useShowAdvanced(): boolean {
  return useContext(COMPONENT_SHOW_ADVANCED_CONTEXT);
}

function formatFieldMetaValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean")
    return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function hasMeaningfulFieldMetaValue(value: unknown): boolean {
  return value !== null && value !== undefined && value !== "";
}

function fieldConstraintTokens(field: WorkflowComponentField): string[] {
  const constraints = field.constraints ?? {};
  const tokens: string[] = [];
  if (typeof constraints.minimum === "number")
    tokens.push(`min ${constraints.minimum}`);
  if (typeof constraints.maximum === "number")
    tokens.push(`max ${constraints.maximum}`);
  if (typeof constraints.min_length === "number") {
    tokens.push(`min length ${constraints.min_length}`);
  }
  if (typeof constraints.max_length === "number") {
    tokens.push(`max length ${constraints.max_length}`);
  }
  if (typeof constraints.min_items === "number")
    tokens.push(`min items ${constraints.min_items}`);
  if (typeof constraints.max_items === "number")
    tokens.push(`max items ${constraints.max_items}`);
  if (typeof constraints.pattern === "string" && constraints.pattern) {
    tokens.push(`pattern ${constraints.pattern}`);
  }
  if (typeof constraints.multiple_of === "number")
    tokens.push(`step ${constraints.multiple_of}`);
  if (constraints.nullable === true) tokens.push("nullable");
  if (Array.isArray(constraints.options) && constraints.options.length > 0) {
    const preview = constraints.options
      .slice(0, 3)
      .map((item) => String(item))
      .join(", ");
    tokens.push(
      `options ${preview}${constraints.options.length > 3 ? "…" : ""}`,
    );
  }
  return tokens;
}

function fieldMetaTokens(
  field: WorkflowComponentField,
): Array<{ label: string; tone: "neutral" | "info" }> {
  const tokens: Array<{ label: string; tone: "neutral" | "info" }> = [];
  if (hasMeaningfulFieldMetaValue(field.default)) {
    tokens.push({
      label: `Default ${formatFieldMetaValue(field.default)}`,
      tone: "neutral",
    });
  }
  for (const token of fieldConstraintTokens(field)) {
    tokens.push({ label: token, tone: "neutral" });
  }
  for (const example of field.examples.slice(0, 2)) {
    tokens.push({
      label: `Example ${formatFieldMetaValue(example)}`,
      tone: "info",
    });
  }
  return tokens;
}

function formatJsonTextareaValue(value: unknown): string {
  if (value == null) return "";
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return "";
  }
}

function formatTraceGroupTagsText(
  value: Record<string, string> | null | undefined,
): string {
  return Object.entries(value ?? {})
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([key, item]) => `${key}=${item}`)
    .join("\n");
}

function parseTraceGroupTagsText(raw: string): Record<string, string> {
  const next: Record<string, string> = {};
  for (const line of raw.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const separator = trimmed.indexOf("=");
    if (separator <= 0) {
      throw new Error("Each trace tag line must use key=value format.");
    }
    const key = trimmed.slice(0, separator).trim();
    const value = trimmed.slice(separator + 1).trim();
    if (!key) {
      throw new Error("Trace tag keys cannot be empty.");
    }
    next[key] = value;
  }
  return next;
}

function hasAgentStructuredOutputPort(
  outputs?: Record<string, PortSpec>,
): boolean {
  if (!outputs) return false;
  return Object.entries(outputs).some(
    ([port, spec]) =>
      spec?.type === "structured" &&
      port !== "history" &&
      port !== "tool_calls",
  );
}

function pruneToolConstraints(
  constraints: Record<string, string> | undefined,
  tools: string[],
): Record<string, string> {
  const allowed = new Set(tools);
  return Object.fromEntries(
    Object.entries(constraints ?? {}).filter(
      ([toolRef, value]) => allowed.has(toolRef) && value,
    ),
  );
}

function targetNodeOptions(
  manifest: WorkflowManifest,
  options: {
    excludeNodeId: string;
    allowedTypes: ReadonlySet<WorkflowNodeType>;
    selectedNodeId: string | null;
  },
): TargetOption[] {
  const { excludeNodeId, allowedTypes, selectedNodeId } = options;
  const filteredOptions = Object.values(manifest.nodes)
    .filter(
      (candidate) =>
        candidate.id !== excludeNodeId && allowedTypes.has(candidate.type),
    )
    .map((candidate) => ({ nodeId: candidate.id, unsupported: false }));
  if (
    selectedNodeId &&
    !filteredOptions.some((option) => option.nodeId === selectedNodeId) &&
    manifest.nodes[selectedNodeId]
  ) {
    return [{ nodeId: selectedNodeId, unsupported: true }, ...filteredOptions];
  }
  return filteredOptions;
}

function targetOptionLabel(
  manifest: WorkflowManifest,
  option: TargetOption,
  options: {
    unsupportedLabel: string;
  },
): string {
  const { unsupportedLabel } = options;
  if (!option.unsupported) return option.nodeId;
  const candidate = manifest.nodes[option.nodeId];
  if (!candidate) return option.nodeId;
  return `${option.nodeId} (${unsupportedLabel}: ${candidate.type})`;
}

function uniqueTrimmedStrings(
  values: Array<string | null | undefined>,
): string[] {
  const seen = new Set<string>();
  const items: string[] = [];
  for (const value of values) {
    const next = String(value ?? "").trim();
    if (!next || seen.has(next)) continue;
    seen.add(next);
    items.push(next);
  }
  return items;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function workflowToolBindingTestId(prefix: string, localName: string): string {
  return `${prefix}-${localName.toLowerCase().replace(/[^a-z0-9]+/g, "-") || "binding"}`;
}

function parseNullablePositiveNumber(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

function parseNonNegativeInteger(value: string): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return 0;
  return Math.max(0, Math.floor(parsed));
}

function coerceWorkflowToolBinding(value: unknown): WorkflowToolBinding | null {
  if (!isRecord(value)) return null;
  if (
    value.type === "mcp_tool" ||
    (typeof value.server_id === "string" && typeof value.tool_name === "string")
  ) {
    if (
      typeof value.server_id !== "string" ||
      !value.server_id.trim() ||
      typeof value.tool_name !== "string" ||
      !value.tool_name.trim()
    ) {
      return null;
    }
    return {
      type: "mcp_tool",
      server_id: value.server_id,
      tool_name: value.tool_name,
      tool_schema_version:
        typeof value.tool_schema_version === "string"
          ? value.tool_schema_version
          : "",
      side_effect_level:
        value.side_effect_level === "write" ||
        value.side_effect_level === "external_action"
          ? value.side_effect_level
          : "read",
      requires_approval: value.requires_approval === true,
      timeout_seconds:
        typeof value.timeout_seconds === "number"
          ? value.timeout_seconds
          : null,
      max_retries:
        typeof value.max_retries === "number"
          ? parseNonNegativeInteger(String(value.max_retries))
          : 0,
    };
  }
  if (typeof value.registry_ref !== "string" || !value.registry_ref.trim()) {
    return null;
  }
  return {
    type:
      value.type === "registered_function" ? "registered_function" : undefined,
    registry_ref: value.registry_ref,
    version_constraint:
      typeof value.version_constraint === "string"
        ? value.version_constraint
        : "",
    requires_approval: value.requires_approval === true,
    secret_refs: Array.isArray(value.secret_refs)
      ? uniqueTrimmedStrings(
          value.secret_refs.map((item) =>
            typeof item === "string" ? item : null,
          ),
        )
      : [],
    timeout_seconds:
      typeof value.timeout_seconds === "number" ? value.timeout_seconds : null,
    max_retries:
      typeof value.max_retries === "number"
        ? parseNonNegativeInteger(String(value.max_retries))
        : 0,
  };
}

function workflowToolBindingEntries(manifest: WorkflowManifest): Array<{
  localName: string;
  binding: WorkflowToolBinding | null;
  raw: unknown;
}> {
  return Object.entries((manifest.tools ?? {}) as Record<string, unknown>)
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([localName, raw]) => ({
      localName,
      binding: coerceWorkflowToolBinding(raw),
      raw,
    }));
}

function workflowToolBindingReferences(
  manifest: WorkflowManifest,
): Record<string, string[]> {
  const refs = new Map<string, Set<string>>();
  for (const node of Object.values(manifest.nodes)) {
    const localNames =
      node.type === "agent"
        ? (node.tools ?? [])
        : node.type === "tool" &&
            typeof node.tool_name === "string" &&
            node.tool_name.trim()
          ? [node.tool_name.trim()]
          : [];
    for (const localName of localNames) {
      if (!refs.has(localName)) refs.set(localName, new Set<string>());
      refs.get(localName)?.add(node.id);
    }
  }
  return Object.fromEntries(
    Array.from(refs.entries()).map(([localName, nodeIds]) => [
      localName,
      Array.from(nodeIds).sort(),
    ]),
  );
}

function workflowToolBindingTypeLabel(
  binding: WorkflowToolBinding | null,
): string {
  if (!binding) return "Unsupported binding payload";
  return binding.type === "mcp_tool" ? "MCP tool" : "Registered function";
}

function workflowDeployGateTestId(prefix: string, gateName: string): string {
  return `${prefix}-${gateName.toLowerCase().replace(/[^a-z0-9]+/g, "-") || "gate"}`;
}

function parseOptionalNumber(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : null;
}

function workflowDeployGateEntries(
  manifest: WorkflowManifest,
): Array<[string, WorkflowDeployGate]> {
  return Object.entries(manifest.deploy_gates ?? {}).sort(([left], [right]) =>
    left.localeCompare(right),
  );
}

function workflowArtifacts(
  manifest: WorkflowManifest,
): WorkflowArtifactsConfig {
  return manifest.artifacts ?? {};
}

function workflowPromptArtifactTestId(
  prefix: string,
  promptRef: string,
): string {
  return `${prefix}-${
    promptRef
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-") || "prompt"
  }`;
}

function workflowPromptArtifacts(
  manifest: WorkflowManifest,
): Record<string, WorkflowPromptArtifact> {
  return { ...(workflowArtifacts(manifest).prompts ?? {}) };
}

function workflowPromptArtifactEntries(
  manifest: WorkflowManifest,
): Array<[string, WorkflowPromptArtifact]> {
  return Object.entries(workflowPromptArtifacts(manifest)).sort(
    ([left], [right]) => left.localeCompare(right),
  );
}

function workflowPromptArtifactReferences(
  manifest: WorkflowManifest,
): Record<string, string[]> {
  const refs = new Map<string, Set<string>>();
  for (const node of Object.values(manifest.nodes)) {
    if (node.type !== "agent") continue;
    const instructions = node.instructions;
    if (!instructions || instructions.type !== "mlflow_prompt") continue;
    const promptRef =
      typeof instructions.ref === "string" ? instructions.ref.trim() : "";
    if (!promptRef) continue;
    if (!refs.has(promptRef)) refs.set(promptRef, new Set<string>());
    refs.get(promptRef)?.add(node.id);
  }
  return Object.fromEntries(
    Array.from(refs.entries()).map(([promptRef, nodeIds]) => [
      promptRef,
      Array.from(nodeIds).sort(),
    ]),
  );
}

function workflowEvalDatasetArtifacts(
  manifest: WorkflowManifest,
): Record<string, WorkflowEvalDatasetArtifact> {
  return { ...(workflowArtifacts(manifest).eval_datasets ?? {}) };
}

function subworkflowWorkflowLabel(
  workflow: Pick<Workflow, "workflow_id" | "name" | "status">,
): string {
  const name = workflow.name.trim();
  const label =
    name && name !== workflow.workflow_id
      ? `${name} (${workflow.workflow_id})`
      : workflow.workflow_id;
  return workflow.status === "active" ? label : `${label} · ${workflow.status}`;
}

function aliasChipTestId(prefix: string, alias: string): string {
  return `${prefix}-${
    alias
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-") || "value"
  }`;
}

function subworkflowAliasTestId(alias: string): string {
  return aliasChipTestId("inspector-subworkflow-alias", alias);
}

function startTriggerAliasTestId(alias: string): string {
  return aliasChipTestId("inspector-start-alias", alias);
}

function subworkflowResolvedVersion(
  versions: WorkflowVersion[],
  alias: string,
  deployment: WorkflowDeployment | null,
): WorkflowVersion | null {
  if (alias === "manual") {
    return (
      [...versions].sort(
        (left, right) => right.version_number - left.version_number,
      )[0] ?? null
    );
  }
  if (!deployment) return null;
  return (
    versions.find((version) => version.version_id === deployment.version_id) ??
    null
  );
}

function workflowManifestStartTrigger(
  workflowVersion: WorkflowVersion | null,
): StartTriggerConfig | null {
  if (!workflowVersion) return null;
  const startNode = Object.values(workflowVersion.manifest.nodes).find(
    (candidate) => candidate.type === "start",
  );
  return startNode?.trigger ?? null;
}

function workflowTriggerSummary(
  trigger: StartTriggerConfig | null | undefined,
): string {
  const mode = trigger?.mode ?? "manual";
  if (mode === "event") {
    return trigger?.event_name
      ? `Event · ${trigger.event_name}`
      : "Event trigger";
  }
  if (mode === "cron") {
    return trigger?.cron ? `Cron · ${trigger.cron}` : "Cron trigger";
  }
  return "Manual trigger";
}

function workflowValidationCounts(workflowVersion: WorkflowVersion | null): {
  errors: number;
  warnings: number;
  valid: boolean;
} {
  const errors = workflowVersion?.validation_report?.errors.length ?? 0;
  const warnings = workflowVersion?.validation_report?.warnings.length ?? 0;
  return { errors, warnings, valid: errors === 0 };
}

function compactGraphOverrides(
  value: KnowledgeQueryGraphOverrides | null | undefined,
): KnowledgeQueryGraphOverrides | null {
  const next: KnowledgeQueryGraphOverrides = {};
  if (value?.retrieval_strength)
    next.retrieval_strength = value.retrieval_strength;
  if (typeof value?.minimum_relationship_weight === "number") {
    next.minimum_relationship_weight = value.minimum_relationship_weight;
  }
  if (value?.age_seed_mode) {
    next.age_seed_mode = value.age_seed_mode;
  }
  if (typeof value?.age_traversal_hops === "number") {
    next.age_traversal_hops = value.age_traversal_hops;
  }
  if (typeof value?.age_candidate_pool_size === "number") {
    next.age_candidate_pool_size = value.age_candidate_pool_size;
  }
  if (typeof value?.age_dense_rerank_weight === "number") {
    next.age_dense_rerank_weight = value.age_dense_rerank_weight;
  }
  if (value?.strict_age_retrieval) next.strict_age_retrieval = true;
  return Object.keys(next).length > 0 ? next : null;
}

function SubworkflowSection({
  manifest,
  node,
  onChangeNode,
}: {
  manifest: WorkflowManifest;
  node: ManifestNode;
  onChangeNode: (nodeId: string, patch: Partial<ManifestNode>) => void;
}): JSX.Element {
  const workflowId =
    typeof node.workflow_id === "string" ? node.workflow_id : "";
  const trimmedWorkflowId = workflowId.trim();
  const alias = typeof node.alias === "string" ? node.alias : "prod";
  const trimmedAlias = alias.trim();
  const parentWorkflowId = manifest.workflow_id.trim();
  const selfReference =
    Boolean(trimmedWorkflowId) && trimmedWorkflowId === parentWorkflowId;
  const workflowsQuery = useApiQuery<Workflow[]>(
    ["workflow-editor", "subworkflow-library"],
    (signal) => caliberApi.listWorkflows("all", signal),
    { enabled: node.type === "subworkflow" },
  );
  const knownWorkflows = workflowsQuery.data ?? [];
  const selectedWorkflow =
    knownWorkflows.find(
      (workflow) => workflow.workflow_id === trimmedWorkflowId,
    ) ?? null;
  const selectableWorkflows = knownWorkflows.filter(
    (workflow) => workflow.workflow_id !== parentWorkflowId,
  );
  const deploymentsQuery = useApiQuery<WorkflowDeployment[]>(
    [
      "workflow-editor",
      "subworkflow-library",
      trimmedWorkflowId,
      "deployments",
    ],
    (signal) => caliberApi.listWorkflowDeployments(trimmedWorkflowId, signal),
    {
      enabled:
        node.type === "subworkflow" &&
        Boolean(trimmedWorkflowId) &&
        !selfReference &&
        selectedWorkflow !== null,
    },
  );
  const activeDeployments = (deploymentsQuery.data ?? []).filter(
    (deployment) => deployment.status === "active",
  );
  const versionsQuery = useApiQuery<WorkflowVersion[]>(
    ["workflow-editor", "subworkflow-library", trimmedWorkflowId, "versions"],
    (signal) => caliberApi.listWorkflowVersions(trimmedWorkflowId, signal),
    {
      enabled:
        node.type === "subworkflow" &&
        Boolean(trimmedWorkflowId) &&
        !selfReference &&
        selectedWorkflow !== null,
    },
  );
  const aliasChoices = uniqueTrimmedStrings([
    ...SUBWORKFLOW_ALIAS_HINTS,
    ...activeDeployments.map((deployment) => deployment.alias),
    alias,
  ]);
  const selectedAliasDeployment =
    activeDeployments.find((deployment) => deployment.alias === trimmedAlias) ??
    null;
  const knownVersions = versionsQuery.data ?? [];
  const resolvedVersion = subworkflowResolvedVersion(
    knownVersions,
    trimmedAlias || "prod",
    selectedAliasDeployment,
  );
  const resolvedTrigger = workflowManifestStartTrigger(resolvedVersion);
  const resolvedValidation = workflowValidationCounts(resolvedVersion);
  const resolvedNodeCount = resolvedVersion
    ? Object.keys(resolvedVersion.manifest.nodes).length
    : 0;
  const resolvedOutputCount = resolvedVersion
    ? Object.values(resolvedVersion.manifest.nodes).filter(
        (candidate) => candidate.type === "output",
      ).length
    : 0;
  const resolvedVersionHelperText =
    trimmedAlias === "manual"
      ? "Manual runs resolve the latest saved child version directly."
      : selectedAliasDeployment
        ? `Alias ${trimmedAlias} resolves to deployment ${selectedAliasDeployment.deployment_id}.`
        : "Resolve an active alias to inspect the exact child version this node will execute.";

  let workflowHelperText =
    "Pick a published child workflow here or type a workflow_id below.";
  let workflowHelperToneClass = "text-zinc-500";
  if (workflowsQuery.isLoading) {
    workflowHelperText = "Loading the workflow library…";
  } else if (workflowsQuery.error) {
    workflowHelperText =
      "Workflow library is unavailable right now. You can still type a workflow_id below.";
    workflowHelperToneClass = "text-amber-600";
  } else if (selfReference) {
    workflowHelperText = `This node points back to the current workflow (${manifest.workflow_id}), which creates direct recursion and will fail validation.`;
    workflowHelperToneClass = "text-red-600";
  } else if (!trimmedWorkflowId) {
    workflowHelperText =
      "Pick a child workflow from the library or type one manually below.";
  } else if (selectedWorkflow) {
    workflowHelperText = `Selected child workflow ${subworkflowWorkflowLabel(selectedWorkflow)}.`;
  } else {
    workflowHelperText = `Using manual workflow ID ${trimmedWorkflowId}. It is not in the current workflow library response, so deployments cannot be checked here yet.`;
    workflowHelperToneClass = "text-amber-600";
  }

  let aliasHelperText =
    "Use manual to target the latest version directly, or choose a deployed alias like prod.";
  let aliasHelperToneClass = "text-zinc-500";
  if (selfReference) {
    aliasHelperText =
      "Choose a different child workflow before checking deployment aliases.";
    aliasHelperToneClass = "text-red-600";
  } else if (!trimmedWorkflowId) {
    aliasHelperText =
      "Pick or type a child workflow before checking deployment aliases.";
  } else if (workflowsQuery.error) {
    aliasHelperText =
      "Alias checks are unavailable until the workflow library loads again.";
    aliasHelperToneClass = "text-amber-600";
  } else if (!selectedWorkflow) {
    aliasHelperText =
      "This workflow ID is not in the current workflow library response yet, so alias checks are unavailable.";
    aliasHelperToneClass = "text-amber-600";
  } else if (deploymentsQuery.isLoading) {
    aliasHelperText = "Loading active deployments…";
  } else if (deploymentsQuery.error) {
    aliasHelperText =
      "Deployment aliases could not be loaded right now. You can still enter one manually.";
    aliasHelperToneClass = "text-amber-600";
  } else if (!trimmedAlias) {
    aliasHelperText =
      "Set an alias. Use manual to run the latest version directly or pick an active deployment alias.";
  } else if (trimmedAlias === "manual") {
    aliasHelperText =
      "Manual runs resolve the latest workflow version directly and do not require an active deployment alias.";
  } else if (selectedAliasDeployment) {
    aliasHelperText = `Alias ${trimmedAlias} resolves to active deployment ${selectedAliasDeployment.deployment_id}.`;
  } else if (activeDeployments.length === 0) {
    aliasHelperText = `No active deployments exist for ${trimmedWorkflowId} yet. Runtime calls with alias ${trimmedAlias} will fail until one is promoted.`;
    aliasHelperToneClass = "text-amber-600";
  } else {
    aliasHelperText = `No active deployment currently serves alias ${trimmedAlias}. Active aliases: ${uniqueTrimmedStrings(activeDeployments.map((deployment) => deployment.alias)).join(", ")}.`;
    aliasHelperToneClass = "text-amber-600";
  }

  return (
    <Section title="Subworkflow">
      <Field label="Workflow library">
        <>
          <select
            data-testid="inspector-subworkflow-workflow-shortcut"
            className={selectClass}
            value={
              selectedWorkflow && !selfReference
                ? selectedWorkflow.workflow_id
                : ""
            }
            onChange={(e) =>
              onChangeNode(node.id, { workflow_id: e.target.value })
            }
          >
            <option value="">Choose a child workflow</option>
            {selectableWorkflows.map((workflow) => (
              <option key={workflow.workflow_id} value={workflow.workflow_id}>
                {subworkflowWorkflowLabel(workflow)}
              </option>
            ))}
          </select>
          <div
            className={`mt-2 text-[11px] leading-relaxed ${workflowHelperToneClass}`}
          >
            {workflowHelperText}
          </div>
        </>
      </Field>
      <Field label="Workflow ID" fieldKey="workflow_id">
        <input
          className={inputClass}
          value={workflowId}
          onChange={(e) =>
            onChangeNode(node.id, { workflow_id: e.target.value })
          }
        />
      </Field>
      <Field label="Alias" fieldKey="alias">
        <>
          <input
            className={inputClass}
            value={alias}
            onChange={(e) => onChangeNode(node.id, { alias: e.target.value })}
          />
          <div className="mt-2 flex flex-wrap gap-2">
            {aliasChoices.map((choice) => (
              <button
                key={choice}
                type="button"
                data-testid={subworkflowAliasTestId(choice)}
                onClick={() => onChangeNode(node.id, { alias: choice })}
                className={`rounded-full border px-2.5 py-1 text-[11px] font-semibold transition ${
                  choice === trimmedAlias
                    ? "border-zinc-900 bg-zinc-900 text-white"
                    : "border-zinc-200 bg-white text-zinc-600 hover:border-zinc-300 hover:text-zinc-900"
                }`}
              >
                {choice}
              </button>
            ))}
          </div>
          <div
            className={`mt-2 text-[11px] leading-relaxed ${aliasHelperToneClass}`}
          >
            {aliasHelperText}
          </div>
        </>
      </Field>
      <Field label="Timeout (seconds)" fieldKey="timeout_seconds">
        <input
          className={inputClass}
          type="number"
          min={1}
          max={3600}
          value={
            typeof node.timeout_seconds === "number"
              ? node.timeout_seconds
              : 120
          }
          onChange={(e) =>
            onChangeNode(node.id, {
              timeout_seconds: Number(e.target.value) || 120,
            })
          }
        />
      </Field>
      <div
        data-testid="inspector-subworkflow-contract"
        className="rounded-xl border border-zinc-200 bg-zinc-50 px-3 py-3"
      >
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="text-[11px] font-semibold uppercase tracking-wider text-zinc-500">
              Resolved child contract
            </div>
            <div className="mt-1 text-[11px] leading-relaxed text-zinc-500">
              {resolvedVersionHelperText}
            </div>
          </div>
          {selectedWorkflow && (
            <a
              data-testid="inspector-subworkflow-open"
              href={`/workflows/${encodeURIComponent(selectedWorkflow.workflow_id)}`}
              className="rounded-lg border border-zinc-200 bg-white px-2.5 py-1 text-[11px] font-semibold text-zinc-600 transition-colors hover:border-zinc-300 hover:text-zinc-900"
            >
              Open workflow
            </a>
          )}
        </div>

        {versionsQuery.isLoading ? (
          <div className="mt-3 rounded-lg border border-zinc-200 bg-white px-3 py-2 text-[11px] text-zinc-500">
            Loading child workflow versions…
          </div>
        ) : versionsQuery.error ? (
          <div className="mt-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] leading-relaxed text-amber-700">
            Child workflow versions could not be loaded right now, so runtime
            contract details are temporarily unavailable.
          </div>
        ) : !selectedWorkflow ? (
          <div className="mt-3 rounded-lg border border-zinc-200 bg-white px-3 py-2 text-[11px] text-zinc-500">
            Pick a child workflow from the library to inspect its current
            runtime contract.
          </div>
        ) : !resolvedVersion ? (
          <div className="mt-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] leading-relaxed text-amber-700">
            {trimmedAlias === "manual"
              ? "This child workflow has no saved versions yet, so manual subworkflow calls cannot resolve."
              : `Alias ${trimmedAlias || "prod"} does not currently resolve to an active deployed child version.`}
          </div>
        ) : (
          <>
            <div className="mt-3 grid gap-2 sm:grid-cols-2">
              <div className="rounded-lg border border-zinc-200 bg-white px-3 py-2">
                <div className="text-[10px] font-semibold uppercase tracking-wider text-zinc-400">
                  Version
                </div>
                <div className="mt-1 text-xs font-semibold text-zinc-900">
                  v{resolvedVersion.version_number} · {resolvedVersion.status}
                </div>
                <div className="mt-1 break-all font-mono text-[11px] text-zinc-500">
                  {resolvedVersion.version_id}
                </div>
              </div>
              <div className="rounded-lg border border-zinc-200 bg-white px-3 py-2">
                <div className="text-[10px] font-semibold uppercase tracking-wider text-zinc-400">
                  Trigger
                </div>
                <div className="mt-1 text-xs font-semibold text-zinc-900">
                  {workflowTriggerSummary(resolvedTrigger)}
                </div>
                <div className="mt-1 text-[11px] text-zinc-500">
                  External triggers do not block nested execution; this node
                  invokes the resolved child version directly.
                </div>
              </div>
              <div className="rounded-lg border border-zinc-200 bg-white px-3 py-2">
                <div className="text-[10px] font-semibold uppercase tracking-wider text-zinc-400">
                  Validation
                </div>
                <div className="mt-1 text-xs font-semibold text-zinc-900">
                  {resolvedValidation.valid
                    ? "Ready to call"
                    : `${resolvedValidation.errors} error${
                        resolvedValidation.errors === 1 ? "" : "s"
                      }`}
                </div>
                <div className="mt-1 text-[11px] text-zinc-500">
                  {resolvedValidation.warnings} warning
                  {resolvedValidation.warnings === 1 ? "" : "s"} on the saved
                  child version.
                </div>
              </div>
              <div className="rounded-lg border border-zinc-200 bg-white px-3 py-2">
                <div className="text-[10px] font-semibold uppercase tracking-wider text-zinc-400">
                  Graph
                </div>
                <div className="mt-1 text-xs font-semibold text-zinc-900">
                  {resolvedNodeCount} node{resolvedNodeCount === 1 ? "" : "s"} ·{" "}
                  {resolvedOutputCount} output
                  {resolvedOutputCount === 1 ? "" : "s"}
                </div>
                <div className="mt-1 text-[11px] text-zinc-500">
                  Child workflow {subworkflowWorkflowLabel(selectedWorkflow)}.
                </div>
              </div>
            </div>

            <div className="mt-3 rounded-lg border border-zinc-200 bg-white px-3 py-2">
              <div className="text-[10px] font-semibold uppercase tracking-wider text-zinc-400">
                Runtime contract
              </div>
              <div className="mt-1 text-[11px] leading-relaxed text-zinc-600">
                Parent port <span className="font-mono">input</span> becomes the
                child workflow run input. The child workflow’s terminal string
                output flows back on <span className="font-mono">output</span>,
                and CALIBER also publishes child run metadata, version details,
                and step lineage on <span className="font-mono">result</span>.
              </div>
            </div>

            {!resolvedValidation.valid && (
              <div className="mt-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-[11px] leading-relaxed text-red-700">
                This saved child version still has validation errors. Publish or
                save a fixed child workflow before relying on this node in
                production.
              </div>
            )}

            {trimmedAlias === "manual" &&
              resolvedVersion.status !== "published" && (
                <div className="mt-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] leading-relaxed text-amber-700">
                  Manual resolution currently targets an unpublished child
                  version. Saving or publishing a newer child draft changes this
                  node’s runtime target immediately.
                </div>
              )}
          </>
        )}
      </div>
    </Section>
  );
}

function KnowledgeBuildSection({
  node,
  onChangeNode,
}: {
  node: ManifestNode;
  onChangeNode: (nodeId: string, patch: Partial<ManifestNode>) => void;
}): JSX.Element {
  const optionsQuery = useApiQuery<KnowledgeOptions>(
    ["workflow-editor", "knowledge-options"],
    (signal) => caliberApi.getKnowledgeOptions(signal),
  );
  const knowledgeBasesQuery = useApiQuery<KnowledgeBase[]>(
    ["workflow-editor", "knowledge-bases"],
    (signal) => caliberApi.listKnowledgeBases({ status: "all" }, signal),
  );
  const knowledgeBaseId =
    typeof node.knowledge_base_id === "string" ? node.knowledge_base_id : "";
  const selectedKnowledgeBase =
    (knowledgeBasesQuery.data ?? []).find(
      (item) => item.knowledge_base_id === knowledgeBaseId,
    ) ?? null;
  const activeSummary = selectedKnowledgeBase?.active_version_summary ?? null;
  const embeddingOptions = optionsQuery.data?.embedding_models ?? [];
  const selectedEmbeddingModel =
    typeof node.embedding_model === "string" ? node.embedding_model : "";
  const selectedEmbeddingOption =
    embeddingOptions.find((item) => item.id === selectedEmbeddingModel) ?? null;
  const embeddingBlockedReason =
    selectedEmbeddingOption?.available === false
      ? (selectedEmbeddingOption.unavailable_reason ??
        "This embedding model is unavailable in the current runtime.")
      : (embeddingOptions.find((item) => item.available === false)
          ?.unavailable_reason ?? null);
  const hasAvailableEmbeddingOptions = embeddingOptions.some(
    (item) => item.available !== false,
  );

  return (
    <Section title="Knowledge build">
      <Field label="Knowledge base" fieldKey="knowledge_base_id">
        <select
          data-testid="inspector-knowledge-build-base"
          className={selectClass}
          value={knowledgeBaseId}
          onChange={(e) =>
            onChangeNode(node.id, { knowledge_base_id: e.target.value })
          }
        >
          <option value="">Select a knowledge base</option>
          {(knowledgeBasesQuery.data ?? []).map((item) => (
            <option key={item.knowledge_base_id} value={item.knowledge_base_id}>
              {item.name}
            </option>
          ))}
        </select>
      </Field>
      <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-3 text-xs leading-relaxed text-zinc-600">
        By default this build reuses the selected knowledge base&apos;s saved
        source manifest and latest graph profile. Wire structured inputs into{" "}
        <span className="font-mono">sources</span>,{" "}
        <span className="font-mono">chunking_config</span>, or{" "}
        <span className="font-mono">graph_config</span> only when an upstream
        step should override them for this run.
      </div>
      <Field label="Chunking strategy" fieldKey="chunking_strategy">
        <select
          data-testid="inspector-knowledge-build-chunker"
          className={selectClass}
          value={
            typeof node.chunking_strategy === "string"
              ? node.chunking_strategy
              : ""
          }
          onChange={(e) =>
            onChangeNode(node.id, { chunking_strategy: e.target.value })
          }
        >
          <option value="">Select a chunking strategy</option>
          {(optionsQuery.data?.chunking_strategies ?? []).map((item) => (
            <option key={item.id} value={item.id}>
              {item.name}
            </option>
          ))}
        </select>
      </Field>
      <Field label="Embedding model" fieldKey="embedding_model">
        <div className="space-y-2">
          <select
            data-testid="inspector-knowledge-build-embedding"
            className={selectClass}
            value={selectedEmbeddingModel}
            onChange={(e) =>
              onChangeNode(node.id, { embedding_model: e.target.value })
            }
            disabled={!hasAvailableEmbeddingOptions}
          >
            <option value="">Select an embedding model</option>
            {embeddingOptions.map((item) => (
              <option
                key={item.id}
                value={item.id}
                disabled={item.available === false}
              >
                {item.name}
                {item.available === false ? " (Blocked)" : ""}
              </option>
            ))}
          </select>
          {embeddingBlockedReason && (
            <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] leading-relaxed text-amber-800">
              {embeddingBlockedReason}
            </div>
          )}
        </div>
      </Field>
      <div
        data-testid="knowledge-build-profile"
        className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-3 text-xs text-zinc-600"
      >
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="font-medium text-zinc-800">Current KB profile</div>
          <span className="rounded-full border border-zinc-200 bg-white px-2.5 py-1 text-[11px] font-semibold text-zinc-500">
            {activeSummary
              ? `Active v${activeSummary.version_number}`
              : "No completed version yet"}
          </span>
        </div>
        <div className="mt-3 grid gap-2 sm:grid-cols-2">
          <div className="rounded-md border border-zinc-200 bg-white px-3 py-2">
            <div className="text-[11px] uppercase tracking-[0.12em] text-zinc-400">
              Current chunker
            </div>
            <div className="mt-1 font-medium text-zinc-800">
              {activeSummary?.chunking_strategy ?? "Awaiting build"}
            </div>
          </div>
          <div className="rounded-md border border-zinc-200 bg-white px-3 py-2">
            <div className="text-[11px] uppercase tracking-[0.12em] text-zinc-400">
              Current embedder
            </div>
            <div className="mt-1 font-medium text-zinc-800">
              {activeSummary?.embedding_model ?? "Awaiting build"}
            </div>
          </div>
        </div>
      </div>
      <label className="flex items-start gap-3 rounded-lg border border-zinc-200 bg-white px-3 py-3 text-sm">
        <input
          type="checkbox"
          data-testid="inspector-knowledge-build-wait"
          checked={Boolean(node.wait_for_completion)}
          onChange={(e) =>
            onChangeNode(node.id, { wait_for_completion: e.target.checked })
          }
          className="mt-0.5 h-4 w-4 rounded border-zinc-300 text-zinc-900 focus:ring-zinc-300"
        />
        <span>
          <span className="font-medium text-zinc-800">Wait for completion</span>
          <span className="mt-1 block text-xs leading-relaxed text-zinc-500">
            Hold the workflow until the launched build reaches a terminal
            status, or until the timeout below is reached.
          </span>
        </span>
      </label>
      <Field label="Wait timeout (seconds)" fieldKey="wait_timeout_seconds">
        <input
          data-testid="inspector-knowledge-build-timeout"
          className={inputClass}
          type="number"
          min={1}
          max={86400}
          step={1}
          value={
            typeof node.wait_timeout_seconds === "number"
              ? node.wait_timeout_seconds
              : 300
          }
          onChange={(e) =>
            onChangeNode(node.id, {
              wait_timeout_seconds: Number(e.target.value) || 300,
            })
          }
        />
      </Field>
      <label className="flex items-start gap-3 rounded-lg border border-zinc-200 bg-white px-3 py-3 text-sm">
        <input
          type="checkbox"
          data-testid="inspector-knowledge-build-activate"
          checked={Boolean(node.activate_when_complete)}
          onChange={(e) =>
            onChangeNode(node.id, {
              activate_when_complete: e.target.checked,
            })
          }
          className="mt-0.5 h-4 w-4 rounded border-zinc-300 text-zinc-900 focus:ring-zinc-300"
        />
        <span>
          <span className="font-medium text-zinc-800">
            Activate when complete
          </span>
          <span className="mt-1 block text-xs leading-relaxed text-zinc-500">
            Promote the new version to active automatically once the build
            finishes successfully.
          </span>
        </span>
      </label>
    </Section>
  );
}

function KnowledgeQuerySection({
  node,
  onChangeNode,
}: {
  node: ManifestNode;
  onChangeNode: (nodeId: string, patch: Partial<ManifestNode>) => void;
}): JSX.Element {
  const optionsQuery = useApiQuery<KnowledgeOptions>(
    ["workflow-editor", "knowledge-options"],
    (signal) => caliberApi.getKnowledgeOptions(signal),
  );
  const knowledgeBasesQuery = useApiQuery<KnowledgeBase[]>(
    ["workflow-editor", "knowledge-bases"],
    (signal) => caliberApi.listKnowledgeBases({ status: "all" }, signal),
  );
  const knowledgeBaseId =
    typeof node.knowledge_base_id === "string" ? node.knowledge_base_id : "";
  const versionsQuery = useApiQuery<KnowledgeBaseVersion[]>(
    ["workflow-editor", "knowledge-bases", knowledgeBaseId, "versions"],
    (signal) => caliberApi.listKnowledgeBaseVersions(knowledgeBaseId, signal),
    { enabled: Boolean(knowledgeBaseId) },
  );
  const retrievalModes: KnowledgeRetrievalMode[] = Array.isArray(
    node.retrieval_modes,
  )
    ? node.retrieval_modes
    : [];
  const graphOverrides = (node.graph_overrides ??
    null) as KnowledgeQueryGraphOverrides | null;
  const retrievalModeOptions = (optionsQuery.data?.retrieval_modes ?? []).map(
    (item) => ({
      value: item.id,
      label: item.name,
      hint: item.description,
      testId: `knowledge-modes-option-${item.id}`,
    }),
  );
  const versionOptions = (versionsQuery.data ?? []).map((version) => ({
    value: version.knowledge_base_version_id,
    label: `v${version.version_number}`,
    hint: `${version.chunking_strategy} · ${version.embedding_model}`,
    testId: `knowledge-versions-option-${version.knowledge_base_version_id}`,
  }));
  const selectedKnowledgeBase =
    (knowledgeBasesQuery.data ?? []).find(
      (item) => item.knowledge_base_id === knowledgeBaseId,
    ) ?? null;
  const pinnedVersionIds = Array.isArray(node.version_ids)
    ? node.version_ids
    : [];
  const pinnedVersions = (versionsQuery.data ?? []).filter((version) =>
    pinnedVersionIds.includes(version.knowledge_base_version_id),
  );
  const resolvedActiveVersion =
    (versionsQuery.data ?? []).find(
      (version) =>
        version.knowledge_base_version_id ===
        selectedKnowledgeBase?.active_version_id,
    ) ??
    (versionsQuery.data ?? [])[0] ??
    null;
  const ageEnabled = Boolean(optionsQuery.data?.age_enabled);
  const ageReadyVersions = (versionsQuery.data ?? []).filter((version) =>
    knowledgeVersionAgeReady(version),
  );
  const primaryProfileVersion =
    pinnedVersions[0] ?? resolvedActiveVersion ?? null;
  const selectedKnowledgeBaseSummary =
    selectedKnowledgeBase?.active_version_summary ?? null;
  const resolvedDefaultMode = primaryProfileVersion
    ? knowledgeResolvedDefaultRetrievalMode(primaryProfileVersion, ageEnabled)
    : knowledgeSummaryResolvedDefaultRetrievalMode(
        selectedKnowledgeBaseSummary,
        ageEnabled,
      );
  const usesAgeGraph =
    retrievalModes.includes("age_graph") ||
    (retrievalModes.length === 0 && resolvedDefaultMode === "age_graph");
  const profileGraphTarget =
    primaryProfileVersion?.graph_config.output_target ??
    selectedKnowledgeBaseSummary?.graph_target ??
    null;
  const profileAgeConfigured = primaryProfileVersion
    ? knowledgeVersionAgeConfigured(primaryProfileVersion)
    : selectedKnowledgeBaseSummary?.graph_target === "object_store_and_age";
  const profileAgeReady = primaryProfileVersion
    ? knowledgeVersionAgeReady(primaryProfileVersion)
    : Boolean(selectedKnowledgeBaseSummary?.age_ready);
  const profileAgeSyncStatus = primaryProfileVersion
    ? knowledgeVersionAgeSyncStatus(primaryProfileVersion)
    : (selectedKnowledgeBaseSummary?.age_sync_status ?? null);
  const agePresetVersion =
    pinnedVersions.find((version) => knowledgeVersionAgeReady(version)) ??
    (pinnedVersions.length === 0
      ? knowledgeVersionAgeReady(resolvedActiveVersion)
        ? resolvedActiveVersion
        : (ageReadyVersions[0] ?? null)
      : null);
  const agePresetWillPinVersion = Boolean(
    agePresetVersion &&
    pinnedVersions.length === 0 &&
    resolvedActiveVersion &&
    agePresetVersion.knowledge_base_version_id !==
      resolvedActiveVersion.knowledge_base_version_id,
  );
  const agePresetDisabledReason = !ageEnabled
    ? "Apache AGE is not enabled for this deployment."
    : pinnedVersions.length > 0
      ? "None of the pinned versions have a completed AGE sync yet."
      : "The selected knowledge base does not currently expose an AGE-ready version.";
  const graphQueryPresets = optionsQuery.data?.graph_query_presets?.length
    ? optionsQuery.data.graph_query_presets
    : fallbackGraphQueryPresets(ageEnabled);
  const queryProfileDefaults = primaryProfileVersion?.graph_config ??
    optionsQuery.data?.default_graph_config ?? {
      retrieval_strength: "balanced" as const,
      minimum_relationship_weight: 1,
      age_seed_mode: "entity_then_text" as const,
      age_traversal_hops: 1,
      age_candidate_pool_size: 24,
      age_dense_rerank_weight: 0.35,
    };
  const soleGraphRetrievalMode =
    retrievalModes.length === 1 && retrievalModes[0] !== "dense"
      ? (retrievalModes[0] ?? null)
      : null;
  const activeGraphQueryPreset = soleGraphRetrievalMode
    ? matchGraphQueryPreset(
        buildGraphQueryPresetState({
          retrievalMode: soleGraphRetrievalMode,
          overrides: graphOverrides,
          fallback: {
            retrieval_strength: queryProfileDefaults.retrieval_strength,
            minimum_relationship_weight:
              queryProfileDefaults.minimum_relationship_weight,
            age_seed_mode: queryProfileDefaults.age_seed_mode,
            age_traversal_hops: queryProfileDefaults.age_traversal_hops,
            age_candidate_pool_size:
              queryProfileDefaults.age_candidate_pool_size,
            age_dense_rerank_weight:
              queryProfileDefaults.age_dense_rerank_weight,
            strict_age_retrieval: false,
          },
        }),
        graphQueryPresets,
      )
    : null;
  const activeGraphQueryPresetDefinition =
    graphQueryPresets.find((preset) => preset.id === activeGraphQueryPreset) ??
    null;

  const patchGraphOverrides = (
    patch: Partial<KnowledgeQueryGraphOverrides>,
  ): void => {
    onChangeNode(node.id, {
      graph_overrides: compactGraphOverrides({
        ...(graphOverrides ?? {}),
        ...patch,
      }),
    });
  };

  const handleKnowledgeBaseChange = (nextKnowledgeBaseId: string): void => {
    const nextKnowledgeBase =
      (knowledgeBasesQuery.data ?? []).find(
        (item) => item.knowledge_base_id === nextKnowledgeBaseId,
      ) ?? null;
    const patch: Partial<ManifestNode> = {
      knowledge_base_id: nextKnowledgeBaseId,
      version_ids: [],
    };
    if (
      nextKnowledgeBase?.active_version_summary &&
      retrievalModes.length === 1 &&
      retrievalModes[0] === "dense"
    ) {
      patch.retrieval_modes = [];
    }
    onChangeNode(node.id, patch);
  };

  const applyKbDefaultPreset = (): void => {
    onChangeNode(node.id, { retrieval_modes: [] });
  };

  const applyAgeGraphPreset = (): void => {
    if (!agePresetVersion) return;
    const patch: Partial<ManifestNode> = { retrieval_modes: ["age_graph"] };
    if (agePresetWillPinVersion) {
      patch.version_ids = [agePresetVersion.knowledge_base_version_id];
    }
    onChangeNode(node.id, patch);
  };

  const applyGraphQueryPreset = (preset: KnowledgeGraphQueryPreset): void => {
    const nextState = resolveGraphQueryPresetState(preset, {
      retrieval_strength: queryProfileDefaults.retrieval_strength,
      minimum_relationship_weight:
        queryProfileDefaults.minimum_relationship_weight,
      age_seed_mode: queryProfileDefaults.age_seed_mode,
      age_traversal_hops: queryProfileDefaults.age_traversal_hops,
      age_candidate_pool_size: queryProfileDefaults.age_candidate_pool_size,
      age_dense_rerank_weight: queryProfileDefaults.age_dense_rerank_weight,
      strict_age_retrieval: false,
    });
    const patch: Partial<ManifestNode> = {
      retrieval_modes: [preset.retrieval_mode],
      graph_overrides: compactGraphOverrides(
        graphQueryOverridesFromState(nextState),
      ),
    };
    if (
      preset.retrieval_mode === "age_graph" &&
      agePresetVersion &&
      agePresetWillPinVersion
    ) {
      patch.version_ids = [agePresetVersion.knowledge_base_version_id];
    }
    onChangeNode(node.id, patch);
  };

  return (
    <Section title="Knowledge retrieval">
      <Field label="Knowledge base" fieldKey="knowledge_base_id">
        <select
          data-testid="inspector-knowledge-base"
          className={selectClass}
          value={knowledgeBaseId}
          onChange={(e) => handleKnowledgeBaseChange(e.target.value)}
        >
          <option value="">Select a knowledge base</option>
          {(knowledgeBasesQuery.data ?? []).map((item) => (
            <option key={item.knowledge_base_id} value={item.knowledge_base_id}>
              {item.name}
            </option>
          ))}
        </select>
      </Field>
      <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2.5 text-xs text-zinc-500">
        Leave version pins empty to resolve the active version of the selected
        knowledge base at runtime.
      </div>
      <Field label="Pinned versions" fieldKey="version_ids">
        <ChipMultiSelect
          prefix="knowledge-versions"
          addLabel="Add version"
          emptyText={
            knowledgeBaseId
              ? "No versions available."
              : "Select a knowledge base first."
          }
          searchPlaceholder="Search versions…"
          selected={Array.isArray(node.version_ids) ? node.version_ids : []}
          onChange={(next) => onChangeNode(node.id, { version_ids: next })}
          options={versionOptions}
        />
      </Field>
      <div
        data-testid="knowledge-runtime-profile"
        className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-3 text-xs text-zinc-600"
      >
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="font-medium text-zinc-800">
            Resolved graph profile
          </div>
          <span className="rounded-full border border-zinc-200 bg-white px-2.5 py-1 text-[11px] font-semibold text-zinc-500">
            {primaryProfileVersion
              ? `v${primaryProfileVersion.version_number}`
              : selectedKnowledgeBaseSummary
                ? `Active v${selectedKnowledgeBaseSummary.version_number}`
                : "Awaiting version metadata"}
          </span>
        </div>
        <div className="mt-3 grid gap-2 sm:grid-cols-2">
          <div className="rounded-md border border-zinc-200 bg-white px-3 py-2">
            <div className="text-[11px] uppercase tracking-[0.12em] text-zinc-400">
              Default path
            </div>
            <div className="mt-1 font-medium text-zinc-800">
              {knowledgeRetrievalModeLabel(
                resolvedDefaultMode,
                optionsQuery.data,
              )}
            </div>
          </div>
          <div className="rounded-md border border-zinc-200 bg-white px-3 py-2">
            <div className="text-[11px] uppercase tracking-[0.12em] text-zinc-400">
              Graph target
            </div>
            <div className="mt-1 font-medium text-zinc-800">
              {knowledgeGraphTargetLabel(profileGraphTarget)}
            </div>
          </div>
          <div className="rounded-md border border-zinc-200 bg-white px-3 py-2">
            <div className="text-[11px] uppercase tracking-[0.12em] text-zinc-400">
              AGE status
            </div>
            <div className="mt-1 font-medium text-zinc-800">
              {knowledgeAgeStatusLabel({
                ageEnabled,
                ageReady: profileAgeReady,
                ageConfigured: profileAgeConfigured,
                syncStatus: profileAgeSyncStatus,
              })}
            </div>
          </div>
          <div className="rounded-md border border-zinc-200 bg-white px-3 py-2">
            <div className="text-[11px] uppercase tracking-[0.12em] text-zinc-400">
              Version set
            </div>
            <div className="mt-1 font-medium text-zinc-800">
              {pinnedVersions.length > 0
                ? `${pinnedVersions.length} pinned`
                : "Active version at runtime"}
            </div>
          </div>
        </div>
        <div className="mt-3 leading-relaxed text-zinc-500">
          {agePresetWillPinVersion && agePresetVersion
            ? `The active version is not AGE-ready yet. Choosing AGE graph below will pin v${agePresetVersion.version_number} automatically so this workflow can use the newest synced Apache AGE graph immediately.`
            : retrievalModes.length === 0
              ? "This node is currently following the knowledge base's default retrieval policy at runtime. Upstream version_ids and graph_overrides inputs can still override it."
              : "This profile reflects the version the node will query by default. Upstream version_ids and graph_overrides inputs can still override it at runtime."}
        </div>
      </div>
      <Field label="Retrieval modes" fieldKey="retrieval_modes">
        <ChipMultiSelect
          prefix="knowledge-modes"
          addLabel="Add mode"
          emptyText="No retrieval modes available."
          searchPlaceholder="Search retrieval modes…"
          selected={retrievalModes}
          onChange={(next) =>
            onChangeNode(node.id, {
              retrieval_modes: next as KnowledgeRetrievalMode[],
            })
          }
          options={retrievalModeOptions}
        />
      </Field>
      <div
        className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-3 text-xs text-zinc-600"
        data-testid="knowledge-retrieval-modes-help"
      >
        Leave this empty to follow the knowledge base default, or wire the{" "}
        <span className="font-mono">retrieval_modes</span> input when upstream
        workflow data should switch between dense, GraphRAG hybrid, and Apache
        AGE retrieval at runtime.
      </div>
      <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-3 text-xs text-zinc-600">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="font-medium text-zinc-800">Retrieval presets</div>
          <span className="rounded-full border border-zinc-200 bg-white px-2.5 py-1 text-[11px] font-semibold text-zinc-500">
            {agePresetVersion
              ? `AGE ready on v${agePresetVersion.version_number}`
              : ageEnabled
                ? "AGE not ready"
                : "AGE disabled"}
          </span>
        </div>
        <div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
          <button
            type="button"
            onClick={applyKbDefaultPreset}
            disabled={!selectedKnowledgeBase}
            className="rounded-lg border border-zinc-200 bg-white px-3 py-2 text-left font-medium text-zinc-700 transition hover:border-zinc-300 hover:text-zinc-900 disabled:cursor-not-allowed disabled:border-zinc-200 disabled:bg-zinc-100 disabled:text-zinc-400"
          >
            KB default
          </button>
          <button
            type="button"
            onClick={() =>
              onChangeNode(node.id, { retrieval_modes: ["dense"] })
            }
            className="rounded-lg border border-zinc-200 bg-white px-3 py-2 text-left font-medium text-zinc-700 transition hover:border-zinc-300 hover:text-zinc-900"
          >
            Dense only
          </button>
          <button
            type="button"
            onClick={() =>
              onChangeNode(node.id, { retrieval_modes: ["graph_hybrid"] })
            }
            className="rounded-lg border border-zinc-200 bg-white px-3 py-2 text-left font-medium text-zinc-700 transition hover:border-zinc-300 hover:text-zinc-900"
          >
            GraphRAG hybrid
          </button>
          <button
            type="button"
            onClick={applyAgeGraphPreset}
            disabled={!agePresetVersion}
            className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-left font-medium text-emerald-700 transition hover:border-emerald-300 hover:bg-emerald-100 disabled:cursor-not-allowed disabled:border-zinc-200 disabled:bg-zinc-100 disabled:text-zinc-400"
          >
            AGE graph
          </button>
        </div>
        <div className="mt-2 leading-relaxed text-zinc-500">
          {agePresetVersion
            ? agePresetWillPinVersion
              ? `Use AGE graph to pin v${agePresetVersion.version_number}, the latest synced version, until the active version finishes syncing.`
              : "Use AGE graph to force graph-native traversal from the synced Apache AGE version selected here."
            : agePresetDisabledReason}
        </div>
        {retrievalModes.length === 0 && (
          <div className="mt-2 leading-relaxed text-zinc-500">
            KB default is active right now, so this node resolves its retrieval
            mode from the selected version instead of hardcoding one into the
            workflow.
          </div>
        )}
      </div>
      <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-3 text-xs text-zinc-600">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="font-medium text-zinc-800">Graph query profiles</div>
          <span className="rounded-full border border-zinc-200 bg-white px-2.5 py-1 text-[11px] font-semibold text-zinc-500">
            {activeGraphQueryPresetDefinition?.label ??
              (retrievalModes.length === 0
                ? "KB default"
                : retrievalModes.length === 1 && retrievalModes[0] === "dense"
                  ? "Dense only"
                  : "Custom graph tuning")}
          </span>
        </div>
        <div className="mt-2 leading-relaxed text-zinc-500">
          Apply a shared GraphRAG or AGE-backed retrieval posture to this node,
          then fine-tune the overrides below only when you need a custom mix.
        </div>
        <div className="mt-3 grid gap-2 sm:grid-cols-2">
          {graphQueryPresets.map((preset) => {
            const active = activeGraphQueryPreset === preset.id;
            const disabled = Boolean(
              preset.age_required && (!ageEnabled || !agePresetVersion),
            );
            return (
              <button
                key={`knowledge-query-preset-${preset.id}`}
                type="button"
                data-testid={`knowledge-query-preset-${preset.id}`}
                onClick={() => applyGraphQueryPreset(preset)}
                disabled={disabled}
                className={`rounded-lg border px-3 py-2 text-left transition ${
                  active
                    ? "border-zinc-900 bg-zinc-900 text-white"
                    : "border-zinc-200 bg-white text-zinc-700 hover:border-zinc-300 hover:text-zinc-900"
                } disabled:cursor-not-allowed disabled:border-zinc-200 disabled:bg-zinc-100 disabled:text-zinc-400`}
              >
                <div
                  className={`text-[11px] uppercase tracking-[0.12em] ${active ? "text-zinc-300" : "text-zinc-400"}`}
                >
                  {preset.eyebrow}
                </div>
                <div className="mt-1 text-sm font-semibold">{preset.label}</div>
                <div
                  className={`mt-1 text-[11px] leading-relaxed ${active ? "text-zinc-200" : "text-zinc-500"}`}
                >
                  {preset.description}
                </div>
              </button>
            );
          })}
        </div>
        {activeGraphQueryPreset === "custom" && (
          <div className="mt-3 leading-relaxed text-zinc-500">
            Manual graph tuning is active right now, so the node no longer
            matches one of the canned GraphRAG / AGE profiles exactly.
          </div>
        )}
      </div>
      <Field label="Top K chunks" fieldKey="top_k">
        <input
          data-testid="inspector-knowledge-top-k"
          className={inputClass}
          type="number"
          min={1}
          max={20}
          value={typeof node.top_k === "number" ? node.top_k : 6}
          onChange={(e) =>
            onChangeNode(node.id, { top_k: Number(e.target.value) || 1 })
          }
        />
      </Field>
      <Field label="Chat model" fieldKey="chat_model">
        <input
          data-testid="inspector-knowledge-chat-model"
          className={inputClass}
          value={typeof node.chat_model === "string" ? node.chat_model : ""}
          placeholder="Optional override"
          onChange={(e) =>
            onChangeNode(node.id, { chat_model: e.target.value || null })
          }
        />
      </Field>
      <Field label="Graph retrieval strength" fieldKey="graph_overrides">
        <select
          data-testid="inspector-knowledge-strength"
          className={selectClass}
          value={graphOverrides?.retrieval_strength ?? ""}
          onChange={(e) =>
            patchGraphOverrides({
              retrieval_strength: (e.target.value ||
                undefined) as KnowledgeQueryGraphOverrides["retrieval_strength"],
            })
          }
        >
          <option value="">Use KB default</option>
          {(optionsQuery.data?.graph_retrieval_strengths ?? []).map((item) => (
            <option key={item.id} value={item.id}>
              {item.name}
            </option>
          ))}
        </select>
      </Field>
      <Field label="Min relationship weight">
        <input
          data-testid="inspector-knowledge-min-weight"
          className={inputClass}
          type="number"
          min={0}
          step={0.5}
          value={
            typeof graphOverrides?.minimum_relationship_weight === "number"
              ? graphOverrides.minimum_relationship_weight
              : ""
          }
          placeholder="Use KB default"
          onChange={(e) =>
            patchGraphOverrides({
              minimum_relationship_weight:
                e.target.value === "" ? undefined : Number(e.target.value) || 0,
            })
          }
        />
      </Field>
      <Field label="AGE seed mode">
        <select
          data-testid="inspector-knowledge-age-seed-mode"
          className={selectClass}
          value={graphOverrides?.age_seed_mode ?? ""}
          onChange={(e) =>
            patchGraphOverrides({
              age_seed_mode: (e.target.value ||
                undefined) as KnowledgeQueryGraphOverrides["age_seed_mode"],
            })
          }
          disabled={!usesAgeGraph}
        >
          <option value="">Use KB default</option>
          {(optionsQuery.data?.graph_age_seed_modes ?? []).map((item) => (
            <option key={item.id} value={item.id}>
              {item.name}
            </option>
          ))}
        </select>
      </Field>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="AGE traversal hops">
          <select
            data-testid="inspector-knowledge-age-hops"
            className={selectClass}
            value={
              typeof graphOverrides?.age_traversal_hops === "number"
                ? String(graphOverrides.age_traversal_hops)
                : ""
            }
            onChange={(e) =>
              patchGraphOverrides({
                age_traversal_hops:
                  e.target.value === ""
                    ? undefined
                    : Number(e.target.value) || 0,
              })
            }
            disabled={!usesAgeGraph}
          >
            <option value="">Use KB default</option>
            <option value="0">0 · Direct entities only</option>
            <option value="1">1 · One-hop expansion</option>
            <option value="2">2 · Two-hop expansion</option>
          </select>
        </Field>
        <Field label="AGE candidate pool">
          <input
            data-testid="inspector-knowledge-age-pool"
            className={inputClass}
            type="number"
            min={4}
            max={200}
            value={
              typeof graphOverrides?.age_candidate_pool_size === "number"
                ? graphOverrides.age_candidate_pool_size
                : ""
            }
            placeholder="Use KB default"
            onChange={(e) =>
              patchGraphOverrides({
                age_candidate_pool_size:
                  e.target.value === ""
                    ? undefined
                    : Number(e.target.value) || 4,
              })
            }
            disabled={!usesAgeGraph}
          />
        </Field>
      </div>
      <Field label="AGE dense rerank weight">
        <input
          data-testid="inspector-knowledge-age-dense-weight"
          className={inputClass}
          type="number"
          min={0}
          max={3}
          step={0.05}
          value={
            typeof graphOverrides?.age_dense_rerank_weight === "number"
              ? graphOverrides.age_dense_rerank_weight
              : ""
          }
          placeholder="Use KB default"
          onChange={(e) =>
            patchGraphOverrides({
              age_dense_rerank_weight:
                e.target.value === "" ? undefined : Number(e.target.value) || 0,
            })
          }
          disabled={!usesAgeGraph}
        />
      </Field>
      <label className="flex items-center gap-2 rounded-lg border border-zinc-200 bg-white px-3 py-2 text-xs text-zinc-700">
        <input
          type="checkbox"
          aria-label="Strict AGE retrieval"
          checked={Boolean(graphOverrides?.strict_age_retrieval)}
          onChange={(e) =>
            patchGraphOverrides({ strict_age_retrieval: e.target.checked })
          }
          disabled={!usesAgeGraph}
          className="rounded border-zinc-300"
        />
        <span className="font-medium">Strict AGE retrieval</span>
      </label>
      {(knowledgeBasesQuery.error ||
        optionsQuery.error ||
        versionsQuery.error) && (
        <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2.5 text-xs text-red-700">
          {knowledgeBasesQuery.error?.message ??
            optionsQuery.error?.message ??
            versionsQuery.error?.message}
        </div>
      )}
    </Section>
  );
}

function ExternalAppSection({
  node,
  onChangeNode,
}: {
  node: ManifestNode;
  onChangeNode: (nodeId: string, patch: Partial<ManifestNode>) => void;
}): JSX.Element {
  return (
    <Section title="External app">
      <Field label="Entrypoint" fieldKey="entrypoint">
        <input
          data-testid="inspector-external-entrypoint"
          className={inputClass}
          value={typeof node.entrypoint === "string" ? node.entrypoint : ""}
          placeholder="package.module:callable"
          onChange={(e) =>
            onChangeNode(node.id, { entrypoint: e.target.value })
          }
        />
      </Field>
      <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2.5 text-xs text-zinc-500">
        Call an existing Python function by module path, for example{" "}
        <span className="font-mono">support.ticketing:handle_request</span>.
      </div>
    </Section>
  );
}

const WEBHOOK_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"] as const;

function WebhookSection({
  node,
  onChangeNode,
}: {
  node: ManifestNode;
  onChangeNode: (nodeId: string, patch: Partial<ManifestNode>) => void;
}): JSX.Element {
  const headers =
    node.headers && typeof node.headers === "object"
      ? (node.headers as Record<string, string>)
      : {};
  const method = typeof node.method === "string" ? node.method : "POST";

  return (
    <Section title="Webhook request">
      <Field label="URL" fieldKey="url">
        <input
          data-testid="inspector-webhook-url"
          className={inputClass}
          value={typeof node.url === "string" ? node.url : ""}
          placeholder="https://example.com/webhook"
          onChange={(e) => onChangeNode(node.id, { url: e.target.value })}
        />
      </Field>
      <Field label="Method" fieldKey="method">
        <select
          data-testid="inspector-webhook-method"
          className={selectClass}
          value={method}
          onChange={(e) =>
            onChangeNode(node.id, {
              method: e.target.value as ManifestNode["method"],
            })
          }
        >
          {WEBHOOK_METHODS.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </select>
      </Field>
      <Field label="Timeout (seconds)" fieldKey="timeout_seconds">
        <input
          data-testid="inspector-webhook-timeout"
          className={inputClass}
          type="number"
          min={1}
          max={600}
          step={1}
          value={
            typeof node.timeout_seconds === "number" ? node.timeout_seconds : 30
          }
          onChange={(e) =>
            onChangeNode(node.id, {
              timeout_seconds: Number(e.target.value) || 30,
            })
          }
        />
      </Field>
      <HeadersEditor
        headers={headers}
        onChange={(next) => onChangeNode(node.id, { headers: next })}
      />
      <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2.5 text-xs text-zinc-500">
        The upstream <span className="font-mono">payload</span> (or{" "}
        <span className="font-mono">input</span>) becomes the request body —
        structured bodies are sent as JSON. Reference auth secrets by name in
        headers rather than pasting them inline.
      </div>
    </Section>
  );
}

const API_REQUEST_METHODS = ["GET", "POST", "PATCH", "PUT", "DELETE"] as const;

/** Reusable key/value headers editor (object is the source of truth). */
function HeadersEditor({
  headers,
  onChange,
}: {
  headers: Record<string, string>;
  onChange: (next: Record<string, string>) => void;
}): JSX.Element {
  const rows = Object.entries(headers);
  const writeRows = (next: [string, string][]): void =>
    onChange(Object.fromEntries(next));
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-[11px] font-semibold uppercase tracking-wider text-zinc-400">
          Headers
        </span>
        <button
          type="button"
          data-testid="inspector-api-add-header"
          onClick={() => writeRows([...rows, ["", ""]])}
          className="inline-flex items-center gap-1 rounded-md border border-zinc-200 bg-white px-2 py-1 text-[11px] font-medium text-zinc-600 transition-colors hover:border-zinc-300 hover:text-zinc-900"
        >
          <Plus size={12} strokeWidth={2.25} aria-hidden /> Add header
        </button>
      </div>
      {rows.length === 0 && (
        <div className="rounded-lg border border-dashed border-zinc-200 px-3 py-2 text-xs text-zinc-400">
          No headers configured.
        </div>
      )}
      {rows.map(([key, value], index) => (
        <div key={index} className="flex items-center gap-1.5">
          <input
            aria-label={`Header ${index + 1} name`}
            className={inputClass}
            value={key}
            placeholder="Header"
            onChange={(e) =>
              writeRows(
                rows.map((r, i) => (i === index ? [e.target.value, r[1]] : r)),
              )
            }
          />
          <input
            aria-label={`Header ${index + 1} value`}
            className={inputClass}
            value={value}
            placeholder="Value"
            onChange={(e) =>
              writeRows(
                rows.map((r, i) => (i === index ? [r[0], e.target.value] : r)),
              )
            }
          />
          <button
            type="button"
            aria-label={`Remove header ${index + 1}`}
            onClick={() => writeRows(rows.filter((_, i) => i !== index))}
            className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-zinc-400 transition-colors hover:bg-red-50 hover:text-red-500"
          >
            <X size={14} strokeWidth={2} aria-hidden />
          </button>
        </div>
      ))}
    </div>
  );
}

function ApiRequestSection({
  node,
  onChangeNode,
}: {
  node: ManifestNode;
  onChangeNode: (nodeId: string, patch: Partial<ManifestNode>) => void;
}): JSX.Element {
  const mode = node.mode === "curl" ? "curl" : "url";
  const method =
    typeof node.method === "string" && node.method ? node.method : "GET";
  const headers =
    node.headers && typeof node.headers === "object"
      ? (node.headers as Record<string, string>)
      : {};

  return (
    <Section title="API request">
      <Field label="Mode" fieldKey="mode">
        <div className="inline-flex w-full rounded-lg border border-zinc-200 bg-zinc-100 p-0.5">
          {(["url", "curl"] as const).map((m) => (
            <button
              key={m}
              type="button"
              data-testid={`inspector-api-mode-${m}`}
              aria-pressed={mode === m}
              onClick={() => onChangeNode(node.id, { mode: m })}
              className={`flex-1 rounded-md px-3 py-1 text-xs font-medium transition-colors ${
                mode === m
                  ? "bg-white text-zinc-900 shadow-sm"
                  : "text-zinc-500 hover:text-zinc-700"
              }`}
            >
              {m === "url" ? "URL" : "cURL"}
            </button>
          ))}
        </div>
      </Field>

      {mode === "url" ? (
        <>
          <Field label="URL" fieldKey="url">
            <input
              data-testid="inspector-api-url"
              className={inputClass}
              value={typeof node.url === "string" ? node.url : ""}
              placeholder="https://api.example.com/v1/resource"
              onChange={(e) => onChangeNode(node.id, { url: e.target.value })}
            />
          </Field>
          <Field label="Method" fieldKey="method">
            <select
              data-testid="inspector-api-method"
              aria-label="Method"
              className={selectClass}
              value={method}
              onChange={(e) =>
                onChangeNode(node.id, {
                  method: e.target.value as ManifestNode["method"],
                })
              }
            >
              {API_REQUEST_METHODS.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Body" fieldKey="body">
            <textarea
              data-testid="inspector-api-body"
              className={textareaClass}
              rows={3}
              value={typeof node.body === "string" ? node.body : ""}
              placeholder='{"key": "value"}  — leave empty to send the upstream payload'
              onChange={(e) => onChangeNode(node.id, { body: e.target.value })}
            />
          </Field>
          <HeadersEditor
            headers={headers}
            onChange={(next) => onChangeNode(node.id, { headers: next })}
          />
        </>
      ) : (
        <Field label="cURL command" fieldKey="curl">
          <textarea
            data-testid="inspector-api-curl"
            className={textareaClass}
            rows={5}
            value={typeof node.curl === "string" ? node.curl : ""}
            placeholder={
              "curl -X POST 'https://api.example.com' \\\n  -H 'Content-Type: application/json' \\\n  -d '{\"key\":\"value\"}'"
            }
            onChange={(e) => onChangeNode(node.id, { curl: e.target.value })}
          />
        </Field>
      )}

      <Field label="Timeout (seconds)" fieldKey="timeout_seconds">
        <input
          data-testid="inspector-api-timeout"
          className={inputClass}
          type="number"
          min={1}
          max={600}
          step={1}
          value={
            typeof node.timeout_seconds === "number" ? node.timeout_seconds : 30
          }
          onChange={(e) =>
            onChangeNode(node.id, {
              timeout_seconds: Number(e.target.value) || 30,
            })
          }
        />
      </Field>

      <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2.5 text-xs text-zinc-500">
        {mode === "curl"
          ? "The cURL command is parsed for the method, URL, headers, and body — no shell is executed."
          : "Leave the body empty to send the upstream payload (or input) as the request body. Structured bodies are sent as JSON."}
      </div>
    </Section>
  );
}

function TemplateSection({
  node,
  onChangeNode,
}: {
  node: ManifestNode;
  onChangeNode: (nodeId: string, patch: Partial<ManifestNode>) => void;
}): JSX.Element {
  return (
    <Section title="Template">
      <Field label="Template body" fieldKey="template">
        <textarea
          data-testid="inspector-template-body"
          className={textareaClass}
          rows={10}
          value={typeof node.template === "string" ? node.template : ""}
          onChange={(e) => onChangeNode(node.id, { template: e.target.value })}
        />
      </Field>
      <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2.5 text-xs text-zinc-500">
        Use placeholders like <span className="font-mono">{"{{input}}"}</span>,{" "}
        <span className="font-mono">{"{{variables.customer.name}}"}</span>, or{" "}
        <span className="font-mono">{"{{items[0]}}"}</span>.
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Output format" fieldKey="output_format">
          <select
            data-testid="inspector-template-output-format"
            className={selectClass}
            value={node.output_format ?? "text"}
            onChange={(e) =>
              onChangeNode(node.id, {
                output_format: e.target.value as ManifestNode["output_format"],
              })
            }
          >
            <option value="text">Text</option>
            <option value="json">JSON</option>
          </select>
        </Field>
        <Field label="Missing variables" fieldKey="missing_variable_mode">
          <select
            data-testid="inspector-template-missing-mode"
            className={selectClass}
            value={node.missing_variable_mode ?? "preserve"}
            onChange={(e) =>
              onChangeNode(node.id, {
                missing_variable_mode: e.target
                  .value as ManifestNode["missing_variable_mode"],
              })
            }
          >
            <option value="preserve">Preserve placeholders</option>
            <option value="empty">Replace with empty text</option>
            <option value="error">Fail the node</option>
          </select>
        </Field>
      </div>
    </Section>
  );
}

function WorkflowPromptArtifactsSection({
  manifest,
  prompts,
  onChangeWorkflow,
}: {
  manifest: WorkflowManifest;
  prompts: PromptInfo[];
  onChangeWorkflow: (patch: Partial<WorkflowManifest>) => void;
}): JSX.Element {
  const promptEntries = workflowPromptArtifactEntries(manifest);
  const promptReferences = workflowPromptArtifactReferences(manifest);
  const promptOptions = prompts.filter(
    (prompt) => prompt.has_prompt && prompt.prompt_name,
  );

  const patchPromptArtifacts = (
    nextPrompts: Record<string, WorkflowPromptArtifact>,
  ): void => {
    onChangeWorkflow({
      artifacts: {
        ...workflowArtifacts(manifest),
        prompts: nextPrompts,
      },
    });
  };

  const updatePromptArtifact = (
    promptRef: string,
    patch: Partial<WorkflowPromptArtifact>,
  ): void => {
    const current = workflowPromptArtifacts(manifest)[promptRef];
    if (!current) return;
    patchPromptArtifacts({
      ...workflowPromptArtifacts(manifest),
      [promptRef]: {
        ...current,
        ...patch,
      },
    });
  };

  const removePromptArtifact = (promptRef: string): void => {
    const nextPrompts = { ...workflowPromptArtifacts(manifest) };
    delete nextPrompts[promptRef];
    patchPromptArtifacts(nextPrompts);
  };

  const addPromptArtifact = (): void => {
    const existing = workflowPromptArtifacts(manifest);
    const firstUnusedPrompt = promptOptions.find((prompt) => {
      const promptRef = prompt.prompt_name?.trim() ?? "";
      return (
        promptRef && !Object.prototype.hasOwnProperty.call(existing, promptRef)
      );
    });
    if (firstUnusedPrompt?.prompt_name) {
      patchPromptArtifacts({
        ...existing,
        [firstUnusedPrompt.prompt_name]: {
          registry_name: firstUnusedPrompt.prompt_name,
          alias: firstUnusedPrompt.alias ?? "prod",
          managed_by: "mlflow_prompt_registry",
        },
      });
      return;
    }
    let index = 1;
    let promptRef = `prompt_artifact_${index}`;
    while (Object.prototype.hasOwnProperty.call(existing, promptRef)) {
      index += 1;
      promptRef = `prompt_artifact_${index}`;
    }
    patchPromptArtifacts({
      ...existing,
      [promptRef]: {
        registry_name: promptRef,
        alias: "prod",
        managed_by: "manual",
      },
    });
  };

  return (
    <Section title="Prompt artifacts">
      <div className="rounded-xl border border-zinc-200 bg-zinc-50 px-3 py-3 text-xs leading-relaxed text-zinc-600">
        Registered prompt references compile through workflow-level prompt
        artifacts. Keep the prompt registry name, alias, and ownership metadata
        aligned here so agent instructions stay governed without editing raw
        manifest JSON.
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-dashed border-zinc-200 bg-white px-3 py-3 text-xs text-zinc-500">
        <div>
          {promptOptions.length > 0
            ? "Add an available registry prompt or keep manual artifacts for explicit governance."
            : "No prompt registry records are loaded right now, but you can still add a manual prompt artifact."}
        </div>
        <button
          type="button"
          data-testid="workflow-prompt-artifacts-add"
          className="rounded-lg border border-zinc-200 px-2.5 py-1 text-xs font-medium text-zinc-700 transition-colors hover:bg-zinc-50"
          onClick={addPromptArtifact}
        >
          Add prompt artifact
        </button>
      </div>

      {promptEntries.length === 0 ? (
        <div className="rounded-xl border border-dashed border-zinc-200 bg-white px-3 py-4 text-xs text-zinc-500">
          Switch an agent to a registered prompt, or add a prompt artifact here,
          and CALIBER will keep the workflow-level prompt registry contract in
          sync.
        </div>
      ) : (
        <div className="space-y-4">
          {promptEntries.map(([promptRef, artifact]) => {
            const references = promptReferences[promptRef] ?? [];
            const canRemove = references.length === 0;
            const registryPrompt = promptOptions.find(
              (prompt) => prompt.prompt_name === artifact.registry_name,
            );
            return (
              <div
                key={promptRef}
                data-testid={workflowPromptArtifactTestId(
                  "workflow-prompt-artifact-card",
                  promptRef,
                )}
                className="rounded-2xl border border-zinc-200 bg-white p-4 shadow-sm"
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="font-mono text-sm text-zinc-900">
                      {promptRef}
                    </div>
                    <div className="mt-2 flex flex-wrap gap-2 text-[11px]">
                      <span className="rounded-full border border-zinc-200 bg-zinc-50 px-2.5 py-1 font-semibold text-zinc-600">
                        Alias @{artifact.alias ?? "prod"}
                      </span>
                      <span
                        className={`rounded-full border px-2.5 py-1 font-semibold ${
                          references.length > 0
                            ? "border-amber-200 bg-amber-50 text-amber-700"
                            : "border-emerald-200 bg-emerald-50 text-emerald-700"
                        }`}
                      >
                        {references.length > 0
                          ? `Used by ${references.join(", ")}`
                          : "Unused artifact"}
                      </span>
                    </div>
                  </div>
                  <button
                    type="button"
                    data-testid={workflowPromptArtifactTestId(
                      "workflow-prompt-artifact-remove",
                      promptRef,
                    )}
                    className="rounded-lg border border-red-200 px-2.5 py-1 text-xs font-medium text-red-600 transition-colors hover:bg-red-50 disabled:cursor-not-allowed disabled:border-zinc-200 disabled:text-zinc-400"
                    disabled={!canRemove}
                    title={
                      canRemove
                        ? "Remove unused prompt artifact"
                        : "Clear agent prompt references before removing this artifact."
                    }
                    onClick={() => removePromptArtifact(promptRef)}
                  >
                    Remove
                  </button>
                </div>

                <div className="mt-3 text-[11px] leading-relaxed text-zinc-500">
                  {registryPrompt
                    ? `Current registry prompt: ${registryPrompt.prompt_name} @${registryPrompt.alias}.`
                    : "This artifact keeps a workflow-level pointer to a prompt registry entry and alias."}
                </div>

                <div className="mt-4 grid gap-3 md:grid-cols-2">
                  <Field label="Registry name">
                    <input
                      data-testid={workflowPromptArtifactTestId(
                        "workflow-prompt-artifact-registry-name",
                        promptRef,
                      )}
                      className={inputClass}
                      value={artifact.registry_name}
                      onChange={(event) =>
                        updatePromptArtifact(promptRef, {
                          registry_name: event.target.value,
                        })
                      }
                      placeholder="support-agent"
                    />
                  </Field>
                  <Field label="Alias">
                    <input
                      data-testid={workflowPromptArtifactTestId(
                        "workflow-prompt-artifact-alias",
                        promptRef,
                      )}
                      className={inputClass}
                      value={artifact.alias ?? "prod"}
                      onChange={(event) =>
                        updatePromptArtifact(promptRef, {
                          alias: event.target.value,
                        })
                      }
                      placeholder="prod"
                    />
                  </Field>
                  <div className="md:col-span-2">
                    <Field label="Managed by">
                      <input
                        data-testid={workflowPromptArtifactTestId(
                          "workflow-prompt-artifact-managed-by",
                          promptRef,
                        )}
                        className={inputClass}
                        value={artifact.managed_by ?? "mlflow_prompt_registry"}
                        onChange={(event) =>
                          updatePromptArtifact(promptRef, {
                            managed_by: event.target.value,
                          })
                        }
                        placeholder="mlflow_prompt_registry"
                      />
                    </Field>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Section>
  );
}

function WorkflowToolBindingsSection({
  manifest,
  onChangeWorkflow,
}: {
  manifest: WorkflowManifest;
  onChangeWorkflow: (patch: Partial<WorkflowManifest>) => void;
}): JSX.Element {
  const bindingEntries = workflowToolBindingEntries(manifest);
  const bindingReferences = workflowToolBindingReferences(manifest);

  const updateBinding = (
    localName: string,
    nextBinding: WorkflowToolBinding,
  ): void => {
    const nextTools = {
      ...((manifest.tools ?? {}) as Record<string, unknown>),
      [localName]: nextBinding,
    };
    onChangeWorkflow({
      tools: nextTools as WorkflowManifest["tools"],
    });
  };

  const patchRegisteredBinding = (
    localName: string,
    patch: Partial<WorkflowRegisteredFunctionToolBinding>,
  ): void => {
    const current = bindingEntries.find(
      (entry) => entry.localName === localName,
    )?.binding;
    if (!current || current.type === "mcp_tool") return;
    updateBinding(localName, { ...current, ...patch });
  };

  const patchMcpBinding = (
    localName: string,
    patch: Partial<WorkflowMcpToolBinding>,
  ): void => {
    const current = bindingEntries.find(
      (entry) => entry.localName === localName,
    )?.binding;
    if (!current || current.type !== "mcp_tool") return;
    updateBinding(localName, { ...current, ...patch, type: "mcp_tool" });
  };

  const removeBinding = (localName: string): void => {
    const nextTools = {
      ...((manifest.tools ?? {}) as Record<string, unknown>),
    };
    delete nextTools[localName];
    onChangeWorkflow({
      tools: nextTools as WorkflowManifest["tools"],
    });
  };

  return (
    <Section title="Tool bindings">
      <div className="rounded-xl border border-zinc-200 bg-zinc-50 px-3 py-3 text-xs leading-relaxed text-zinc-600">
        Tool bindings are the workflow-level runtime contracts that agent and
        direct tool nodes resolve by local name. Configure registry refs, MCP
        server mappings, approvals, secrets, timeouts, and retries here without
        hand-editing the manifest.
      </div>

      {bindingEntries.length === 0 ? (
        <div className="rounded-xl border border-dashed border-zinc-200 bg-white px-3 py-4 text-xs text-zinc-500">
          Select one or more tools on agent nodes, or choose a direct tool
          binding on a tool node, and CALIBER will create the workflow-level
          binding here automatically.
        </div>
      ) : (
        <div className="space-y-4">
          {bindingEntries.map(({ localName, binding, raw }) => {
            const references = bindingReferences[localName] ?? [];
            const canRemove = references.length === 0;
            return (
              <div
                key={localName}
                data-testid={workflowToolBindingTestId(
                  "workflow-tool-binding-card",
                  localName,
                )}
                className="rounded-2xl border border-zinc-200 bg-white p-4 shadow-sm"
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="font-mono text-sm text-zinc-900">
                      {localName}
                    </div>
                    <div className="mt-2 flex flex-wrap gap-2 text-[11px]">
                      <span className="rounded-full border border-zinc-200 bg-zinc-50 px-2.5 py-1 font-semibold text-zinc-600">
                        {workflowToolBindingTypeLabel(binding)}
                      </span>
                      <span
                        className={`rounded-full border px-2.5 py-1 font-semibold ${
                          references.length > 0
                            ? "border-amber-200 bg-amber-50 text-amber-700"
                            : "border-emerald-200 bg-emerald-50 text-emerald-700"
                        }`}
                      >
                        {references.length > 0
                          ? `Used by ${references.join(", ")}`
                          : "Unused binding"}
                      </span>
                    </div>
                  </div>
                  <button
                    type="button"
                    data-testid={workflowToolBindingTestId(
                      "workflow-tool-binding-remove",
                      localName,
                    )}
                    className="rounded-lg border border-red-200 px-2.5 py-1 text-xs font-medium text-red-600 transition-colors hover:bg-red-50 disabled:cursor-not-allowed disabled:border-zinc-200 disabled:text-zinc-400"
                    disabled={!canRemove}
                    title={
                      canRemove
                        ? "Remove unused binding"
                        : "Clear node references before removing this binding."
                    }
                    onClick={() => removeBinding(localName)}
                  >
                    Remove
                  </button>
                </div>

                <div className="mt-3 text-[11px] leading-relaxed text-zinc-500">
                  {binding
                    ? binding.type === "mcp_tool"
                      ? "This local name resolves to a remote MCP server tool at runtime."
                      : "This local name resolves through the registered tool registry at runtime."
                    : "This binding payload no longer matches the current manifest schema. Fix it here or remove it once no nodes reference it."}
                </div>

                {binding ? (
                  binding.type === "mcp_tool" ? (
                    <div className="mt-4 grid gap-3 md:grid-cols-2">
                      <Field label="Server ID">
                        <input
                          data-testid={workflowToolBindingTestId(
                            "workflow-tool-binding-server-id",
                            localName,
                          )}
                          className={inputClass}
                          value={binding.server_id}
                          onChange={(event) =>
                            patchMcpBinding(localName, {
                              server_id: event.target.value,
                            })
                          }
                        />
                      </Field>
                      <Field label="Remote tool">
                        <input
                          data-testid={workflowToolBindingTestId(
                            "workflow-tool-binding-tool-name",
                            localName,
                          )}
                          className={inputClass}
                          value={binding.tool_name}
                          onChange={(event) =>
                            patchMcpBinding(localName, {
                              tool_name: event.target.value,
                            })
                          }
                        />
                      </Field>
                      <Field label="Schema version">
                        <input
                          data-testid={workflowToolBindingTestId(
                            "workflow-tool-binding-schema-version",
                            localName,
                          )}
                          className={inputClass}
                          value={binding.tool_schema_version ?? ""}
                          onChange={(event) =>
                            patchMcpBinding(localName, {
                              tool_schema_version: event.target.value,
                            })
                          }
                        />
                      </Field>
                      <Field label="Side effect level">
                        <select
                          data-testid={workflowToolBindingTestId(
                            "workflow-tool-binding-side-effect",
                            localName,
                          )}
                          className={selectClass}
                          value={binding.side_effect_level ?? "read"}
                          onChange={(event) =>
                            patchMcpBinding(localName, {
                              side_effect_level: event.target
                                .value as WorkflowMcpToolBinding["side_effect_level"],
                            })
                          }
                        >
                          <option value="read">Read</option>
                          <option value="write">Write</option>
                          <option value="external_action">
                            External action
                          </option>
                        </select>
                      </Field>
                      <Field label="Timeout (seconds)">
                        <input
                          data-testid={workflowToolBindingTestId(
                            "workflow-tool-binding-timeout",
                            localName,
                          )}
                          className={inputClass}
                          type="number"
                          min={1}
                          step={1}
                          value={binding.timeout_seconds ?? ""}
                          onChange={(event) =>
                            patchMcpBinding(localName, {
                              timeout_seconds: parseNullablePositiveNumber(
                                event.target.value,
                              ),
                            })
                          }
                        />
                      </Field>
                      <Field label="Max retries">
                        <input
                          data-testid={workflowToolBindingTestId(
                            "workflow-tool-binding-retries",
                            localName,
                          )}
                          className={inputClass}
                          type="number"
                          min={0}
                          max={5}
                          step={1}
                          value={binding.max_retries ?? 0}
                          onChange={(event) =>
                            patchMcpBinding(localName, {
                              max_retries: parseNonNegativeInteger(
                                event.target.value,
                              ),
                            })
                          }
                        />
                      </Field>
                      <Field label="Approval">
                        <label className="flex items-start gap-3 rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2 text-sm text-zinc-700">
                          <input
                            type="checkbox"
                            data-testid={workflowToolBindingTestId(
                              "workflow-tool-binding-approval",
                              localName,
                            )}
                            className="mt-0.5 rounded border-zinc-300"
                            checked={binding.requires_approval === true}
                            onChange={(event) =>
                              patchMcpBinding(localName, {
                                requires_approval: event.target.checked,
                              })
                            }
                          />
                          <span className="text-xs leading-relaxed">
                            Require a workflow approval gate before this MCP
                            tool is executed.
                          </span>
                        </label>
                      </Field>
                    </div>
                  ) : (
                    <div className="mt-4 grid gap-3 md:grid-cols-2">
                      <Field label="Registry ref">
                        <input
                          data-testid={workflowToolBindingTestId(
                            "workflow-tool-binding-registry-ref",
                            localName,
                          )}
                          className={inputClass}
                          value={binding.registry_ref}
                          onChange={(event) =>
                            patchRegisteredBinding(localName, {
                              registry_ref: event.target.value,
                            })
                          }
                        />
                      </Field>
                      <Field label="Version constraint">
                        <input
                          data-testid={workflowToolBindingTestId(
                            "workflow-tool-binding-version-constraint",
                            localName,
                          )}
                          className={inputClass}
                          value={binding.version_constraint ?? ""}
                          onChange={(event) =>
                            patchRegisteredBinding(localName, {
                              version_constraint: event.target.value,
                            })
                          }
                        />
                      </Field>
                      <Field label="Timeout (seconds)">
                        <input
                          data-testid={workflowToolBindingTestId(
                            "workflow-tool-binding-timeout",
                            localName,
                          )}
                          className={inputClass}
                          type="number"
                          min={1}
                          step={1}
                          value={binding.timeout_seconds ?? ""}
                          onChange={(event) =>
                            patchRegisteredBinding(localName, {
                              timeout_seconds: parseNullablePositiveNumber(
                                event.target.value,
                              ),
                            })
                          }
                        />
                      </Field>
                      <Field label="Max retries">
                        <input
                          data-testid={workflowToolBindingTestId(
                            "workflow-tool-binding-retries",
                            localName,
                          )}
                          className={inputClass}
                          type="number"
                          min={0}
                          max={5}
                          step={1}
                          value={binding.max_retries ?? 0}
                          onChange={(event) =>
                            patchRegisteredBinding(localName, {
                              max_retries: parseNonNegativeInteger(
                                event.target.value,
                              ),
                            })
                          }
                        />
                      </Field>
                      <Field label="Approval">
                        <label className="flex items-start gap-3 rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2 text-sm text-zinc-700">
                          <input
                            type="checkbox"
                            data-testid={workflowToolBindingTestId(
                              "workflow-tool-binding-approval",
                              localName,
                            )}
                            className="mt-0.5 rounded border-zinc-300"
                            checked={binding.requires_approval === true}
                            onChange={(event) =>
                              patchRegisteredBinding(localName, {
                                requires_approval: event.target.checked,
                              })
                            }
                          />
                          <span className="text-xs leading-relaxed">
                            Require a workflow approval gate before this tool is
                            executed.
                          </span>
                        </label>
                      </Field>
                      <div className="md:col-span-2">
                        <Field label="Secret refs">
                          <textarea
                            data-testid={workflowToolBindingTestId(
                              "workflow-tool-binding-secret-refs",
                              localName,
                            )}
                            className={textareaClass}
                            rows={3}
                            value={(binding.secret_refs ?? []).join("\n")}
                            onChange={(event) =>
                              patchRegisteredBinding(localName, {
                                secret_refs: uniqueTrimmedStrings(
                                  event.target.value
                                    .split("\n")
                                    .map((item) => item.trim()),
                                ),
                              })
                            }
                            placeholder="One secret ref per line"
                          />
                        </Field>
                      </div>
                    </div>
                  )
                ) : (
                  <div className="mt-4 space-y-3">
                    <div className="rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-xs leading-relaxed text-amber-800">
                      The current payload is outside the supported workflow
                      binding schema.
                    </div>
                    <pre className="max-h-48 overflow-auto rounded-xl border border-zinc-200 bg-zinc-50 px-3 py-2 text-[11px] text-zinc-700">
                      {formatJsonTextareaValue(raw) || "{}"}
                    </pre>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </Section>
  );
}

function WorkflowDeployGatesSection({
  manifest,
  evalDatasets,
  onChangeWorkflow,
}: {
  manifest: WorkflowManifest;
  evalDatasets: EvalDataset[];
  onChangeWorkflow: (patch: Partial<WorkflowManifest>) => void;
}): JSX.Element {
  const deployGateEntries = workflowDeployGateEntries(manifest);

  const patchDeployGates = (
    nextDeployGates: Record<string, WorkflowDeployGate>,
    options?: {
      selectedDataset?: EvalDataset | null;
    },
  ): void => {
    const patch: Partial<WorkflowManifest> = {
      deploy_gates: nextDeployGates,
    };
    if (options?.selectedDataset) {
      patch.artifacts = {
        ...workflowArtifacts(manifest),
        eval_datasets: {
          ...workflowEvalDatasetArtifacts(manifest),
          [options.selectedDataset.dataset_id]: {
            dataset_name: options.selectedDataset.name,
          },
        },
      };
    }
    onChangeWorkflow(patch);
  };

  const addDeployGate = (): void => {
    const existingNames = new Set(
      deployGateEntries.map(([gateName]) => gateName),
    );
    let index = 1;
    let gateName = `deploy_gate_${index}`;
    while (existingNames.has(gateName)) {
      index += 1;
      gateName = `deploy_gate_${index}`;
    }
    const selectedDataset = evalDatasets[0] ?? null;
    patchDeployGates(
      {
        ...(manifest.deploy_gates ?? {}),
        [gateName]: {
          type: "deploy_gate",
          dataset_ref: selectedDataset?.dataset_id ?? "",
          required_for_aliases: ["prod"],
          thresholds: { min_pass_rate: 1 },
        },
      },
      { selectedDataset },
    );
  };

  const removeDeployGate = (gateName: string): void => {
    const nextDeployGates = {
      ...(manifest.deploy_gates ?? {}),
    };
    delete nextDeployGates[gateName];
    onChangeWorkflow({ deploy_gates: nextDeployGates });
  };

  const updateDeployGate = (
    gateName: string,
    updater: (gate: WorkflowDeployGate) => WorkflowDeployGate,
    options?: {
      selectedDataset?: EvalDataset | null;
    },
  ): void => {
    const current = (manifest.deploy_gates ?? {})[gateName];
    if (!current) return;
    patchDeployGates(
      {
        ...(manifest.deploy_gates ?? {}),
        [gateName]: updater(current),
      },
      options,
    );
  };

  const setDeployGateThreshold = (
    gateName: string,
    thresholdKey: string,
    nextValue: number | null,
  ): void => {
    updateDeployGate(gateName, (gate) => {
      const nextThresholds = { ...(gate.thresholds ?? {}) };
      if (nextValue === null) delete nextThresholds[thresholdKey];
      else nextThresholds[thresholdKey] = nextValue;
      return {
        ...gate,
        thresholds: nextThresholds,
      };
    });
  };

  const toggleDeployGateAlias = (gateName: string, alias: string): void => {
    updateDeployGate(gateName, (gate) => {
      const selected = new Set(gate.required_for_aliases ?? []);
      if (selected.has(alias)) selected.delete(alias);
      else selected.add(alias);
      return {
        ...gate,
        required_for_aliases: Array.from(selected),
      };
    });
  };

  return (
    <Section title="Deploy gates">
      <div className="rounded-xl border border-zinc-200 bg-zinc-50 px-3 py-3 text-xs leading-relaxed text-zinc-600">
        Deploy gates bind evaluation datasets to promotion aliases so CALIBER
        can block weak workflow versions before they rotate into environments
        like prod or staging.
      </div>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="text-[11px] leading-relaxed text-zinc-500">
          {evalDatasets.length > 0
            ? "Selecting a dataset here also records its artifact reference in the workflow manifest automatically."
            : "No active eval datasets are available yet. Create one first, then add deploy gates here."}
        </div>
        <button
          type="button"
          data-testid="workflow-deploy-gates-add"
          onClick={addDeployGate}
          disabled={evalDatasets.length === 0}
          className="rounded-lg border border-zinc-200 bg-white px-3 py-1.5 text-xs font-semibold text-zinc-700 transition hover:border-zinc-300 hover:text-zinc-900 disabled:cursor-not-allowed disabled:border-zinc-200 disabled:bg-zinc-100 disabled:text-zinc-400"
        >
          Add gate
        </button>
      </div>
      {deployGateEntries.length === 0 ? (
        <div className="rounded-xl border border-dashed border-zinc-200 bg-white px-3 py-4 text-xs text-zinc-500">
          No deploy gates configured yet.
        </div>
      ) : (
        <div className="space-y-4">
          {deployGateEntries.map(([gateName, gate]) => {
            const aliases = uniqueTrimmedStrings([
              ...DEPLOY_GATE_ALIAS_HINTS,
              ...(gate.required_for_aliases ?? []),
            ]);
            const selectedDataset =
              evalDatasets.find(
                (dataset) => dataset.dataset_id === gate.dataset_ref,
              ) ?? null;
            const datasetArtifacts = workflowEvalDatasetArtifacts(manifest);
            const artifactName =
              datasetArtifacts[gate.dataset_ref]?.dataset_name ?? null;

            return (
              <div
                key={gateName}
                data-testid={workflowDeployGateTestId(
                  "workflow-deploy-gate-card",
                  gateName,
                )}
                className="rounded-2xl border border-zinc-200 bg-white p-4 shadow-sm"
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="font-mono text-sm text-zinc-900">
                      {gateName}
                    </div>
                    <div className="mt-2 flex flex-wrap gap-2 text-[11px]">
                      <span className="rounded-full border border-zinc-200 bg-zinc-50 px-2.5 py-1 font-semibold text-zinc-600">
                        Deploy gate
                      </span>
                      <span className="rounded-full border border-amber-200 bg-amber-50 px-2.5 py-1 font-semibold text-amber-700">
                        {gate.required_for_aliases?.length
                          ? `Aliases ${gate.required_for_aliases.join(", ")}`
                          : "No aliases selected"}
                      </span>
                    </div>
                  </div>
                  <button
                    type="button"
                    data-testid={workflowDeployGateTestId(
                      "workflow-deploy-gate-remove",
                      gateName,
                    )}
                    className="rounded-lg border border-red-200 px-2.5 py-1 text-xs font-medium text-red-600 transition-colors hover:bg-red-50"
                    onClick={() => removeDeployGate(gateName)}
                  >
                    Remove
                  </button>
                </div>

                <div className="mt-4 grid gap-3 md:grid-cols-2">
                  <Field label="Eval dataset">
                    <>
                      <select
                        data-testid={workflowDeployGateTestId(
                          "workflow-deploy-gate-dataset",
                          gateName,
                        )}
                        className={selectClass}
                        value={gate.dataset_ref}
                        onChange={(event) => {
                          const datasetRef = event.target.value;
                          const nextDataset =
                            evalDatasets.find(
                              (dataset) => dataset.dataset_id === datasetRef,
                            ) ?? null;
                          updateDeployGate(
                            gateName,
                            (current) => ({
                              ...current,
                              dataset_ref: datasetRef,
                            }),
                            { selectedDataset: nextDataset },
                          );
                        }}
                      >
                        <option value="">Select an eval dataset</option>
                        {selectedDataset === null &&
                          gate.dataset_ref &&
                          artifactName && (
                            <option value={gate.dataset_ref}>
                              {artifactName} ({gate.dataset_ref})
                            </option>
                          )}
                        {selectedDataset === null &&
                          gate.dataset_ref &&
                          !artifactName && (
                            <option value={gate.dataset_ref}>
                              {gate.dataset_ref} (not currently active)
                            </option>
                          )}
                        {evalDatasets.map((dataset) => (
                          <option
                            key={dataset.dataset_id}
                            value={dataset.dataset_id}
                          >
                            {dataset.name} ({dataset.dataset_id})
                          </option>
                        ))}
                      </select>
                      <div className="mt-2 text-[11px] leading-relaxed text-zinc-500">
                        {selectedDataset
                          ? `Gate uses ${selectedDataset.name} (${selectedDataset.dataset_id}).`
                          : artifactName
                            ? `Gate references saved dataset artifact ${artifactName}.`
                            : "Pick the dataset CALIBER should evaluate before promoting this alias."}
                      </div>
                    </>
                  </Field>
                  <Field label="Required aliases">
                    <>
                      <input
                        data-testid={workflowDeployGateTestId(
                          "workflow-deploy-gate-aliases",
                          gateName,
                        )}
                        className={inputClass}
                        value={(gate.required_for_aliases ?? []).join(", ")}
                        onChange={(event) =>
                          updateDeployGate(gateName, (current) => ({
                            ...current,
                            required_for_aliases: uniqueTrimmedStrings(
                              event.target.value.split(","),
                            ),
                          }))
                        }
                        placeholder="prod, staging"
                      />
                      <div className="mt-2 flex flex-wrap gap-2">
                        {aliases.map((alias) => {
                          const active = (
                            gate.required_for_aliases ?? []
                          ).includes(alias);
                          return (
                            <button
                              key={alias}
                              type="button"
                              data-testid={workflowDeployGateTestId(
                                `workflow-deploy-gate-alias-${alias}`,
                                gateName,
                              )}
                              onClick={() =>
                                toggleDeployGateAlias(gateName, alias)
                              }
                              className={`rounded-full border px-2.5 py-1 text-[11px] font-semibold transition ${
                                active
                                  ? "border-zinc-900 bg-zinc-900 text-white"
                                  : "border-zinc-200 bg-white text-zinc-600 hover:border-zinc-300 hover:text-zinc-900"
                              }`}
                            >
                              {alias}
                            </button>
                          );
                        })}
                      </div>
                    </>
                  </Field>
                </div>

                <div className="mt-4 grid gap-3 md:grid-cols-3">
                  <Field label="Min pass rate">
                    <input
                      data-testid={workflowDeployGateTestId(
                        "workflow-deploy-gate-threshold-min-pass-rate",
                        gateName,
                      )}
                      className={inputClass}
                      type="number"
                      min={0}
                      max={1}
                      step={0.05}
                      value={gate.thresholds?.min_pass_rate ?? ""}
                      onChange={(event) =>
                        setDeployGateThreshold(
                          gateName,
                          "min_pass_rate",
                          parseOptionalNumber(event.target.value),
                        )
                      }
                      placeholder="1.0"
                    />
                  </Field>
                  <Field label="Min overall delta">
                    <input
                      data-testid={workflowDeployGateTestId(
                        "workflow-deploy-gate-threshold-min-overall-delta",
                        gateName,
                      )}
                      className={inputClass}
                      type="number"
                      step={0.01}
                      value={gate.thresholds?.min_overall_delta ?? ""}
                      onChange={(event) =>
                        setDeployGateThreshold(
                          gateName,
                          "min_overall_delta",
                          parseOptionalNumber(event.target.value),
                        )
                      }
                      placeholder="0.02"
                    />
                  </Field>
                  {/* Replaces the former "Max tone regression" field. No product
                      scorer measures tone, so that threshold was accepted and
                      silently ignored — it read as a configured safety control
                      while enforcing nothing. The gate now rejects any threshold
                      it cannot evaluate, so only measurable bounds are offered. */}
                  <Field label="Max p95 latency (ms)">
                    <input
                      data-testid={workflowDeployGateTestId(
                        "workflow-deploy-gate-threshold-max-p95-latency-ms",
                        gateName,
                      )}
                      className={inputClass}
                      type="number"
                      min={0}
                      step={50}
                      value={gate.thresholds?.max_p95_latency_ms ?? ""}
                      onChange={(event) =>
                        setDeployGateThreshold(
                          gateName,
                          "max_p95_latency_ms",
                          parseOptionalNumber(event.target.value),
                        )
                      }
                      placeholder="5000"
                    />
                  </Field>
                </div>
                <p className="mt-2 text-xs text-slate-500">
                  Thresholds are evaluated against the graded replay. A threshold
                  the gate cannot measure — including one whose scorer is not
                  configured, or a dataset with no expected output — fails the gate
                  rather than being skipped. Use <code>min_completion_rate</code>{" "}
                  when the dataset has no expected answers.
                </p>
              </div>
            );
          })}
        </div>
      )}
    </Section>
  );
}

function NodeGuideSection({
  manifest,
  node,
  componentSpec,
  validationReport,
}: {
  manifest: WorkflowManifest;
  node: ManifestNode;
  componentSpec?: WorkflowComponent | null;
  validationReport: ValidationReport | null | undefined;
}): JSX.Element {
  const guide = nodeGuide(node, componentSpec ?? null, manifest);
  const issues = nodeValidationIssues(validationReport, node.id);
  const errorCount = issues.filter(
    (issue) => issue.severity === "error",
  ).length;
  const warningCount = issues.length - errorCount;
  const hasBlockingIssues = errorCount > 0;
  const hasGuideGaps = guide.missingLabels.length > 0;
  const toneClass = hasBlockingIssues
    ? "border-red-200 bg-red-50 text-red-800"
    : warningCount > 0 || hasGuideGaps
      ? "border-amber-200 bg-amber-50 text-amber-800"
      : "border-emerald-200 bg-emerald-50 text-emerald-800";
  const statusText = hasBlockingIssues
    ? `${errorCount} validation error${errorCount === 1 ? "" : "s"} currently reference this node.`
    : warningCount > 0
      ? `${warningCount} warning${warningCount === 1 ? "" : "s"} currently reference this node.`
      : hasGuideGaps
        ? "Configuration is incomplete and needs setup before publish."
        : "Configuration checklist is complete for this node.";

  return (
    <Section title="Guide">
      <div
        data-testid="inspector-node-guide"
        className={`rounded-xl border px-3 py-3 text-xs leading-relaxed ${toneClass}`}
      >
        <div className="text-sm font-semibold">{guide.summary}</div>
        <div className="mt-1.5">{statusText}</div>
      </div>

      {guide.tips.length > 0 && (
        <div className="space-y-2">
          {guide.tips.map((tip) => (
            <div
              key={tip}
              className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2 text-xs text-zinc-600"
            >
              {tip}
            </div>
          ))}
        </div>
      )}

      {guide.checks.length > 0 && (
        <div className="rounded-xl border border-zinc-200 bg-white">
          {guide.checks.map((check) => (
            <div
              key={check.label}
              className="flex items-start gap-2 border-b border-zinc-100 px-3 py-2 last:border-b-0"
            >
              <span
                className={`mt-0.5 inline-flex h-5 min-w-5 items-center justify-center rounded-full text-[10px] font-semibold ${
                  check.satisfied
                    ? "bg-emerald-100 text-emerald-700"
                    : "bg-amber-100 text-amber-700"
                }`}
              >
                {check.satisfied ? "OK" : "!"}
              </span>
              <div className="min-w-0">
                <div className="text-xs font-medium text-zinc-800">
                  {check.label}
                </div>
                <div className="mt-0.5 text-[11px] text-zinc-500">
                  {check.help}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {issues.length > 0 ? (
        <div
          data-testid="inspector-node-issues"
          className="rounded-xl border border-red-200 bg-red-50 px-3 py-2.5 text-xs text-red-700"
        >
          {issues.map((issue) => (
            <div key={`${issue.code}-${issue.path}`} className="py-0.5">
              {issue.message}
            </div>
          ))}
        </div>
      ) : validationReport ? (
        <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2 text-xs text-zinc-500">
          No validation issues currently reference this node.
        </div>
      ) : (
        <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2 text-xs text-zinc-500">
          Run Validate to surface graph-level issues for this node.
        </div>
      )}
    </Section>
  );
}

export function Inspector({
  manifest,
  projectId = null,
  selectedNodeId,
  focusFieldKey = null,
  focusFieldSignal = 0,
  tools,
  prompts = [],
  skills = [],
  evalDatasets = [],
  mcpServers,
  componentSpec,
  validationReport,
  lastStep = null,
  onChangeNode,
  onChangeWorkflow,
  onDeleteNode,
}: InspectorProps): JSX.Element {
  const node = selectedNodeId ? manifest.nodes[selectedNodeId] : null;
  const inspectorRootRef = useRef<HTMLDivElement | null>(null);
  const [highlightedFieldKey, setHighlightedFieldKey] = useState<string | null>(
    null,
  );
  const [showAdvanced, setShowAdvanced] = useState(false);
  const componentFieldMap = useMemo(
    () =>
      new Map((componentSpec?.fields ?? []).map((field) => [field.key, field])),
    [componentSpec],
  );
  const hasAdvancedFields = useMemo(
    () => (componentSpec?.fields ?? []).some((field) => field.advanced),
    [componentSpec],
  );
  const componentFieldFeedback = useMemo(() => {
    if (!node) {
      return new Map<
        string,
        { issues: ValidationIssue[]; setupChecks: NodeGuideCheck[] }
      >();
    }
    const fieldKeys = new Set<string>(
      (componentSpec?.fields ?? []).map((field) => field.key),
    );
    const issuePrefix = `nodes.${node.id}.`;
    for (const issue of [
      ...(validationReport?.errors ?? []),
      ...(validationReport?.warnings ?? []),
    ]) {
      if (!issue.path.startsWith(issuePrefix)) continue;
      const fieldKey = issue.path
        .slice(issuePrefix.length)
        .match(/^([^.[]+)/)?.[1];
      if (fieldKey) fieldKeys.add(fieldKey);
    }
    return new Map(
      Array.from(fieldKeys).map((fieldKey) => [
        fieldKey,
        {
          issues: nodeFieldValidationIssues(
            validationReport,
            node.id,
            fieldKey,
          ),
          setupChecks: nodeFieldSetupChecks(
            node,
            componentSpec ?? null,
            fieldKey,
            manifest,
          ).filter((check) => !check.satisfied),
        },
      ]),
    );
  }, [componentSpec, manifest, node, validationReport]);

  useEffect(() => {
    if (!node || !focusFieldKey) {
      setHighlightedFieldKey(null);
      return;
    }
    setHighlightedFieldKey(focusFieldKey);
    const focusTimer = window.setTimeout(() => {
      setHighlightedFieldKey((current) =>
        current === focusFieldKey ? null : current,
      );
    }, 2200);
    const run = window.setTimeout(() => {
      const target = inspectorRootRef.current?.querySelector<HTMLElement>(
        `[data-workflow-field-key="${focusFieldKey}"]`,
      );
      if (!target) return;
      target.scrollIntoView?.({ block: "center", behavior: "smooth" });
      const focusable = target.querySelector<HTMLElement>(
        'input, textarea, select, button, [tabindex]:not([tabindex="-1"])',
      );
      focusable?.focus?.({ preventScroll: true });
    }, 0);
    return () => {
      window.clearTimeout(run);
      window.clearTimeout(focusTimer);
    };
  }, [focusFieldKey, focusFieldSignal, node]);
  // Agent instructions can be inline text or a reference to a registered
  // (mlflow) prompt; the picker writes both the node ref and the
  // manifest-level artifacts.prompts entry the compiler resolves.
  const agentInstructions =
    node?.type === "agent"
      ? (node.instructions as
          | { type?: string; text?: string; ref?: string }
          | undefined)
      : undefined;
  const isPromptMode = agentInstructions?.type === "mlflow_prompt";
  const promptOptions = prompts.filter((p) => p.has_prompt && p.prompt_name);
  const activeMcpServers = (mcpServers ?? []).filter(
    (server) => server.status === "active",
  );
  const selectedMcpServerId =
    node && typeof node.server_id === "string" ? node.server_id : "";
  const selectedMcpServer = activeMcpServers.find(
    (server) => server.server_id === selectedMcpServerId,
  );
  const toolPickerOptions = useMemo(
    () => [
      ...tools.map((tool) => ({
        value: tool.name,
        label: tool.name,
        testId: `tool-${tool.name}`,
        badge: (
          <span
            className="rounded-full bg-zinc-100 px-1.5 py-0.5 text-[10px]"
            title={SIDE_EFFECT_LABEL[tool.side_effect_level] ?? ""}
          >
            {SIDE_EFFECT_BADGE[tool.side_effect_level] ?? ""}{" "}
            {SIDE_EFFECT_LABEL[tool.side_effect_level] ??
              tool.side_effect_level}
          </span>
        ),
      })),
      ...activeMcpServers.flatMap((server) =>
        server.discovered_tools.map((mcpTool) => {
          const key = `mcp:${server.name}/${mcpTool.name}`;
          return {
            value: key,
            label: `${server.name} / ${mcpTool.name}`,
            hint: server.name,
            testId: `mcp-tool-${key}`,
            badge: (
              <span className="rounded-full bg-purple-100 px-1.5 py-0.5 text-[10px] text-purple-700">
                MCP
              </span>
            ),
          };
        }),
      ),
    ],
    [activeMcpServers, tools],
  );
  const toolPickerValueSet = useMemo(
    () => new Set(toolPickerOptions.map((option) => option.value)),
    [toolPickerOptions],
  );
  const selectedToolBindingName =
    node?.type === "tool" && typeof node.tool_name === "string"
      ? node.tool_name
      : "";
  const selectedDirectToolDefinition =
    node?.type === "tool"
      ? (tools.find((tool) => tool.name === selectedToolBindingName) ?? null)
      : null;
  const selectedDirectMcpToolLabel =
    node?.type === "tool" && selectedToolBindingName.startsWith("mcp:")
      ? (toolPickerOptions.find(
          (option) => option.value === selectedToolBindingName,
        )?.label ?? selectedToolBindingName)
      : null;
  const forEachTargetOptions =
    node?.type === "for_each"
      ? targetNodeOptions(manifest, {
          excludeNodeId: node.id,
          allowedTypes: FOR_EACH_TARGET_TYPES,
          selectedNodeId:
            typeof node.target_node_id === "string"
              ? node.target_node_id
              : null,
        })
      : [];
  const loopTargetOptions =
    node?.type === "loop"
      ? targetNodeOptions(manifest, {
          excludeNodeId: node.id,
          allowedTypes: LOOP_TARGET_TYPES,
          selectedNodeId:
            typeof node.target_node_id === "string"
              ? node.target_node_id
              : null,
        })
      : [];
  const errorBoundaryTargetOptions =
    node?.type === "error_boundary"
      ? targetNodeOptions(manifest, {
          excludeNodeId: node.id,
          allowedTypes: ERROR_BOUNDARY_TARGET_TYPES,
          selectedNodeId:
            typeof node.target_node_id === "string"
              ? node.target_node_id
              : null,
        })
      : [];
  const errorBoundaryCompensationOptions =
    node?.type === "error_boundary"
      ? targetNodeOptions(manifest, {
          excludeNodeId: node.id,
          allowedTypes: ERROR_BOUNDARY_TARGET_TYPES,
          selectedNodeId:
            typeof node.compensate_with === "string"
              ? node.compensate_with
              : null,
        })
      : [];
  const agentOutputTypeValue =
    node?.type === "agent" ? formatJsonTextareaValue(node.output_type) : "";
  const [agentOutputTypeText, setAgentOutputTypeText] =
    useState(agentOutputTypeValue);
  const [agentOutputTypeError, setAgentOutputTypeError] = useState<
    string | null
  >(null);
  const workflowMlflow = (manifest.mlflow ?? {}) as {
    experiment_name?: string | null;
    trace_group_tags?: Record<string, string>;
  };
  const workflowTraceGroupTagsValue = formatTraceGroupTagsText(
    workflowMlflow.trace_group_tags,
  );
  const [workflowTraceGroupTagsText, setWorkflowTraceGroupTagsText] = useState(
    workflowTraceGroupTagsValue,
  );
  const [workflowTraceGroupTagsError, setWorkflowTraceGroupTagsError] =
    useState<string | null>(null);

  useEffect(() => {
    setAgentOutputTypeText(agentOutputTypeValue);
    setAgentOutputTypeError(null);
  }, [selectedNodeId, agentOutputTypeValue]);

  useEffect(() => {
    setWorkflowTraceGroupTagsText(workflowTraceGroupTagsValue);
    setWorkflowTraceGroupTagsError(null);
  }, [selectedNodeId, workflowTraceGroupTagsValue]);

  const commitAgentOutputType = (): void => {
    if (!node || node.type !== "agent") return;
    const trimmed = agentOutputTypeText.trim();
    if (!trimmed) {
      setAgentOutputTypeError(null);
      onChangeNode(node.id, { output_type: null });
      return;
    }
    try {
      const parsed = JSON.parse(trimmed);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("Schema must be a JSON object.");
      }
      const patch: Partial<ManifestNode> = {
        output_type: parsed as Record<string, unknown>,
      };
      if (!hasAgentStructuredOutputPort(node.outputs)) {
        patch.outputs = {
          ...(node.outputs ?? {}),
          structured_output: { type: "structured" },
        };
      }
      setAgentOutputTypeError(null);
      onChangeNode(node.id, patch);
    } catch (error) {
      setAgentOutputTypeError(
        error instanceof Error ? error.message : "Schema must be valid JSON.",
      );
    }
  };

  const commitWorkflowTraceGroupTags = (): void => {
    try {
      const parsed = parseTraceGroupTagsText(workflowTraceGroupTagsText);
      setWorkflowTraceGroupTagsError(null);
      onChangeWorkflow({
        mlflow: {
          ...workflowMlflow,
          trace_group_tags: parsed,
        },
      });
    } catch (error) {
      setWorkflowTraceGroupTagsError(
        error instanceof Error
          ? error.message
          : "Trace group tags must be valid key=value lines.",
      );
    }
  };

  const updateAgentToolConstraint = (
    toolRef: string,
    constraint: string,
  ): void => {
    if (!node || node.type !== "agent") return;
    const next = { ...(node.tool_constraints ?? {}) };
    if (constraint) next[toolRef] = constraint;
    else delete next[toolRef];
    onChangeNode(node.id, { tool_constraints: next });
  };

  const handleAgentEvalDatasetChange = (datasetRef: string): void => {
    if (!node || node.type !== "agent") return;
    onChangeNode(node.id, { eval_dataset: datasetRef || null });
    if (!datasetRef) return;
    const selectedDataset = evalDatasets.find(
      (dataset) => dataset.dataset_id === datasetRef,
    );
    if (!selectedDataset) return;
    onChangeWorkflow({
      artifacts: {
        ...workflowArtifacts(manifest),
        eval_datasets: {
          ...workflowEvalDatasetArtifacts(manifest),
          [selectedDataset.dataset_id]: {
            dataset_name: selectedDataset.name,
          },
        },
      },
    });
  };

  if (!node) {
    const sessionMode = workflowSessionMode(manifest);
    const runtime = workflowRuntimeConfig(manifest);
    const runtimeSession = runtime.session ?? {};
    const runtimeOpenAI = workflowRuntimeOpenAIConfig(runtime);
    const runtimeSdk =
      typeof runtime.sdk === "string" && runtime.sdk.trim()
        ? runtime.sdk.trim()
        : "openai-agents-python";
    const runtimeSdkVersionPolicy =
      typeof runtime.sdk_version_policy === "string" &&
      runtime.sdk_version_policy.trim()
        ? runtime.sdk_version_policy.trim()
        : "runtime-pinned";
    const runtimeCompilerVersion =
      typeof runtime.compiler_version === "string" &&
      runtime.compiler_version.trim()
        ? runtime.compiler_version.trim()
        : "caliber-workflow-compiler-v1";
    const runtimeDefaultModelRef =
      typeof runtime.default_model_ref === "string" &&
      runtime.default_model_ref.trim()
        ? runtime.default_model_ref
        : "CALIBER_WORKFLOW_DEFAULT_MODEL";
    const runtimeOpenAIWorkflowApi =
      typeof runtimeOpenAI.workflow_api === "string"
        ? runtimeOpenAI.workflow_api
        : "";
    const runtimeOpenAIParallelToolCalls =
      typeof runtimeOpenAI.parallel_tool_calls === "string"
        ? runtimeOpenAI.parallel_tool_calls
        : "";
    const runtimeOpenAIPromptCacheMode =
      typeof runtimeOpenAI.prompt_cache_mode === "string"
        ? runtimeOpenAI.prompt_cache_mode
        : "";
    const runtimeOpenAIPromptCacheRetention =
      typeof runtimeOpenAI.prompt_cache_retention === "string"
        ? runtimeOpenAI.prompt_cache_retention
        : "";
    const patchWorkflowOpenAI = (
      patch: Partial<WorkflowRuntimeOpenAIConfig>,
    ): void => {
      const nextRuntime: WorkflowRuntimeConfig = { ...runtime };
      const nextOpenAI = normalizeWorkflowRuntimeOpenAIConfig({
        ...runtimeOpenAI,
        ...patch,
      });
      if (nextOpenAI) {
        nextRuntime.openai = nextOpenAI;
      } else {
        delete nextRuntime.openai;
      }
      onChangeWorkflow({ runtime: nextRuntime });
    };
    const mlflowExperimentName =
      typeof workflowMlflow.experiment_name === "string"
        ? workflowMlflow.experiment_name
        : "";
    return (
      <COMPONENT_FIELD_CONTEXT.Provider value={componentFieldMap}>
        <COMPONENT_FIELD_FEEDBACK_CONTEXT.Provider value={null}>
          <COMPONENT_FIELD_HIGHLIGHT_CONTEXT.Provider value={null}>
            <div data-testid="wf-inspector" className="space-y-4">
              <div className="flex items-center gap-2 rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2.5">
                <span className="text-base">⚙️</span>
                <h3 className="text-sm font-semibold text-zinc-900">
                  Workflow settings
                </h3>
              </div>
              <Field label="Name">
                <input
                  className={inputClass}
                  value={manifest.name}
                  onChange={(e) => onChangeWorkflow({ name: e.target.value })}
                />
              </Field>
              <Field label="Owner">
                <input
                  className={inputClass}
                  value={manifest.owner ?? ""}
                  onChange={(e) => onChangeWorkflow({ owner: e.target.value })}
                />
              </Field>
              <Field label="Description">
                <textarea
                  className={textareaClass}
                  rows={3}
                  value={manifest.description ?? ""}
                  onChange={(e) =>
                    onChangeWorkflow({ description: e.target.value })
                  }
                />
              </Field>
              <Field label="Session memory">
                <select
                  data-testid="workflow-session-mode"
                  className={selectClass}
                  value={sessionMode}
                  onChange={(e) => {
                    onChangeWorkflow({
                      runtime: {
                        ...runtime,
                        session: {
                          ...runtimeSession,
                          type: e.target.value as WorkflowSessionMode,
                        },
                      },
                    } as Partial<WorkflowManifest>);
                  }}
                >
                  <option value="none">Disabled</option>
                  <option value="in_memory">In-memory</option>
                  <option value="persistent">Persistent</option>
                </select>
                <p className="mt-1 text-[11px] leading-relaxed text-zinc-500">
                  Automatic agent memory is keyed by the workflow run&apos;s
                  shared <span className="font-mono">session_id</span>. Reuse
                  the same session ID across preview or run requests to continue
                  the conversation.
                </p>
              </Field>
              <Section title="Runtime defaults">
                <Field label="Default model ref">
                  <input
                    data-testid="workflow-default-model-ref"
                    className={inputClass}
                    value={runtimeDefaultModelRef}
                    onChange={(e) =>
                      onChangeWorkflow({
                        runtime: {
                          ...runtime,
                          default_model_ref: e.target.value,
                        },
                      } as Partial<WorkflowManifest>)
                    }
                    placeholder="CALIBER_WORKFLOW_DEFAULT_MODEL"
                  />
                  <p className="mt-1 text-[11px] leading-relaxed text-zinc-500">
                    Agents that keep their model set to{" "}
                    <span className="font-mono">inherit</span> resolve against
                    this workflow-wide reference at runtime.
                  </p>
                </Field>
                <div className="grid gap-3 lg:grid-cols-2">
                  <Field label="OpenAI API surface">
                    <>
                      <select
                        data-testid="workflow-openai-api"
                        className={selectClass}
                        value={runtimeOpenAIWorkflowApi}
                        onChange={(e) =>
                          patchWorkflowOpenAI({
                            workflow_api: e.target.value
                              ? (e.target
                                  .value as WorkflowRuntimeOpenAIConfig["workflow_api"])
                              : null,
                          })
                        }
                      >
                        <option value="">Inherit deployment default</option>
                        <option value="chat_completions">
                          Chat Completions
                        </option>
                        <option value="responses">Responses API</option>
                        <option value="agents_sdk">Agents SDK</option>
                      </select>
                      <p className="mt-1 text-[11px] leading-relaxed text-zinc-500">
                        Overrides how agent nodes talk to OpenAI for this
                        workflow only. Secrets and gateway endpoints still come
                        from the deployment runtime.
                      </p>
                    </>
                  </Field>
                  <Field label="Parallel tool calls">
                    <select
                      data-testid="workflow-openai-parallel-tool-calls"
                      className={selectClass}
                      value={runtimeOpenAIParallelToolCalls}
                      onChange={(e) =>
                        patchWorkflowOpenAI({
                          parallel_tool_calls: e.target.value
                            ? (e.target
                                .value as WorkflowRuntimeOpenAIConfig["parallel_tool_calls"])
                            : null,
                        })
                      }
                    >
                      <option value="">Inherit deployment default</option>
                      <option value="auto">Auto</option>
                      <option value="enabled">Enabled</option>
                      <option value="disabled">Disabled</option>
                    </select>
                  </Field>
                  <Field label="Prompt-cache hints">
                    <select
                      data-testid="workflow-openai-prompt-cache-mode"
                      className={selectClass}
                      value={runtimeOpenAIPromptCacheMode}
                      onChange={(e) =>
                        patchWorkflowOpenAI({
                          prompt_cache_mode: e.target.value
                            ? (e.target
                                .value as WorkflowRuntimeOpenAIConfig["prompt_cache_mode"])
                            : null,
                        })
                      }
                    >
                      <option value="">Inherit deployment default</option>
                      <option value="auto">Auto</option>
                      <option value="enabled">Enabled</option>
                      <option value="disabled">Disabled</option>
                    </select>
                  </Field>
                  <Field label="Prompt-cache retention">
                    <>
                      <select
                        data-testid="workflow-openai-prompt-cache-retention"
                        className={selectClass}
                        value={runtimeOpenAIPromptCacheRetention}
                        onChange={(e) =>
                          patchWorkflowOpenAI({
                            prompt_cache_retention: e.target.value
                              ? (e.target
                                  .value as WorkflowRuntimeOpenAIConfig["prompt_cache_retention"])
                              : null,
                          })
                        }
                      >
                        <option value="">Inherit deployment default</option>
                        <option value="default">Model / org default</option>
                        <option value="in_memory">In-memory</option>
                        <option value="24h">24h extended</option>
                      </select>
                      <p className="mt-1 text-[11px] leading-relaxed text-zinc-500">
                        Choose a workflow-specific cache retention policy when
                        the selected OpenAI model supports it.
                      </p>
                    </>
                  </Field>
                </div>
                <div className="grid gap-3 sm:grid-cols-3">
                  <div className="rounded-xl border border-zinc-200 bg-zinc-50 px-3 py-3">
                    <div className="text-[10px] font-semibold uppercase tracking-[0.14em] text-zinc-400">
                      SDK
                    </div>
                    <div className="mt-1 font-mono text-xs text-zinc-800">
                      {runtimeSdk}
                    </div>
                  </div>
                  <div className="rounded-xl border border-zinc-200 bg-zinc-50 px-3 py-3">
                    <div className="text-[10px] font-semibold uppercase tracking-[0.14em] text-zinc-400">
                      SDK policy
                    </div>
                    <div className="mt-1 font-mono text-xs text-zinc-800">
                      {runtimeSdkVersionPolicy}
                    </div>
                  </div>
                  <div className="rounded-xl border border-zinc-200 bg-zinc-50 px-3 py-3">
                    <div className="text-[10px] font-semibold uppercase tracking-[0.14em] text-zinc-400">
                      Compiler
                    </div>
                    <div className="mt-1 font-mono text-xs text-zinc-800">
                      {runtimeCompilerVersion}
                    </div>
                  </div>
                </div>
              </Section>
              <WorkflowPromptArtifactsSection
                manifest={manifest}
                prompts={prompts}
                onChangeWorkflow={onChangeWorkflow}
              />
              <Section title="MLflow">
                <Field label="Experiment name">
                  <input
                    data-testid="workflow-mlflow-experiment-name"
                    className={inputClass}
                    value={mlflowExperimentName}
                    onChange={(e) =>
                      onChangeWorkflow({
                        mlflow: {
                          ...workflowMlflow,
                          experiment_name: e.target.value || null,
                        },
                      })
                    }
                    placeholder="e.g. caliber/support"
                  />
                  <p className="mt-1 text-[11px] leading-relaxed text-zinc-500">
                    When set, workflow executions request this MLflow experiment
                    for their root trace run.
                  </p>
                </Field>
                <Field label="Trace group tags">
                  <>
                    <textarea
                      data-testid="workflow-mlflow-trace-group-tags"
                      className={textareaClass}
                      rows={4}
                      value={workflowTraceGroupTagsText}
                      onChange={(e) =>
                        setWorkflowTraceGroupTagsText(e.target.value)
                      }
                      onBlur={commitWorkflowTraceGroupTags}
                      placeholder={"team=ops\nservice=support"}
                    />
                    <p className="mt-1 text-[11px] leading-relaxed text-zinc-500">
                      Add one <span className="font-mono">key=value</span> pair
                      per line. These tags flow into workflow run traces and
                      step spans.
                    </p>
                    {workflowTraceGroupTagsError && (
                      <div className="mt-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-[11px] text-red-700">
                        {workflowTraceGroupTagsError}
                      </div>
                    )}
                  </>
                </Field>
              </Section>
              <WorkflowToolBindingsSection
                manifest={manifest}
                onChangeWorkflow={onChangeWorkflow}
              />
              <WorkflowDeployGatesSection
                manifest={manifest}
                evalDatasets={evalDatasets}
                onChangeWorkflow={onChangeWorkflow}
              />
            </div>
          </COMPONENT_FIELD_HIGHLIGHT_CONTEXT.Provider>
        </COMPONENT_FIELD_FEEDBACK_CONTEXT.Provider>
      </COMPONENT_FIELD_CONTEXT.Provider>
    );
  }

  const color = nodeColor(node.type);

  return (
    <COMPONENT_FIELD_CONTEXT.Provider value={componentFieldMap}>
      <COMPONENT_FIELD_FEEDBACK_CONTEXT.Provider value={componentFieldFeedback}>
        <COMPONENT_FIELD_HIGHLIGHT_CONTEXT.Provider value={highlightedFieldKey}>
          <COMPONENT_SHOW_ADVANCED_CONTEXT.Provider value={showAdvanced}>
            <div
              ref={inspectorRootRef}
              data-testid="wf-inspector"
              data-node-type={node.type}
              className="space-y-4"
            >
              {/* Type-accented header — n8n style */}
              <div className="flex items-center justify-between rounded-lg border border-zinc-200 px-3 py-2.5">
                <div className="flex items-center gap-2">
                  <span
                    className="flex h-7 w-7 items-center justify-center rounded-md"
                    style={{ backgroundColor: `${color}12`, color }}
                  >
                    <NodeIcon type={node.type} size={16} />
                  </span>
                  <div>
                    <h3 className="text-sm font-semibold text-zinc-900">
                      {node.id}
                    </h3>
                    <div
                      className="text-[10px] uppercase font-medium tracking-wider"
                      style={{ color }}
                    >
                      {node.type.replace("_", " ")}
                    </div>
                  </div>
                </div>
                {onDeleteNode &&
                  node.type !== "start" &&
                  node.type !== "output" && (
                    <button
                      type="button"
                      data-testid="inspector-delete"
                      className="flex items-center gap-1 rounded-lg border border-red-200 px-2.5 py-1 text-xs font-medium text-red-600 transition-colors hover:bg-red-50 active:scale-[0.97]"
                      onClick={() => onDeleteNode(node.id)}
                    >
                      🗑 Delete
                    </button>
                  )}
              </div>

              <NodeMetaSection node={node} onChangeNode={onChangeNode} />

              {lastStep && <NodeOutputSection step={lastStep} />}

              {hasAdvancedFields && (
                <div className="flex items-center justify-end">
                  <button
                    type="button"
                    data-testid="inspector-toggle-advanced"
                    aria-pressed={showAdvanced}
                    onClick={() => setShowAdvanced((v) => !v)}
                    className="text-[11px] font-medium text-caliber-600 transition-colors hover:text-caliber-700"
                  >
                    {showAdvanced
                      ? "Hide advanced fields"
                      : "Show advanced fields"}
                  </button>
                </div>
              )}

              {node.type === "start" && (
                <StartTriggerSection
                  workflowId={manifest.workflow_id}
                  nodeId={node.id}
                  trigger={(node.trigger ?? null) as StartTriggerConfig | null}
                  onChangeNode={onChangeNode}
                />
              )}

              <NodeGuideSection
                manifest={manifest}
                node={node}
                componentSpec={componentSpec}
                validationReport={validationReport}
              />

              {componentSpec && (
                <Section title="Runtime schema">
                  <WorkflowComponentSchemaSummary component={componentSpec} />
                </Section>
              )}

              {node.type === "agent" && (
                <>
                  <Section title="Configuration">
                    <Field label="Name" fieldKey="name">
                      <input
                        className={inputClass}
                        value={node.name ?? ""}
                        onChange={(e) =>
                          onChangeNode(node.id, { name: e.target.value })
                        }
                      />
                    </Field>
                    <Field label="Model" fieldKey="model">
                      <input
                        className={inputClass}
                        value={
                          typeof node.model === "string"
                            ? node.model
                            : "inherit"
                        }
                        onChange={(e) =>
                          onChangeNode(node.id, { model: e.target.value })
                        }
                      />
                    </Field>
                  </Section>
                  <Section title="Instructions">
                    <Field
                      label="Prompt or inline instructions"
                      fieldKey="instructions"
                    >
                      <div className="mb-2 inline-flex rounded-lg border border-zinc-200 p-0.5 text-xs">
                        <button
                          type="button"
                          data-testid="instructions-mode-inline"
                          onClick={() =>
                            onChangeNode(node.id, {
                              instructions: {
                                type: "inline",
                                text: agentInstructions?.text ?? "",
                              },
                            })
                          }
                          className={`rounded-md px-2 py-1 font-medium transition-colors ${
                            isPromptMode
                              ? "text-zinc-500 hover:bg-zinc-50"
                              : "bg-zinc-900 text-white"
                          }`}
                        >
                          Inline
                        </button>
                        <button
                          type="button"
                          data-testid="instructions-mode-prompt"
                          onClick={() =>
                            onChangeNode(node.id, {
                              instructions: {
                                type: "mlflow_prompt",
                                ref: agentInstructions?.ref ?? "",
                              },
                            })
                          }
                          className={`rounded-md px-2 py-1 font-medium transition-colors ${
                            isPromptMode
                              ? "bg-zinc-900 text-white"
                              : "text-zinc-500 hover:bg-zinc-50"
                          }`}
                        >
                          Registered prompt
                        </button>
                      </div>

                      {isPromptMode ? (
                        <>
                          <select
                            data-testid="inspector-prompt-ref"
                            aria-label="Registered prompt"
                            className={selectClass}
                            value={agentInstructions?.ref ?? ""}
                            onChange={(e) => {
                              const ref = e.target.value;
                              onChangeNode(node.id, {
                                instructions: { type: "mlflow_prompt", ref },
                              });
                              if (!ref) return;
                              const picked = prompts.find(
                                (p) => p.prompt_name === ref,
                              );
                              const currentArtifacts =
                                workflowPromptArtifacts(manifest);
                              const existingArtifact = currentArtifacts[ref];
                              onChangeWorkflow({
                                artifacts: {
                                  ...workflowArtifacts(manifest),
                                  prompts: {
                                    ...currentArtifacts,
                                    [ref]: {
                                      registry_name: ref,
                                      alias:
                                        picked?.alias ??
                                        existingArtifact?.alias ??
                                        "prod",
                                      managed_by:
                                        existingArtifact?.managed_by ??
                                        "mlflow_prompt_registry",
                                    },
                                  },
                                },
                              } as Partial<WorkflowManifest>);
                            }}
                          >
                            <option value="">Select a prompt…</option>
                            {promptOptions.map((p) => (
                              <option
                                key={p.prompt_name!}
                                value={p.prompt_name!}
                              >
                                {p.prompt_name} @{p.alias}
                              </option>
                            ))}
                          </select>
                          {!agentInstructions?.ref && (
                            <p className="mt-1 text-[11px] text-amber-600">
                              Pick a registered prompt — the agent won’t compile
                              until its instructions resolve.
                            </p>
                          )}
                          {promptOptions.length === 0 && (
                            <p className="mt-1 text-[11px] text-zinc-400">
                              No registered prompts available.
                            </p>
                          )}
                        </>
                      ) : (
                        <textarea
                          data-testid="inspector-instructions"
                          aria-label="Agent instructions"
                          className={textareaClass}
                          rows={5}
                          value={
                            agentInstructions?.type === "inline"
                              ? (agentInstructions.text ?? "")
                              : ""
                          }
                          onChange={(e) =>
                            onChangeNode(node.id, {
                              instructions: {
                                type: "inline",
                                text: e.target.value,
                              },
                            })
                          }
                        />
                      )}
                    </Field>
                  </Section>
                  <Section title="Tools">
                    <Field label="Allowed tools" fieldKey="tools">
                      <ChipMultiSelect
                        prefix="tools"
                        addLabel="Add tool"
                        emptyText="No registered tools or MCP servers."
                        searchPlaceholder="Search tools…"
                        selected={node.tools ?? []}
                        onChange={(next) =>
                          onChangeNode(node.id, {
                            tools: next,
                            tool_constraints: pruneToolConstraints(
                              node.tool_constraints,
                              next,
                            ),
                          })
                        }
                        options={toolPickerOptions}
                      />
                    </Field>
                  </Section>
                  <Section title="Tool rules">
                    <Field
                      label="Per-tool constraints"
                      fieldKey="tool_constraints"
                    >
                      {(node.tools ?? []).length === 0 ? (
                        <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2.5 text-xs text-zinc-500">
                          Select one or more tools to configure claim or
                          grounding rules.
                        </div>
                      ) : (
                        <div className="space-y-2">
                          {(node.tools ?? []).map((toolRef) => (
                            <label
                              key={toolRef}
                              className="grid gap-1 rounded-lg border border-zinc-200 bg-white px-3 py-2 text-xs text-zinc-600"
                            >
                              <span className="font-mono text-[11px] text-zinc-700">
                                {toolRef}
                              </span>
                              <select
                                data-testid={`inspector-tool-constraint-${toolRef.replace(/[^a-zA-Z0-9_-]/g, "_")}`}
                                className={selectClass}
                                value={node.tool_constraints?.[toolRef] ?? ""}
                                onChange={(e) =>
                                  updateAgentToolConstraint(
                                    toolRef,
                                    e.target.value,
                                  )
                                }
                              >
                                <option value="">No extra rule</option>
                                <option value="required_before_claim">
                                  Required before claim
                                </option>
                              </select>
                            </label>
                          ))}
                          <p className="text-[11px] leading-relaxed text-zinc-500">
                            Use these rules to bias the compiler and guardrails
                            toward grounded answers for higher-risk tools.
                          </p>
                        </div>
                      )}
                    </Field>
                  </Section>
                  <Section title="Structured output">
                    <Field label="JSON Schema" fieldKey="output_type">
                      <textarea
                        data-testid="inspector-agent-output-type"
                        className={textareaClass}
                        rows={10}
                        placeholder={`{\n  "type": "object",\n  "properties": {\n    "answer": { "type": "string" }\n  },\n  "required": ["answer"]\n}`}
                        value={agentOutputTypeText}
                        onChange={(e) => {
                          setAgentOutputTypeText(e.target.value);
                          if (agentOutputTypeError)
                            setAgentOutputTypeError(null);
                        }}
                        onBlur={commitAgentOutputType}
                      />
                      {agentOutputTypeError ? (
                        <p className="mt-1 text-[11px] text-red-600">
                          {agentOutputTypeError}
                        </p>
                      ) : (
                        <p className="mt-1 text-[11px] leading-relaxed text-zinc-500">
                          Save a JSON Schema here to request parsed agent
                          output. The first time you add a schema, Caliber also
                          adds a{" "}
                          <span className="font-mono">structured_output</span>{" "}
                          port so downstream nodes can consume the object
                          directly.
                        </p>
                      )}
                    </Field>
                  </Section>
                  <Section title="Evaluation">
                    <Field label="Eval dataset" fieldKey="eval_dataset">
                      <select
                        data-testid="inspector-agent-eval-dataset"
                        className={selectClass}
                        value={
                          typeof node.eval_dataset === "string"
                            ? node.eval_dataset
                            : ""
                        }
                        onChange={(e) =>
                          handleAgentEvalDatasetChange(e.target.value)
                        }
                      >
                        <option value="">No dataset</option>
                        {evalDatasets.map((dataset) => (
                          <option
                            key={dataset.dataset_id}
                            value={dataset.dataset_id}
                          >
                            {dataset.name} ({dataset.dataset_id})
                          </option>
                        ))}
                      </select>
                      {evalDatasets.length === 0 && (
                        <p className="mt-1 text-[11px] text-zinc-400">
                          No active eval datasets available.
                        </p>
                      )}
                    </Field>
                  </Section>
                  <Section title="Skills">
                    <Field label="Reusable skills" fieldKey="skills">
                      <ChipMultiSelect
                        prefix="skills"
                        addLabel="Add skill"
                        emptyText="No registered skills."
                        searchPlaceholder="Search skills…"
                        selected={node.skills ?? []}
                        onChange={(next) =>
                          onChangeNode(node.id, { skills: next })
                        }
                        options={skills.map((skill) => ({
                          value: skill.name,
                          label: skill.name,
                          hint: skill.summary,
                          testId: `skill-${skill.name}`,
                        }))}
                      />
                    </Field>
                  </Section>
                  <Section title="Handoffs">
                    <Field label="Delegation handoffs" fieldKey="handoffs">
                      <AgentHandoffEditor
                        agentId={node.id}
                        nodes={manifest.nodes}
                        handoffs={node.handoffs ?? []}
                        onChange={(handoffs) =>
                          onChangeNode(node.id, { handoffs })
                        }
                      />
                    </Field>
                  </Section>
                </>
              )}

              {node.type === "file_input" && (
                <Section title="File source">
                  <Field label="Managed project file" fieldKey="file_ref">
                    {projectId ? (
                      <ManagedProjectFileSelect
                        projectId={projectId}
                        value={node.file_ref?.file_id ?? ""}
                        onSelect={(selectedFile) => {
                          onChangeNode(node.id, {
                            file_ref: selectedFile?.immutable_ref ?? null,
                            path: "",
                          });
                        }}
                      />
                    ) : (
                      <select
                        data-testid="inspector-managed-file"
                        className={selectClass}
                        disabled
                        value=""
                        onChange={() => undefined}
                      >
                        <option value="">Select an active project first</option>
                      </select>
                    )}
                  </Field>
                  <Field label="Legacy host path (advanced)" fieldKey="path">
                    <input
                      data-testid="inspector-file-path"
                      className={inputClass}
                      value={typeof node.path === "string" ? node.path : ""}
                      onChange={(e) =>
                        onChangeNode(node.id, {
                          path: e.target.value,
                          file_ref: null,
                        })
                      }
                    />
                  </Field>
                  <Field label="Max bytes" fieldKey="max_bytes">
                    <input
                      data-testid="inspector-file-max-bytes"
                      className={inputClass}
                      type="number"
                      min={1}
                      max={5000000}
                      value={
                        typeof node.max_bytes === "number"
                          ? node.max_bytes
                          : 200000
                      }
                      onChange={(e) =>
                        onChangeNode(node.id, {
                          max_bytes: Number(e.target.value) || 1,
                        })
                      }
                    />
                  </Field>
                  <Field label="Encoding" fieldKey="encoding">
                    <input
                      className={inputClass}
                      value={
                        typeof node.encoding === "string"
                          ? node.encoding
                          : "utf-8"
                      }
                      onChange={(e) =>
                        onChangeNode(node.id, { encoding: e.target.value })
                      }
                    />
                  </Field>
                </Section>
              )}

              {node.type === "folder_input" && (
                <Section title="Folder source">
                  <Field label="Path" fieldKey="path">
                    <input
                      data-testid="inspector-folder-path"
                      className={inputClass}
                      value={typeof node.path === "string" ? node.path : ""}
                      onChange={(e) =>
                        onChangeNode(node.id, { path: e.target.value })
                      }
                    />
                  </Field>
                  <Field label="Pattern" fieldKey="pattern">
                    <input
                      data-testid="inspector-folder-pattern"
                      className={inputClass}
                      value={
                        typeof node.pattern === "string" ? node.pattern : "**/*"
                      }
                      onChange={(e) =>
                        onChangeNode(node.id, { pattern: e.target.value })
                      }
                    />
                  </Field>
                  <Field label="Recursive" fieldKey="recursive">
                    <span className="flex items-center gap-2 rounded-lg border border-zinc-200 bg-white px-3 py-2 text-xs text-zinc-700">
                      <input
                        data-testid="inspector-folder-recursive"
                        aria-label="Recursive"
                        type="checkbox"
                        className="rounded border-zinc-300"
                        checked={node.recursive !== false}
                        onChange={(e) =>
                          onChangeNode(node.id, { recursive: e.target.checked })
                        }
                      />
                      <span className="font-medium">
                        Include nested folders
                      </span>
                    </span>
                  </Field>
                  <Field label="Max files" fieldKey="max_files">
                    <input
                      data-testid="inspector-folder-max-files"
                      className={inputClass}
                      type="number"
                      min={1}
                      max={500}
                      value={
                        typeof node.max_files === "number" ? node.max_files : 50
                      }
                      onChange={(e) =>
                        onChangeNode(node.id, {
                          max_files: Number(e.target.value) || 1,
                        })
                      }
                    />
                  </Field>
                  <Field label="Bytes per file" fieldKey="max_bytes_per_file">
                    <input
                      className={inputClass}
                      type="number"
                      min={1}
                      max={1000000}
                      value={
                        typeof node.max_bytes_per_file === "number"
                          ? node.max_bytes_per_file
                          : 100000
                      }
                      onChange={(e) =>
                        onChangeNode(node.id, {
                          max_bytes_per_file: Number(e.target.value) || 1,
                        })
                      }
                    />
                  </Field>
                  <Field label="Encoding" fieldKey="encoding">
                    <input
                      className={inputClass}
                      value={
                        typeof node.encoding === "string"
                          ? node.encoding
                          : "utf-8"
                      }
                      onChange={(e) =>
                        onChangeNode(node.id, { encoding: e.target.value })
                      }
                    />
                  </Field>
                </Section>
              )}

              {node.type === "input_bucket" && (
                <Section title="Input bucket">
                  <Field label="Bucket" fieldKey="bucket">
                    <BucketSelect
                      testId="inspector-input-bucket"
                      value={typeof node.bucket === "string" ? node.bucket : ""}
                      onChange={(bucket) => onChangeNode(node.id, { bucket })}
                    />
                  </Field>
                  <Field label="Prefix" fieldKey="prefix">
                    <BucketPrefixField
                      testId="inspector-input-bucket-prefix"
                      bucket={
                        typeof node.bucket === "string" ? node.bucket : ""
                      }
                      value={typeof node.prefix === "string" ? node.prefix : ""}
                      onChange={(prefix) => onChangeNode(node.id, { prefix })}
                      placeholder="e.g. docs/ (optional)"
                    />
                  </Field>
                  <Field label="Recursive" fieldKey="recursive">
                    <span className="flex items-center gap-2 rounded-lg border border-zinc-200 bg-white px-3 py-2 text-xs text-zinc-700">
                      <input
                        data-testid="inspector-input-bucket-recursive"
                        aria-label="Recursive"
                        type="checkbox"
                        className="rounded border-zinc-300"
                        checked={node.recursive !== false}
                        onChange={(e) =>
                          onChangeNode(node.id, { recursive: e.target.checked })
                        }
                      />
                      <span className="font-medium">
                        Include nested prefixes
                      </span>
                    </span>
                  </Field>
                  <Field label="Max objects" fieldKey="max_files">
                    <input
                      className={inputClass}
                      type="number"
                      min={1}
                      max={500}
                      value={
                        typeof node.max_files === "number" ? node.max_files : 50
                      }
                      onChange={(e) =>
                        onChangeNode(node.id, {
                          max_files: Number(e.target.value) || 1,
                        })
                      }
                    />
                  </Field>
                  <Field label="Bytes per object" fieldKey="max_bytes_per_file">
                    <input
                      className={inputClass}
                      type="number"
                      min={1}
                      max={5000000}
                      value={
                        typeof node.max_bytes_per_file === "number"
                          ? node.max_bytes_per_file
                          : 100000
                      }
                      onChange={(e) =>
                        onChangeNode(node.id, {
                          max_bytes_per_file: Number(e.target.value) || 1,
                        })
                      }
                    />
                  </Field>
                  <Field label="Encoding" fieldKey="encoding">
                    <input
                      data-testid="inspector-input-bucket-encoding"
                      className={inputClass}
                      value={
                        typeof node.encoding === "string"
                          ? node.encoding
                          : "utf-8"
                      }
                      onChange={(e) =>
                        onChangeNode(node.id, { encoding: e.target.value })
                      }
                    />
                  </Field>
                  <Field label="Objects">
                    <BucketContentsField
                      testId="inspector-input-bucket-contents"
                      bucket={
                        typeof node.bucket === "string" ? node.bucket : ""
                      }
                      prefix={
                        typeof node.prefix === "string" ? node.prefix : ""
                      }
                    />
                  </Field>
                </Section>
              )}

              {node.type === "output_bucket" && (
                <Section title="Output bucket">
                  <Field label="Bucket" fieldKey="bucket">
                    <BucketSelect
                      testId="inspector-output-bucket"
                      value={typeof node.bucket === "string" ? node.bucket : ""}
                      onChange={(bucket) => onChangeNode(node.id, { bucket })}
                    />
                  </Field>
                  <Field label="Prefix" fieldKey="prefix">
                    <BucketPrefixField
                      testId="inspector-output-bucket-prefix"
                      bucket={
                        typeof node.bucket === "string" ? node.bucket : ""
                      }
                      value={typeof node.prefix === "string" ? node.prefix : ""}
                      onChange={(prefix) => onChangeNode(node.id, { prefix })}
                      placeholder="e.g. runs/output/ (optional)"
                    />
                  </Field>
                  <Field label="Overwrite" fieldKey="overwrite">
                    <span className="flex items-center gap-2 rounded-lg border border-zinc-200 bg-white px-3 py-2 text-xs text-zinc-700">
                      <input
                        data-testid="inspector-output-bucket-overwrite"
                        aria-label="Overwrite existing objects"
                        type="checkbox"
                        className="rounded border-zinc-300"
                        checked={node.overwrite !== false}
                        onChange={(e) =>
                          onChangeNode(node.id, { overwrite: e.target.checked })
                        }
                      />
                      <span className="font-medium">
                        Overwrite existing objects
                      </span>
                    </span>
                  </Field>
                  <p className="text-[11px] leading-relaxed text-zinc-500">
                    Writes every artifact produced by the workflow into this
                    bucket.
                  </p>
                  {typeof node.bucket === "string" &&
                    node.bucket.trim() !== "" && (
                      <a
                        href={`/object-store?bucket=${encodeURIComponent(node.bucket)}${
                          typeof node.prefix === "string" && node.prefix
                            ? `&prefix=${encodeURIComponent(node.prefix.replace(/^\/+/, ""))}`
                            : ""
                        }`}
                        target="_blank"
                        rel="noreferrer"
                        className="inline-block text-[11px] font-medium text-caliber-purple hover:underline"
                      >
                        Open in Object Store ↗
                      </a>
                    )}
                </Section>
              )}

              {node.type === "output_folder" && (
                <Section title="Output folder">
                  <Field label="Path" fieldKey="path">
                    <input
                      data-testid="inspector-output-folder-path"
                      className={inputClass}
                      placeholder="e.g. /data/exports"
                      value={typeof node.path === "string" ? node.path : ""}
                      onChange={(e) =>
                        onChangeNode(node.id, { path: e.target.value })
                      }
                    />
                  </Field>
                  <Field label="Overwrite" fieldKey="overwrite">
                    <span className="flex items-center gap-2 rounded-lg border border-zinc-200 bg-white px-3 py-2 text-xs text-zinc-700">
                      <input
                        data-testid="inspector-output-folder-overwrite"
                        aria-label="Overwrite existing files"
                        type="checkbox"
                        className="rounded border-zinc-300"
                        checked={node.overwrite !== false}
                        onChange={(e) =>
                          onChangeNode(node.id, { overwrite: e.target.checked })
                        }
                      />
                      <span className="font-medium">
                        Overwrite existing files
                      </span>
                    </span>
                  </Field>
                  <p className="text-[11px] leading-relaxed text-zinc-500">
                    Writes every artifact produced by the workflow into this
                    folder.
                  </p>
                </Section>
              )}

              {node.type === "wait_until" && (
                <Section title="Wait until">
                  <Field label="Timestamp (ISO)" fieldKey="wait_until">
                    <input
                      data-testid="inspector-wait-until"
                      className={inputClass}
                      value={
                        typeof node.wait_until === "string"
                          ? node.wait_until
                          : ""
                      }
                      onChange={(e) =>
                        onChangeNode(node.id, { wait_until: e.target.value })
                      }
                    />
                  </Field>
                  <Field label="Timezone" fieldKey="timezone">
                    <input
                      className={inputClass}
                      value={
                        typeof node.timezone === "string"
                          ? node.timezone
                          : "UTC"
                      }
                      onChange={(e) =>
                        onChangeNode(node.id, { timezone: e.target.value })
                      }
                    />
                  </Field>
                </Section>
              )}

              {node.type === "wait_for_event" && (
                <Section title="Wait for event">
                  <Field label="Event name" fieldKey="event_name">
                    <input
                      data-testid="inspector-wait-event-name"
                      className={inputClass}
                      value={
                        typeof node.event_name === "string"
                          ? node.event_name
                          : ""
                      }
                      onChange={(e) =>
                        onChangeNode(node.id, { event_name: e.target.value })
                      }
                    />
                  </Field>
                  <Field label="Correlation key" fieldKey="correlation_key">
                    <input
                      className={inputClass}
                      value={
                        typeof node.correlation_key === "string"
                          ? node.correlation_key
                          : ""
                      }
                      onChange={(e) =>
                        onChangeNode(node.id, {
                          correlation_key: e.target.value,
                        })
                      }
                    />
                  </Field>
                  <Field label="Timeout (seconds)" fieldKey="timeout_seconds">
                    <input
                      data-testid="inspector-wait-event-timeout"
                      className={inputClass}
                      type="number"
                      min={1}
                      max={2592000}
                      step={1}
                      value={
                        typeof node.timeout_seconds === "number"
                          ? node.timeout_seconds
                          : ""
                      }
                      onChange={(e) =>
                        onChangeNode(node.id, {
                          timeout_seconds:
                            e.target.value === ""
                              ? null
                              : Number(e.target.value) || null,
                        })
                      }
                      placeholder="Optional"
                    />
                  </Field>
                </Section>
              )}

              {node.type === "parallel" && (
                <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-3 text-xs text-zinc-600">
                  Fans out to all downstream edges in parallel.
                </div>
              )}

              {node.type === "join" && (
                <Section title="Join settings">
                  <Field label="Mode" fieldKey="mode">
                    <select
                      className={selectClass}
                      value={typeof node.mode === "string" ? node.mode : "all"}
                      onChange={(e) =>
                        onChangeNode(node.id, {
                          mode: e.target.value as "all" | "any",
                        })
                      }
                    >
                      <option value="all">Wait for all inputs</option>
                      <option value="any">First available input</option>
                    </select>
                  </Field>
                </Section>
              )}

              {node.type === "for_each" && (
                <Section title="For each settings">
                  <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2.5 text-xs text-zinc-500">
                    ForEach targets can be any executable node, including
                    agents, subworkflows, tool nodes, MCP resources, knowledge
                    queries, templates, Python code, and external apps.
                  </div>
                  <Field label="Target node" fieldKey="target_node_id">
                    <select
                      data-testid="inspector-for-each-target"
                      className={selectClass}
                      value={
                        typeof node.target_node_id === "string"
                          ? node.target_node_id
                          : ""
                      }
                      onChange={(e) =>
                        onChangeNode(node.id, {
                          target_node_id: e.target.value || null,
                        })
                      }
                    >
                      <option value="">(none)</option>
                      {forEachTargetOptions.map((option) => (
                        <option key={option.nodeId} value={option.nodeId}>
                          {targetOptionLabel(manifest, option, {
                            unsupportedLabel: "unsupported target",
                          })}
                        </option>
                      ))}
                    </select>
                  </Field>
                  <Field label="Items input port" fieldKey="item_input_port">
                    <input
                      className={inputClass}
                      value={
                        typeof node.item_input_port === "string"
                          ? node.item_input_port
                          : "items"
                      }
                      onChange={(e) =>
                        onChangeNode(node.id, {
                          item_input_port: e.target.value,
                        })
                      }
                    />
                  </Field>
                  <Field label="Max items" fieldKey="max_items">
                    <input
                      className={inputClass}
                      type="number"
                      min={1}
                      max={10000}
                      value={
                        typeof node.max_items === "number"
                          ? node.max_items
                          : 100
                      }
                      onChange={(e) =>
                        onChangeNode(node.id, {
                          max_items: Number(e.target.value) || 1,
                        })
                      }
                    />
                  </Field>
                </Section>
              )}

              {node.type === "loop" && (
                <Section title="Loop settings">
                  <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2.5 text-xs text-zinc-500">
                    Loop targets can be any executable node. The stop condition
                    runs after each iteration and can reference{" "}
                    <span className="font-mono">
                      iteration, state, output, result
                    </span>
                    , and <span className="font-mono">outputs</span>.
                  </div>
                  <Field label="Target node" fieldKey="target_node_id">
                    <select
                      data-testid="inspector-loop-target"
                      className={selectClass}
                      value={
                        typeof node.target_node_id === "string"
                          ? node.target_node_id
                          : ""
                      }
                      onChange={(e) =>
                        onChangeNode(node.id, {
                          target_node_id: e.target.value || null,
                        })
                      }
                    >
                      <option value="">(none)</option>
                      {loopTargetOptions.map((option) => (
                        <option key={option.nodeId} value={option.nodeId}>
                          {targetOptionLabel(manifest, option, {
                            unsupportedLabel: "unsupported target",
                          })}
                        </option>
                      ))}
                    </select>
                  </Field>
                  <Field label="Max iterations" fieldKey="max_iterations">
                    <input
                      data-testid="inspector-loop-max-iterations"
                      className={inputClass}
                      type="number"
                      min={1}
                      max={10000}
                      value={
                        typeof node.max_iterations === "number"
                          ? node.max_iterations
                          : 10
                      }
                      onChange={(e) =>
                        onChangeNode(node.id, {
                          max_iterations: Number(e.target.value) || 1,
                        })
                      }
                    />
                  </Field>
                  <Field label="Stop condition" fieldKey="stop_condition">
                    <input
                      data-testid="inspector-loop-stop-condition"
                      className={inputClass}
                      value={
                        typeof node.stop_condition === "string"
                          ? node.stop_condition
                          : ""
                      }
                      onChange={(e) =>
                        onChangeNode(node.id, {
                          stop_condition: e.target.value,
                        })
                      }
                      placeholder="state.done or iteration >= 3"
                    />
                  </Field>
                </Section>
              )}

              {node.type === "error_boundary" && (
                <Section title="Error boundary">
                  <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2.5 text-xs text-zinc-500">
                    Error boundaries can guard any executable node and
                    optionally compensate with another executable node when the
                    target fails.
                  </div>
                  <Field label="Target node" fieldKey="target_node_id">
                    <select
                      data-testid="inspector-error-boundary-target"
                      className={selectClass}
                      value={
                        typeof node.target_node_id === "string"
                          ? node.target_node_id
                          : ""
                      }
                      onChange={(e) =>
                        onChangeNode(node.id, {
                          target_node_id: e.target.value || null,
                        })
                      }
                    >
                      <option value="">(none)</option>
                      {errorBoundaryTargetOptions.map((option) => (
                        <option key={option.nodeId} value={option.nodeId}>
                          {targetOptionLabel(manifest, option, {
                            unsupportedLabel: "unsupported target",
                          })}
                        </option>
                      ))}
                    </select>
                  </Field>
                  <Field label="Compensate with" fieldKey="compensate_with">
                    <select
                      data-testid="inspector-error-boundary-compensate"
                      className={selectClass}
                      value={
                        typeof node.compensate_with === "string"
                          ? node.compensate_with
                          : ""
                      }
                      onChange={(e) =>
                        onChangeNode(node.id, {
                          compensate_with: e.target.value || null,
                        })
                      }
                    >
                      <option value="">(none)</option>
                      {errorBoundaryCompensationOptions.map((option) => (
                        <option key={option.nodeId} value={option.nodeId}>
                          {targetOptionLabel(manifest, option, {
                            unsupportedLabel: "unsupported compensation",
                          })}
                        </option>
                      ))}
                    </select>
                  </Field>
                  <Field label="Fallback text" fieldKey="fallback_text">
                    <textarea
                      className={textareaClass}
                      rows={3}
                      value={
                        typeof node.fallback_text === "string"
                          ? node.fallback_text
                          : ""
                      }
                      onChange={(e) =>
                        onChangeNode(node.id, { fallback_text: e.target.value })
                      }
                    />
                  </Field>
                </Section>
              )}

              {node.type === "subworkflow" && (
                <SubworkflowSection
                  manifest={manifest}
                  node={node}
                  onChangeNode={onChangeNode}
                />
              )}

              {node.type === "tool" && (
                <Section title="Tool">
                  <Field label="Binding" fieldKey="tool_name">
                    <select
                      data-testid="inspector-tool-node-name"
                      className={selectClass}
                      value={selectedToolBindingName}
                      onChange={(e) =>
                        onChangeNode(node.id, { tool_name: e.target.value })
                      }
                    >
                      <option value="">Select a tool binding</option>
                      {tools.length > 0 && (
                        <optgroup label="Registered tools">
                          {tools.map((tool) => (
                            <option key={tool.name} value={tool.name}>
                              {tool.name}
                            </option>
                          ))}
                        </optgroup>
                      )}
                      {activeMcpServers.length > 0 && (
                        <optgroup label="MCP tools">
                          {activeMcpServers.flatMap((server) =>
                            server.discovered_tools.map((toolDef) => {
                              const value = `mcp:${server.name}/${toolDef.name}`;
                              return (
                                <option key={value} value={value}>
                                  {server.name} / {toolDef.name}
                                </option>
                              );
                            }),
                          )}
                        </optgroup>
                      )}
                      {selectedToolBindingName &&
                        !toolPickerValueSet.has(selectedToolBindingName) && (
                          <option value={selectedToolBindingName}>
                            {selectedToolBindingName}
                          </option>
                        )}
                    </select>
                  </Field>
                  {selectedDirectToolDefinition ? (
                    <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2.5 text-xs text-zinc-600">
                      Calls{" "}
                      <span className="font-mono">
                        {selectedDirectToolDefinition.name}
                      </span>{" "}
                      directly. Side effect level:{" "}
                      <span className="font-medium">
                        {SIDE_EFFECT_LABEL[
                          selectedDirectToolDefinition.side_effect_level
                        ] ?? selectedDirectToolDefinition.side_effect_level}
                      </span>
                      {selectedDirectToolDefinition.requires_approval
                        ? " · marked as approval-sensitive"
                        : ""}
                    </div>
                  ) : selectedDirectMcpToolLabel ? (
                    <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2.5 text-xs text-zinc-600">
                      Calls{" "}
                      <span className="font-mono">
                        {selectedDirectMcpToolLabel}
                      </span>{" "}
                      through an MCP-backed manifest binding.
                    </div>
                  ) : (
                    <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2.5 text-xs text-zinc-500">
                      Pick a registered tool or MCP tool to invoke directly
                      without an agent step.
                    </div>
                  )}
                </Section>
              )}

              {node.type === "mcp_resource" && (
                <Section title="MCP resource">
                  <Field label="Server" fieldKey="server_id">
                    <select
                      data-testid="inspector-mcp-server"
                      className={selectClass}
                      value={selectedMcpServerId}
                      onChange={(e) => {
                        const serverId = e.target.value;
                        const server = activeMcpServers.find(
                          (item) => item.server_id === serverId,
                        );
                        const firstTool =
                          server?.discovered_tools[0]?.name ?? "";
                        onChangeNode(node.id, {
                          server_id: serverId,
                          tool_name: firstTool,
                        });
                      }}
                    >
                      <option value="">Select an MCP server</option>
                      {activeMcpServers.map((server) => (
                        <option key={server.server_id} value={server.server_id}>
                          {server.name}
                        </option>
                      ))}
                    </select>
                  </Field>
                  <Field label="Tool" fieldKey="tool_name">
                    <select
                      data-testid="inspector-mcp-tool"
                      className={selectClass}
                      value={
                        typeof node.tool_name === "string" ? node.tool_name : ""
                      }
                      onChange={(e) =>
                        onChangeNode(node.id, { tool_name: e.target.value })
                      }
                      disabled={!selectedMcpServer}
                    >
                      <option value="">
                        {selectedMcpServer
                          ? "Select a tool"
                          : "Select a server first"}
                      </option>
                      {(selectedMcpServer?.discovered_tools ?? []).map(
                        (toolDef) => (
                          <option key={toolDef.name} value={toolDef.name}>
                            {toolDef.name}
                          </option>
                        ),
                      )}
                    </select>
                  </Field>
                  <Field label="Timeout (seconds)" fieldKey="timeout_seconds">
                    <input
                      data-testid="inspector-mcp-timeout"
                      className={inputClass}
                      type="number"
                      min={1}
                      max={600}
                      step={1}
                      value={
                        typeof node.timeout_seconds === "number"
                          ? node.timeout_seconds
                          : 45
                      }
                      onChange={(e) =>
                        onChangeNode(node.id, {
                          timeout_seconds: Number(e.target.value) || 45,
                        })
                      }
                    />
                  </Field>
                  {activeMcpServers.length === 0 && (
                    <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2.5 text-xs text-zinc-500">
                      No active MCP servers are available.
                    </div>
                  )}
                </Section>
              )}

              {node.type === "knowledge_query" && (
                <KnowledgeQuerySection
                  node={node}
                  onChangeNode={onChangeNode}
                />
              )}

              {node.type === "knowledge_build" && (
                <KnowledgeBuildSection
                  node={node}
                  onChangeNode={onChangeNode}
                />
              )}

              {node.type === "template" && (
                <TemplateSection node={node} onChangeNode={onChangeNode} />
              )}

              {node.type === "external_app" && (
                <ExternalAppSection node={node} onChangeNode={onChangeNode} />
              )}

              {node.type === "webhook" && (
                <WebhookSection node={node} onChangeNode={onChangeNode} />
              )}

              {node.type === "api_request" && (
                <ApiRequestSection node={node} onChangeNode={onChangeNode} />
              )}

              {node.type === "python_code" && (
                <Section title="Python code">
                  <Field label="Code" fieldKey="code">
                    <textarea
                      data-testid="inspector-python-code"
                      className={textareaClass}
                      rows={10}
                      value={typeof node.code === "string" ? node.code : ""}
                      onChange={(e) =>
                        onChangeNode(node.id, { code: e.target.value })
                      }
                    />
                  </Field>
                  <Field label="Timeout (seconds)" fieldKey="timeout_seconds">
                    <input
                      data-testid="inspector-python-timeout"
                      className={inputClass}
                      type="number"
                      min={1}
                      max={120}
                      step={1}
                      value={
                        typeof node.timeout_seconds === "number"
                          ? node.timeout_seconds
                          : 5
                      }
                      onChange={(e) =>
                        onChangeNode(node.id, {
                          timeout_seconds: Number(e.target.value) || 5,
                        })
                      }
                    />
                  </Field>
                </Section>
              )}

              {node.type === "guardrail" && (
                <Section title="Guardrail settings">
                  <Field label="Mode" fieldKey="mode">
                    <select
                      data-testid="inspector-mode"
                      className={selectClass}
                      value={node.mode ?? "post_agent"}
                      onChange={(e) =>
                        onChangeNode(node.id, {
                          mode: e.target.value as ManifestNode["mode"],
                        })
                      }
                    >
                      <option value="pre_agent">Pre-Agent</option>
                      <option value="post_agent">Post-Agent</option>
                    </select>
                  </Field>
                  <Field label="On failure" fieldKey="on_failure">
                    <select
                      data-testid="inspector-on-failure"
                      className={selectClass}
                      value={node.on_failure ?? "block"}
                      onChange={(e) =>
                        onChangeNode(node.id, {
                          on_failure: e.target
                            .value as ManifestNode["on_failure"],
                        })
                      }
                    >
                      <option value="block">Block</option>
                      <option value="block_retry">Block + Retry</option>
                      <option value="warn">Warn + Continue</option>
                      <option value="redact">Redact</option>
                      <option value="escalate">Escalate</option>
                    </select>
                  </Field>
                  <Field label="Retry attempts" fieldKey="max_retries">
                    <input
                      data-testid="inspector-guardrail-max-retries"
                      className={inputClass}
                      type="number"
                      min={0}
                      max={10}
                      step={1}
                      value={
                        typeof node.max_retries === "number"
                          ? node.max_retries
                          : 0
                      }
                      onChange={(e) =>
                        onChangeNode(node.id, {
                          max_retries: Number(e.target.value) || 0,
                        })
                      }
                      disabled={(node.on_failure ?? "block") !== "block_retry"}
                    />
                  </Field>
                  <Field label="Checks" fieldKey="checks">
                    <GuardrailChecksEditor
                      checks={
                        (node.checks ?? []) as Array<Record<string, unknown>>
                      }
                      onChange={(checks) => onChangeNode(node.id, { checks })}
                    />
                  </Field>
                </Section>
              )}

              {node.type === "router" && (
                <Section title="Routing conditions">
                  <Field label="Branches" fieldKey="branches">
                    <RouterConditionBuilder
                      branches={(node.branches ?? []) as RouterBranch[]}
                      nodeIds={routerTargets(manifest.nodes)}
                      onChange={(branches) =>
                        onChangeNode(node.id, { branches })
                      }
                    />
                  </Field>
                </Section>
              )}

              {node.type === "human_approval" && (
                <Section title="Human approval">
                  <Field label="Required role" fieldKey="required_role">
                    <input
                      data-testid="inspector-approval-role"
                      className={inputClass}
                      value={
                        typeof node.required_role === "string"
                          ? node.required_role
                          : "caliber.approver"
                      }
                      onChange={(e) =>
                        onChangeNode(node.id, { required_role: e.target.value })
                      }
                    />
                  </Field>
                  <Field label="Approval count" fieldKey="approval_count">
                    <input
                      data-testid="inspector-approval-count"
                      className={inputClass}
                      type="number"
                      min={1}
                      max={10}
                      step={1}
                      value={
                        typeof node.approval_count === "number"
                          ? node.approval_count
                          : 1
                      }
                      onChange={(e) =>
                        onChangeNode(node.id, {
                          approval_count: Number(e.target.value) || 1,
                        })
                      }
                    />
                  </Field>
                  <Field label="Timeout behavior" fieldKey="timeout_behavior">
                    <select
                      data-testid="inspector-approval-timeout"
                      className={selectClass}
                      value={
                        typeof node.timeout_behavior === "string"
                          ? node.timeout_behavior
                          : "block"
                      }
                      onChange={(e) =>
                        onChangeNode(node.id, {
                          timeout_behavior: e.target.value as
                            | "block"
                            | "escalate"
                            | "auto_reject",
                        })
                      }
                    >
                      <option value="block">Block until decision</option>
                      <option value="escalate">Escalate on timeout</option>
                      <option value="auto_reject">
                        Auto-reject on timeout
                      </option>
                    </select>
                  </Field>
                  <div className="rounded-lg border border-violet-200 bg-violet-50 px-3 py-3 text-xs leading-relaxed text-violet-700">
                    Runtime approval records inherit this policy. When more than
                    one approval is required, the run remains blocked until
                    every pending approval decision for this node has been
                    recorded.
                  </div>
                </Section>
              )}

              {node.type === "note" && (
                <Section title="Note">
                  <Field label="Note" fieldKey="text">
                    <textarea
                      data-testid="inspector-note-text"
                      className={textareaClass}
                      rows={4}
                      value={node.text ?? ""}
                      onChange={(e) =>
                        onChangeNode(node.id, { text: e.target.value })
                      }
                    />
                  </Field>
                </Section>
              )}

              {node.type !== "start" && node.type !== "output" && (
                <Section title="Execution Policy">
                  <Field label="Timeout (seconds)" fieldKey="execution_policy">
                    <input
                      className={inputClass}
                      type="number"
                      min={0}
                      step={1}
                      value={
                        typeof node.execution_policy?.timeout_seconds ===
                        "number"
                          ? node.execution_policy.timeout_seconds
                          : ""
                      }
                      onChange={(e) => {
                        const raw = e.target.value.trim();
                        const timeout = raw === "" ? null : Number(raw);
                        onChangeNode(node.id, {
                          execution_policy: {
                            timeout_seconds:
                              timeout == null ||
                              Number.isNaN(timeout) ||
                              timeout <= 0
                                ? null
                                : timeout,
                            max_retries:
                              typeof node.execution_policy?.max_retries ===
                              "number"
                                ? node.execution_policy.max_retries
                                : 0,
                            idempotent: Boolean(
                              node.execution_policy?.idempotent,
                            ),
                          },
                        });
                      }}
                    />
                  </Field>
                  <Field label="Max retries">
                    <input
                      className={inputClass}
                      type="number"
                      min={0}
                      max={10}
                      value={
                        typeof node.execution_policy?.max_retries === "number"
                          ? node.execution_policy.max_retries
                          : 0
                      }
                      onChange={(e) =>
                        onChangeNode(node.id, {
                          execution_policy: {
                            timeout_seconds:
                              typeof node.execution_policy?.timeout_seconds ===
                              "number"
                                ? node.execution_policy.timeout_seconds
                                : null,
                            max_retries: Number(e.target.value) || 0,
                            idempotent: Boolean(
                              node.execution_policy?.idempotent,
                            ),
                          },
                        })
                      }
                    />
                  </Field>
                  <label className="flex items-center gap-2 rounded-lg border border-zinc-200 bg-white px-3 py-2 text-xs text-zinc-700">
                    <input
                      type="checkbox"
                      className="rounded border-zinc-300"
                      checked={Boolean(node.execution_policy?.idempotent)}
                      onChange={(e) =>
                        onChangeNode(node.id, {
                          execution_policy: {
                            timeout_seconds:
                              typeof node.execution_policy?.timeout_seconds ===
                              "number"
                                ? node.execution_policy.timeout_seconds
                                : null,
                            max_retries:
                              typeof node.execution_policy?.max_retries ===
                              "number"
                                ? node.execution_policy.max_retries
                                : 0,
                            idempotent: e.target.checked,
                          },
                        })
                      }
                    />
                    <span className="font-medium">Idempotent execution</span>
                  </label>
                </Section>
              )}
            </div>
          </COMPONENT_SHOW_ADVANCED_CONTEXT.Provider>
        </COMPONENT_FIELD_HIGHLIGHT_CONTEXT.Provider>
      </COMPONENT_FIELD_FEEDBACK_CONTEXT.Provider>
    </COMPONENT_FIELD_CONTEXT.Provider>
  );
}

function StartTriggerSection({
  workflowId,
  nodeId,
  trigger,
  onChangeNode,
}: {
  workflowId: string;
  nodeId: string;
  trigger: StartTriggerConfig | null;
  onChangeNode: (nodeId: string, patch: Partial<ManifestNode>) => void;
}): JSX.Element {
  const mode: StartTriggerMode = trigger?.mode ?? "manual";
  const alias = trigger?.alias ?? "prod";
  const trimmedAlias = alias.trim();
  const deploymentsQuery = useApiQuery<WorkflowDeployment[]>(
    ["workflow-editor", workflowId, "deployments"],
    (signal) => caliberApi.listWorkflowDeployments(workflowId, signal),
    { enabled: Boolean(workflowId) && mode !== "manual" },
  );
  const activeAliases = uniqueTrimmedStrings(
    (deploymentsQuery.data ?? [])
      .filter((deployment) => deployment.status === "active")
      .map((deployment) => deployment.alias),
  );
  const aliasChoices = uniqueTrimmedStrings([
    ...START_TRIGGER_ALIAS_HINTS,
    ...activeAliases,
    alias,
  ]);
  const cronExpr = (trigger?.cron ?? "").trim();
  const cronTz = (trigger?.timezone ?? "UTC").trim() || "UTC";
  const cronPreviewQuery = useApiQuery<WorkflowCronPreview>(
    ["workflow-editor", "cron-preview", cronExpr, cronTz],
    (signal) => caliberApi.previewWorkflowCron(cronExpr, cronTz, 5, signal),
    { enabled: mode === "cron" && cronExpr.length > 0 },
  );
  const patch = (next: Partial<StartTriggerConfig>): void =>
    onChangeNode(nodeId, {
      trigger: { mode, ...(trigger ?? {}), ...next } as StartTriggerConfig,
    });

  const setMode = (next: StartTriggerMode): void => {
    if (next === "manual") {
      onChangeNode(nodeId, { trigger: null });
      return;
    }
    if (next === "event") {
      onChangeNode(nodeId, {
        trigger: {
          mode: "event",
          event_name: trigger?.event_name ?? "",
          alias: trigger?.alias ?? "prod",
          enabled: trigger?.enabled ?? true,
        },
      });
      return;
    }
    onChangeNode(nodeId, {
      trigger: {
        mode: "cron",
        cron: trigger?.cron ?? "0 9 * * *",
        timezone: trigger?.timezone ?? "UTC",
        alias: trigger?.alias ?? "prod",
        enabled: trigger?.enabled ?? true,
      },
    });
  };

  let aliasHelperText = `This ${mode} trigger targets the workflow deployment alias that should receive new runs.`;
  let aliasHelperToneClass = "text-zinc-500";
  if (!workflowId) {
    aliasHelperText =
      "Save this workflow before checking which deployment aliases are active.";
  } else if (deploymentsQuery.isLoading) {
    aliasHelperText = "Loading deployment aliases…";
  } else if (deploymentsQuery.error) {
    aliasHelperText =
      "Deployment aliases could not be loaded right now. You can still type one manually.";
    aliasHelperToneClass = "text-amber-600";
  } else if (!trimmedAlias) {
    aliasHelperText = "Set the deployment alias this trigger should target.";
  } else if (activeAliases.includes(trimmedAlias)) {
    aliasHelperText = `This ${mode} trigger targets active deployment alias ${trimmedAlias}.`;
  } else if (activeAliases.length === 0) {
    aliasHelperText = `No active deployments exist for this workflow yet. ${mode === "event" ? "Event calls" : "Scheduled runs"} will fail until alias ${trimmedAlias} is deployed.`;
    aliasHelperToneClass = "text-amber-600";
  } else {
    aliasHelperText = `Alias ${trimmedAlias} is not currently active. Active aliases: ${activeAliases.join(", ")}.`;
    aliasHelperToneClass = "text-amber-600";
  }

  return (
    <Section title="Trigger">
      <Field label="Start mode" fieldKey="trigger">
        <select
          data-testid="inspector-start-mode"
          aria-label="Start mode"
          className={selectClass}
          value={mode}
          onChange={(e) => setMode(e.target.value as StartTriggerMode)}
        >
          <option value="manual">Manual (run on demand)</option>
          <option value="event">Event (external trigger)</option>
          <option value="cron">Cron schedule</option>
        </select>
      </Field>

      {mode === "event" && (
        <Field label="Event name">
          <input
            data-testid="inspector-start-event-name"
            className={inputClass}
            placeholder="e.g. order_created"
            value={trigger?.event_name ?? ""}
            onChange={(e) => patch({ event_name: e.target.value })}
          />
        </Field>
      )}

      {mode === "cron" && (
        <>
          <Field label="Cron expression">
            <input
              data-testid="inspector-start-cron"
              className={inputClass}
              placeholder="*/15 * * * *  (min hour dom mon dow)"
              value={trigger?.cron ?? ""}
              onChange={(e) => patch({ cron: e.target.value })}
            />
          </Field>
          <Field label="Timezone">
            <input
              className={inputClass}
              placeholder="UTC"
              value={trigger?.timezone ?? "UTC"}
              onChange={(e) => patch({ timezone: e.target.value })}
            />
          </Field>
          {cronExpr.length > 0 && (
            <div
              data-testid="inspector-start-cron-preview"
              className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2 text-[11px] text-zinc-600"
            >
              <div className="mb-1 font-semibold uppercase tracking-wider text-zinc-400">
                Next runs
              </div>
              {cronPreviewQuery.isLoading && <div>Calculating next runs…</div>}
              {cronPreviewQuery.error && (
                <div
                  data-testid="inspector-start-cron-preview-error"
                  className="text-amber-600"
                >
                  {cronPreviewQuery.error.message ||
                    "This cron expression or timezone isn't valid yet."}
                </div>
              )}
              {!cronPreviewQuery.isLoading &&
                !cronPreviewQuery.error &&
                (cronPreviewQuery.data?.fire_times.length ? (
                  <ul className="space-y-0.5">
                    {cronPreviewQuery.data.fire_times.map((ts) => (
                      <li
                        key={ts}
                        data-testid="inspector-start-cron-fire-time"
                        className="font-mono tabular-nums text-zinc-700"
                      >
                        {ts.replace("T", " ").slice(0, 16)}
                      </li>
                    ))}
                    <li className="pt-0.5 text-zinc-400">
                      Times shown in {cronPreviewQuery.data.timezone}.
                    </li>
                  </ul>
                ) : (
                  <div>
                    This expression has no upcoming runs in the next year.
                  </div>
                ))}
            </div>
          )}
        </>
      )}

      {mode !== "manual" && (
        <>
          <Field label="Target deployment">
            <>
              <input
                data-testid="inspector-start-alias"
                aria-label="Target deployment"
                className={inputClass}
                value={alias}
                onChange={(e) => patch({ alias: e.target.value })}
              />
              <div className="mt-2 flex flex-wrap gap-2">
                {aliasChoices.map((choice) => (
                  <button
                    key={choice}
                    type="button"
                    data-testid={startTriggerAliasTestId(choice)}
                    onClick={() => patch({ alias: choice })}
                    className={`rounded-full border px-2.5 py-1 text-[11px] font-semibold transition ${
                      choice === trimmedAlias
                        ? "border-zinc-900 bg-zinc-900 text-white"
                        : "border-zinc-200 bg-white text-zinc-600 hover:border-zinc-300 hover:text-zinc-900"
                    }`}
                  >
                    {choice}
                  </button>
                ))}
              </div>
              <div
                className={`mt-2 text-[11px] leading-relaxed ${aliasHelperToneClass}`}
              >
                {aliasHelperText}
              </div>
            </>
          </Field>
          <label className="flex items-center gap-2 rounded-lg border border-zinc-200 bg-white px-3 py-2 text-xs text-zinc-700">
            <input
              type="checkbox"
              className="rounded border-zinc-300"
              checked={trigger?.enabled !== false}
              onChange={(e) => patch({ enabled: e.target.checked })}
            />
            <span className="font-medium">Enabled</span>
          </label>
        </>
      )}

      <p className="text-[11px] leading-relaxed text-zinc-500">
        {mode === "manual" && "Runs start on demand from the UI or API."}
        {mode === "event" &&
          "An external caller starts a run by POSTing to this workflow's trigger endpoint."}
        {mode === "cron" &&
          "The scheduler starts a run of the deployed version when the cron fires (requires the run queue + scheduler enabled)."}
      </p>
    </Section>
  );
}

/** Generic per-node metadata: a display name + an author note (Markdown). */
function NodeMetaSection({
  node,
  onChangeNode,
}: {
  node: ManifestNode;
  onChangeNode: (nodeId: string, patch: Partial<ManifestNode>) => void;
}): JSX.Element {
  return (
    <Section title="Node">
      <label className="block space-y-1">
        <span className="text-xs font-medium text-zinc-600">Display name</span>
        <input
          data-testid="inspector-node-label"
          className={inputClass}
          value={typeof node.label === "string" ? node.label : ""}
          placeholder={node.id}
          onChange={(e) =>
            onChangeNode(node.id, { label: e.target.value || undefined })
          }
        />
      </label>
      <label className="block space-y-1">
        <span className="text-xs font-medium text-zinc-600">Description</span>
        <textarea
          data-testid="inspector-node-description"
          className={textareaClass}
          rows={2}
          value={typeof node.description === "string" ? node.description : ""}
          placeholder="Optional note for collaborators (Markdown supported)"
          onChange={(e) =>
            onChangeNode(node.id, { description: e.target.value || undefined })
          }
        />
      </label>
      <div className="text-[10px] text-zinc-400">
        ID <span className="font-mono">{node.id}</span> — referenced by
        connections; edit via the node&apos;s code (
        <span className="font-mono">&lt;&gt;</span>) view.
      </div>
    </Section>
  );
}

/** The selected node's most recent preview/run step — status, output, logs. */
function NodeOutputSection({ step }: { step: PreviewStep }): JSX.Element {
  const statusClass =
    step.status === "error"
      ? "bg-red-100 text-red-700"
      : step.status === "ok" || step.status === "completed"
        ? "bg-emerald-100 text-emerald-700"
        : "bg-zinc-100 text-zinc-600";
  return (
    <Section title="Last output">
      <div
        data-testid="inspector-node-output"
        className="space-y-2 rounded-lg border border-zinc-200 bg-zinc-50 p-3"
      >
        <div className="flex items-center justify-between gap-2 text-xs">
          <span
            className={`rounded-full px-2 py-0.5 text-[10px] font-semibold capitalize ${statusClass}`}
          >
            {step.status || "—"}
          </span>
          {typeof step.duration_ms === "number" && (
            <span className="text-zinc-400">{step.duration_ms} ms</span>
          )}
        </div>
        {step.output && (
          <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words rounded bg-white p-2 text-[11px] leading-relaxed text-zinc-700">
            {step.output}
          </pre>
        )}
        {step.detail && (
          <div className="text-[11px] text-zinc-500">{step.detail}</div>
        )}
        {step.tool_calls.length > 0 && (
          <div className="text-[11px] text-zinc-500">
            {step.tool_calls.length} tool call
            {step.tool_calls.length === 1 ? "" : "s"}
          </div>
        )}
      </div>
    </Section>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}): JSX.Element {
  return (
    <div className="space-y-2">
      <h4 className="text-[11px] font-semibold uppercase tracking-wider text-zinc-400">
        {title}
      </h4>
      {children}
    </div>
  );
}

function Field({
  label,
  fieldKey,
  children,
}: {
  label: string;
  fieldKey?: string;
  children: React.ReactNode;
}): JSX.Element {
  const meta = useComponentFieldMeta(fieldKey);
  const feedback = useComponentFieldFeedback(fieldKey);
  const showAdvanced = useShowAdvanced();
  const highlightedFieldKey = useHighlightedFieldKey();
  const isHighlighted = Boolean(fieldKey && highlightedFieldKey === fieldKey);
  // Collapse advanced fields unless revealed — but never hide one that has an
  // active issue or is the focus target, so problems stay reachable.
  if (
    meta?.advanced &&
    !showAdvanced &&
    !isHighlighted &&
    feedback.issues.length === 0
  ) {
    return <></>;
  }
  const errors = feedback.issues.filter((issue) => issue.severity === "error");
  const warnings = feedback.issues.filter(
    (issue) => issue.severity !== "error",
  );
  const metaTokens = meta ? fieldMetaTokens(meta) : [];
  return (
    <label
      data-testid={fieldKey ? `inspector-field-${fieldKey}` : undefined}
      data-workflow-field-key={fieldKey}
      data-highlighted={isHighlighted ? "true" : "false"}
      className={`block rounded-xl transition-all ${
        isHighlighted
          ? "bg-caliber-50/70 ring-2 ring-caliber-300 ring-offset-2 ring-offset-white"
          : ""
      }`}
    >
      <span className="flex flex-wrap items-center gap-2 text-xs font-medium text-zinc-600">
        <span>{label}</span>
        {meta && (
          <span className="rounded-full bg-zinc-100 px-2 py-0.5 text-[10px] font-semibold text-zinc-500">
            {meta.type}
          </span>
        )}
        {meta && (
          <span
            className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${
              meta.required
                ? "bg-amber-50 text-amber-700"
                : "bg-emerald-50 text-emerald-700"
            }`}
          >
            {meta.required ? "Required" : "Optional"}
          </span>
        )}
        {errors.length > 0 && (
          <span className="rounded-full bg-red-100 px-2 py-0.5 text-[10px] font-semibold text-red-700">
            {errors.length} issue{errors.length === 1 ? "" : "s"}
          </span>
        )}
        {errors.length === 0 && warnings.length > 0 && (
          <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-semibold text-amber-700">
            {warnings.length} warning{warnings.length === 1 ? "" : "s"}
          </span>
        )}
        {feedback.issues.length === 0 && feedback.setupChecks.length > 0 && (
          <span className="rounded-full bg-amber-50 px-2 py-0.5 text-[10px] font-semibold text-amber-700">
            Needs setup
          </span>
        )}
      </span>
      <div className="mt-1">{children}</div>
      {meta?.description && (
        <div className="mt-1 text-[11px] leading-relaxed text-zinc-500">
          {meta.description}
        </div>
      )}
      {metaTokens.length > 0 && (
        <div
          data-testid={
            fieldKey ? `inspector-field-meta-${fieldKey}` : undefined
          }
          className="mt-1.5 flex flex-wrap gap-2 text-[10px]"
        >
          {metaTokens.map((token) => (
            <span
              key={`${fieldKey ?? label}-${token.label}`}
              className={`rounded-full border px-2 py-0.5 font-medium ${
                token.tone === "info"
                  ? "border-sky-200 bg-sky-50 text-sky-700"
                  : "border-zinc-200 bg-zinc-50 text-zinc-600"
              }`}
            >
              {token.label}
            </span>
          ))}
        </div>
      )}
      {feedback.issues.length > 0 && (
        <div
          data-testid={
            fieldKey ? `inspector-field-issues-${fieldKey}` : undefined
          }
          className={`mt-2 rounded-lg border px-3 py-2 text-[11px] ${
            errors.length > 0
              ? "border-red-200 bg-red-50 text-red-700"
              : "border-amber-200 bg-amber-50 text-amber-700"
          }`}
        >
          {feedback.issues.map((issue) => (
            <div
              key={`${issue.code}-${issue.path}`}
              className="py-0.5 leading-relaxed"
            >
              {issue.message}
            </div>
          ))}
        </div>
      )}
      {feedback.issues.length === 0 && feedback.setupChecks.length > 0 && (
        <div
          data-testid={
            fieldKey ? `inspector-field-setup-${fieldKey}` : undefined
          }
          className="mt-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-700"
        >
          {feedback.setupChecks.map((check) => (
            <div key={check.label} className="py-0.5 leading-relaxed">
              <span className="font-semibold">{check.label}.</span> {check.help}
            </div>
          ))}
        </div>
      )}
    </label>
  );
}
