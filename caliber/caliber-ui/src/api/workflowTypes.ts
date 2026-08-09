/**
 * TypeScript types for the Workflow Studio API surface.
 *
 * Kept in a dedicated module (rather than the large `types.ts`) so the
 * workflow-builder types stay easy to find. The manifest is intentionally
 * typed loosely (`WorkflowManifest`) — the backend Pydantic schema is the
 * authoritative validator; the editor manipulates a structured-but-open shape.
 */

import type { RefinementJob, VerificationItem } from "./types";
import type {
  KnowledgeGraphConfig,
  KnowledgeQueryGraphOverrides,
  KnowledgeRetrievalMode,
} from "./knowledgeTypes";

export type DataType =
  | "string"
  | "structured"
  | "messages"
  | "boolean"
  | "void";
export type GuardrailFailureMode =
  | "block"
  | "block_retry"
  | "warn"
  | "redact"
  | "escalate";

export type WorkflowNodeType =
  | "start"
  | "file_input"
  | "folder_input"
  | "input_bucket"
  | "output_bucket"
  | "output_folder"
  | "wait_until"
  | "wait_for_event"
  | "parallel"
  | "join"
  | "for_each"
  | "loop"
  | "error_boundary"
  | "subworkflow"
  | "tool"
  | "mcp_resource"
  | "knowledge_query"
  | "knowledge_build"
  | "template"
  | "data_transform"
  | "review_queue_enqueue"
  | "python_code"
  | "agent"
  | "guardrail"
  | "router"
  | "human_approval"
  | "output"
  | "note"
  | "external_app"
  | "webhook"
  | "api_request";

export interface PortSpec {
  type: DataType;
  description?: string;
  schema?: Record<string, unknown> | null;
}

export type StartTriggerMode = "manual" | "event" | "cron";

export interface StartTriggerConfig {
  mode: StartTriggerMode;
  event_name?: string;
  cron?: string;
  timezone?: string;
  alias?: string;
  enabled?: boolean;
}

/** Next fire-times preview for a cron Start trigger (GET /workflow-cron-preview). */
export interface WorkflowCronPreview {
  timezone: string;
  expression: string;
  /** ISO-8601 local datetimes (tz-naive), soonest first. */
  fire_times: string[];
}

export interface HandoffSpec {
  target: string;
  description?: string;
  input_filter?: string | null;
  condition?: string | null;
}

/** Immutable project-file snapshot embedded in a workflow version. */
export interface ManagedFileReference {
  file_id: string;
  file_ref: string;
  sha256: string;
  name: string;
  size_bytes: number;
  media_type: string | null;
  object_version_id: string | null;
}

export interface ManifestNode {
  id: string;
  type: WorkflowNodeType;
  /** Optional human-friendly display name (the id stays the stable identifier). */
  label?: string;
  /** Optional author note (may contain Markdown); presentation-only. */
  description?: string;
  name?: string;
  model?: string;
  instructions?:
    | { type: "inline"; text: string }
    | { type: "mlflow_prompt"; ref: string };
  tools?: string[];
  skills?: string[];
  tool_constraints?: Record<string, string>;
  output_type?: Record<string, unknown> | null;
  eval_dataset?: string | null;
  server_id?: string;
  tool_name?: string;
  entrypoint?: string;
  // Webhook + API Request (outbound HTTP) nodes
  url?: string;
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  headers?: Record<string, string>;
  // API Request node only
  curl?: string;
  body?: string;
  knowledge_base_id?: string;
  chunking_strategy?: string;
  embedding_model?: string;
  chunking_config?: Record<string, unknown>;
  graph_config?: KnowledgeGraphConfig | null;
  activate_when_complete?: boolean;
  wait_for_completion?: boolean;
  wait_timeout_seconds?: number;
  version_ids?: string[];
  retrieval_modes?: KnowledgeRetrievalMode[];
  top_k?: number;
  chat_model?: string | null;
  graph_overrides?: KnowledgeQueryGraphOverrides | null;
  template?: string;
  output_format?: "text" | "json";
  missing_variable_mode?: "preserve" | "empty" | "error";
  operation?: "fixture" | "mapping" | "json_schema" | "decision_table" | "confidence";
  config?: Record<string, unknown>;
  fail_on_invalid?: boolean;
  queue_id?: string;
  experiment_id?: string | null;
  assigned_to?: string | null;
  code?: string;
  timeout_seconds?: number | null;
  wait_until?: string;
  timezone?: string;
  event_name?: string;
  correlation_key?: string;
  mode?: "pre_agent" | "post_agent" | "all" | "any" | "url" | "curl";
  target_node_id?: string | null;
  item_input_port?: string;
  max_items?: number;
  max_iterations?: number;
  stop_condition?: string;
  fallback_text?: string;
  compensate_with?: string | null;
  workflow_id?: string;
  alias?: string;
  // Start node trigger (manual / event / cron)
  trigger?: StartTriggerConfig | null;
  // Bucket / folder I/O nodes
  file_ref?: ManagedFileReference | null;
  bucket?: string;
  prefix?: string;
  path?: string;
  recursive?: boolean;
  max_files?: number;
  max_bytes?: number;
  max_bytes_per_file?: number;
  encoding?: string;
  overwrite?: boolean;
  execution_policy?: {
    timeout_seconds?: number | null;
    max_retries?: number;
    idempotent?: boolean;
  } | null;
  handoffs?: HandoffSpec[];
  inputs?: Record<string, PortSpec>;
  outputs?: Record<string, PortSpec>;
  checks?: Array<Record<string, unknown>>;
  on_failure?: GuardrailFailureMode;
  max_retries?: number;
  branches?: Array<{ condition?: Record<string, unknown> | null; to: string }>;
  required_role?: string;
  approval_count?: number;
  timeout_behavior?: "block" | "escalate" | "auto_reject";
  text?: string;
  [key: string]: unknown;
}

export interface ManifestEdge {
  id: string;
  from: string;
  to: string;
  map: Record<string, string>;
}

export interface WorkflowRegisteredFunctionToolBinding {
  type?: "registered_function";
  registry_ref: string;
  version_constraint?: string;
  requires_approval?: boolean;
  secret_refs?: string[];
  timeout_seconds?: number | null;
  max_retries?: number;
}

export interface WorkflowMcpToolBinding {
  type: "mcp_tool";
  server_id: string;
  tool_name: string;
  tool_schema_version?: string;
  side_effect_level?: "read" | "write" | "external_action";
  requires_approval?: boolean;
  timeout_seconds?: number | null;
  max_retries?: number;
}

export type WorkflowToolBinding =
  | WorkflowRegisteredFunctionToolBinding
  | WorkflowMcpToolBinding;

export interface WorkflowDeployGate {
  type?: "deploy_gate";
  dataset_ref: string;
  required_for_aliases?: string[];
  /**
   * Keys the backend evaluates (see caliber.workflows.deploy_gate.
   * SUPPORTED_THRESHOLDS). An unrecognised key fails the gate closed rather than
   * being ignored, so this stays a free-form map on purpose: the server, not the
   * client, is the authority on what can be measured.
   */
  thresholds?: Record<string, number>;
  /** Deterministic scorers used to grade each replay against expected output. */
  scorers?: string[];
  /** Per-example score at or above which a replay counts as passing. */
  pass_threshold?: number;
}

export interface WorkflowMlflowConfig {
  experiment_name?: string | null;
  trace_group_tags?: Record<string, string>;
}

export interface WorkflowPromptArtifact {
  registry_name: string;
  alias?: string;
  managed_by?: string;
}

export interface WorkflowEvalDatasetArtifact {
  dataset_name: string;
  min_overall_delta?: number | null;
  max_tone_regression?: number | null;
}

export interface WorkflowArtifactsConfig {
  prompts?: Record<string, WorkflowPromptArtifact>;
  eval_datasets?: Record<string, WorkflowEvalDatasetArtifact>;
}

export type WorkflowSessionMode = "none" | "in_memory" | "persistent";
export type WorkflowOpenAIAPI = "chat_completions" | "responses" | "agents_sdk";
export type WorkflowOpenAIParallelToolCallsMode =
  | "auto"
  | "enabled"
  | "disabled";
export type WorkflowOpenAIPromptCacheMode = "auto" | "enabled" | "disabled";
export type WorkflowOpenAIPromptCacheRetention =
  | "default"
  | "in_memory"
  | "24h";

export interface WorkflowRuntimeSessionConfig {
  type?: WorkflowSessionMode;
}

export interface WorkflowRuntimeOpenAIConfig {
  workflow_api?: WorkflowOpenAIAPI | null;
  parallel_tool_calls?: WorkflowOpenAIParallelToolCallsMode | null;
  prompt_cache_mode?: WorkflowOpenAIPromptCacheMode | null;
  prompt_cache_retention?: WorkflowOpenAIPromptCacheRetention | null;
}

export interface WorkflowRuntimeConfig {
  sdk?: string;
  sdk_version_policy?: "runtime-pinned";
  compiler_version?: string;
  default_model_ref?: string;
  session?: WorkflowRuntimeSessionConfig;
  openai?: WorkflowRuntimeOpenAIConfig | null;
}

export interface WorkflowManifest {
  schema_version: number;
  workflow_id: string;
  name: string;
  description?: string;
  owner?: string;
  runtime?: WorkflowRuntimeConfig;
  mlflow?: WorkflowMlflowConfig;
  artifacts?: WorkflowArtifactsConfig;
  nodes: Record<string, ManifestNode>;
  edges: ManifestEdge[];
  tools?: Record<string, WorkflowToolBinding>;
  deploy_gates?: Record<string, WorkflowDeployGate>;
  [key: string]: unknown;
}

export interface Workflow {
  workflow_id: string;
  project_id: string | null;
  name: string;
  description: string;
  owner: string;
  status: "active" | "paused" | "archived";
  default_experiment_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface WorkflowVersion {
  version_id: string;
  workflow_id: string;
  version_number: number;
  status: "draft" | "published" | "deprecated";
  manifest: WorkflowManifest;
  manifest_hash: string;
  compiler_version: string | null;
  compiled_artifact_uri: string | null;
  compiled_bundle: {
    generated_python?: string;
    compiler_report?: Record<string, unknown>;
    requirements?: string[];
  } | null;
  validation_report: ValidationReport | null;
  created_by: string;
  created_at: string;
  published_by: string | null;
  published_at: string | null;
}

export interface WorkflowImportDependency {
  kind:
    | "prompt"
    | "eval_dataset"
    | "tool"
    | "mcp_tool"
    | "skill"
    | "knowledge_base"
    | "knowledge_base_version"
    | "subworkflow";
  reference: string;
  path: string;
  status: "resolved" | "unresolved" | "unverified";
  version: string | null;
  detail: string;
}

export interface WorkflowImportPreview {
  source_workflow_id: string;
  name: string;
  description: string;
  node_count: number;
  edge_count: number;
  validation: ValidationReport;
  dependencies: WorkflowImportDependency[];
  ready_to_import: boolean;
}

export interface WorkflowRun {
  workflow_run_id: string;
  workflow_id: string;
  project_id: string | null;
  tenant_id: string | null;
  workflow_version_id: string | null;
  deployment_alias: string | null;
  mlflow_run_id: string | null;
  trace_id: string | null;
  session_id: string | null;
  status: string;
  source: string | null;
  priority: number | null;
  queued_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  claimed_by?: string | null;
  claimed_at?: string | null;
  lease_expires_at?: string | null;
  last_heartbeat_at?: string | null;
  attempt_number?: number | null;
  parent_run_id?: string | null;
  cancel_requested_at?: string | null;
  cancel_requested_by?: string | null;
  cancel_reason?: string | null;
  current_node_id?: string | null;
  idempotency_key?: string | null;
  input_file_ref?: string | null;
  error_code?: string | null;
  error_summary?: string | null;
  summary: {
    output?: string;
    input?: string;
    tokens?: number;
    error?: string | null;
    preview?: boolean;
    status?: string;
    manifest_mode?: "saved_version" | "snapshot";
    manifest_hash?: string;
    workflow_version_number?: number;
    node_path?: string[];
    steps?: WorkflowRunStep[];
    logs?: Array<Record<string, unknown>>;
    tags?: Record<string, string>;
    guardrail_results?: Array<Record<string, unknown>>;
    retry_of?: string;
    retry_mode?: string;
    resume_checkpoint_id?: string;
    resume_checkpoint_run_id?: string;
    timeout_checkpoint_id?: string;
    artifact_persistence?: WorkflowRunArtifactPersistenceSummary | null;
  } | null;
}

export interface WorkflowRunArtifactPersistenceSummary {
  status: string;
  bucket: string;
  object_count: number;
  artifact_names: string[];
  error?: string;
  persisted_object_count?: number;
  recent_persisted_keys?: string[];
  failed_object_key?: string;
}

export interface WorkflowRunHistoryArtifactStats {
  failed: number;
  persisted: number;
}

export interface WorkflowRunHistoryStats {
  workflow_id: string;
  total_runs: number;
  matching_runs: number;
  waiting_event_runs: number;
  artifact_persistence: WorkflowRunHistoryArtifactStats;
}

export interface WorkflowRunLineage {
  workflow_run_id: string;
  root_run_id: string;
  total_attempts: number;
  parent_count: number;
  child_count: number;
  missing_parent_id: string | null;
  truncated: boolean;
  runs: WorkflowRun[];
}

export interface WorkflowRunManifest {
  workflow_run_id: string;
  workflow_id: string;
  workflow_version_id: string | null;
  manifest_mode: "saved_version" | "snapshot";
  manifest_hash: string;
  manifest: WorkflowManifest;
}

export interface WorkflowRunEvent {
  event_id: number;
  workflow_run_id: string;
  project_id: string | null;
  sequence: number;
  event_type: string;
  node_id: string | null;
  payload: Record<string, unknown> | null;
  created_at: string;
}

export interface WorkflowRunCheckpoint {
  checkpoint_id: string;
  workflow_run_id: string;
  project_id: string | null;
  sequence: number;
  node_id: string;
  state_blob: Record<string, unknown> | null;
  created_at: string;
}

/** One span in a workflow run's MLflow trace (in-app trace viewer). */
export interface WorkflowRunTraceSpan {
  span_id: string | null;
  parent_id: string | null;
  name: string;
  span_type: string;
  start_time_ms: number | null;
  end_time_ms: number | null;
  duration_ms: number | null;
  status: string;
  inputs?: unknown;
  outputs?: unknown;
  attributes: Record<string, unknown>;
}

/**
 * Response of GET /workflow-runs/{id}/trace. ``spans`` is empty (and
 * ``trace_id`` null) when the run has no MLflow trace — fake provider / tracing
 * off / MLflow absent — so the viewer renders a friendly empty state.
 */
export interface WorkflowRunTrace {
  trace_id: string | null;
  spans: WorkflowRunTraceSpan[];
  mlflow_url?: string | null;
}

/** An MLflow experiment (traces are scoped to one). */
export interface ObservabilityExperiment {
  experiment_id: string;
  name: string;
}

/** One row in the Observability trace list (compact MLflow trace summary). */
export interface ObservabilityTrace {
  trace_id: string | null;
  name: string;
  status: string;
  experiment_id: string | null;
  experiment_name: string | null;
  session_id: string | null;
  user: string | null;
  request_preview: string;
  response_preview: string;
  timestamp_ms: number | null;
  execution_time_ms: number | null;
  span_count: number;
  tool_call_count: number;
  total_tokens: number | null;
  cost_usd: number | null;
}

/** One time-bucket of the monitoring dashboard. */
export interface ObservabilityMetricBucket {
  ts: number;
  count: number;
  error_count: number;
  error_rate: number;
  p50_ms: number | null;
  p95_ms: number | null;
  tokens: number;
  cost_usd: number;
}

/** Time-bucketed trace metrics for the monitoring dashboard. */
export interface ObservabilityMetrics {
  buckets: ObservabilityMetricBucket[];
  bucket_ms: number;
  totals: {
    count: number;
    error_rate: number;
    p50_ms: number | null;
    p95_ms: number | null;
    tokens: number;
    cost_usd: number;
  };
}

/** A feedback/expectation assessment attached to a trace. */
export interface ObservabilityTraceAssessment {
  name: string;
  value: string | number | boolean | null;
  rationale: string | null;
  source: string | null;
}

/** Full in-app trace detail (the CALIBER mirror of MLflow's trace view). */
export interface ObservabilityTraceDetail {
  trace_id: string | null;
  name: string;
  status: string;
  experiment_id: string | null;
  session_id: string | null;
  user: string | null;
  spans: WorkflowRunTraceSpan[];
  mlflow_url?: string | null;
  request?: unknown;
  response?: unknown;
  request_time_ms: number | null;
  execution_time_ms: number | null;
  total_tokens: number | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  cost_usd: number | null;
  tags: Record<string, string>;
  assessments: ObservabilityTraceAssessment[];
}

export interface WorkflowSessionMemoryMessage {
  role: "user" | "assistant";
  content: string;
}

export interface WorkflowSessionMemoryEntry {
  workflow_id: string;
  node_id: string;
  session_id: string;
  message_history: WorkflowSessionMemoryMessage[];
  message_count: number;
  turn_count: number;
  created_at: string;
  updated_at: string;
  last_user_message: string | null;
  last_assistant_message: string | null;
}

export interface WorkflowSessionMemoryClearResult {
  workflow_id: string;
  session_id: string;
  node_id: string | null;
  deleted_entries: number;
  deleted_messages: number;
}

export interface WorkflowRuntimeApproval {
  runtime_approval_id: string;
  workflow_run_id: string;
  project_id: string | null;
  node_id: string;
  status: "pending" | "approved" | "rejected";
  requested_at: string;
  decided_at: string | null;
  decided_by: string | null;
  decision_reason: string | null;
  policy_snapshot: Record<string, unknown> | null;
}

export interface WorkflowRunCapabilities {
  queue_enabled: boolean;
  supports_async_submit: boolean;
  supports_cancel: boolean;
  supports_retry: boolean;
  supports_resume: boolean;
  runtime_approvals_enabled: boolean;
  checkpointing_enabled: boolean;
  event_backend: string;
  approval_readiness: {
    status: "ready" | "configuration_required";
    blockers: string[];
    decision_scope: string;
    allow_self_approval: boolean;
    audit_actions: string[];
    settings_path: string;
  };
}

export interface WorkflowComponentField {
  key: string;
  label: string;
  type: string;
  required: boolean;
  default: unknown;
  description?: string | null;
  /** Advanced/tuning field — collapsed behind "Show advanced" in the inspector. */
  advanced?: boolean;
  constraints: Record<string, unknown>;
  examples: unknown[];
}

export interface WorkflowComponentSetupCheck {
  label: string;
  help: string;
  kind: string;
  field?: string | null;
  fields?: string[];
  minimum?: number | null;
}

export interface WorkflowComponent {
  type: WorkflowNodeType;
  label: string;
  category: string;
  description: string;
  docs: string[];
  default_inputs: Record<string, PortSpec>;
  default_outputs: Record<string, PortSpec>;
  starter_node?: Record<string, unknown> | null;
  fields: WorkflowComponentField[];
  setup_checks?: WorkflowComponentSetupCheck[];
  /** Kept for compatibility but de-emphasized in the palette. */
  legacy?: boolean;
  /** Suggested modern replacement, shown on the legacy badge. */
  legacy_replacement?: string | null;
}

export interface WorkflowComponentCatalog {
  schema_version: number;
  components: WorkflowComponent[];
}

export type WorkflowTemplateKind =
  | "single_agent"
  | "multi_agent_handoff"
  | "guarded_pipeline"
  | "parallel_fanout"
  | "hitl_review"
  | "for_each_loop"
  | "refinement_loop"
  | "event_resume"
  | "graph_hybrid_rag"
  | "knowledge_rag"
  | "knowledge_age"
  | "knowledge_age_build"
  | "blank";

export interface WorkflowTemplate {
  kind: WorkflowTemplateKind;
  label: string;
  description: string;
  icon: string;
  gradient: string;
  manifest_template: WorkflowManifest;
}

export interface WorkflowBakeoffScenario {
  id: string;
  title: string;
  starter_kind: WorkflowTemplateKind;
  capabilities: string[];
  evidence_to_capture: string[];
}

export interface WorkflowBakeoffRubricSection {
  title: string;
  checks: string[];
}

export type WorkflowBakeoffScenarioStatus =
  | "not_started"
  | "in_progress"
  | "passed"
  | "blocked";

export type WorkflowBakeoffRubricScore = "" | "1" | "2" | "3" | "4" | "5";

export interface WorkflowBakeoffScenarioWorksheetEntry {
  status: WorkflowBakeoffScenarioStatus;
  minutes_to_first_success: string;
  evidence_links: string;
  notes: string;
}

export interface WorkflowBakeoffRubricWorksheetEntry {
  score: WorkflowBakeoffRubricScore;
  notes: string;
}

export interface WorkflowBakeoffWorksheet {
  product_name: string;
  evaluator: string;
  environment: string;
  summary: string;
  updated_at: string | null;
  scenarios: Record<string, WorkflowBakeoffScenarioWorksheetEntry>;
  rubric: Record<string, WorkflowBakeoffRubricWorksheetEntry>;
}

export type WorkflowBenchmarkReportStatus = "draft" | "completed" | "archived";

export interface WorkflowBenchmarkReport {
  report_id: string;
  name: string;
  owner: string;
  status: WorkflowBenchmarkReportStatus;
  product_name: string;
  evaluator: string;
  environment: string;
  summary: string;
  scenario_count: number;
  captured_count: number;
  passed_count: number;
  blocked_count: number;
  worksheet: WorkflowBakeoffWorksheet;
  created_at: string;
  updated_at: string;
}

export interface WorkflowTemplateCatalog {
  schema_version: number;
  templates: WorkflowTemplate[];
  bakeoff_scenarios?: WorkflowBakeoffScenario[];
  operator_rubric?: WorkflowBakeoffRubricSection[];
}

export interface CookbookReadinessCheck {
  label: string;
  /**
   * Mirrors the three states ``_catalog_payload`` in routes/cookbooks.py emits.
   * ``operator_confirmation_required`` is a recipe prerequisite the operator
   * attests to; ``configuration_required`` is a server-side capability check
   * that failed and that the operator can actually go and fix — which is why
   * only that branch carries ``settings_path``.
   */
  status: "operator_confirmation_required" | "configuration_required" | "ready";
  /** In-app route that resolves this check, when one exists. */
  settings_path?: string;
}

/**
 * One addressable step of a cookbook's recipe.
 *
 * The prose lives in `docs-site/cookbooks/<slug>/README.md`; this is the form
 * the guided checklist can record progress against. `id` is positional
 * (`01.1`, `01.2`, ...) and stable for a given catalog version.
 */
export interface CookbookStep {
  id: string;
  title: string;
  /** In-app route the step is performed on; always a route the SPA registers. */
  route: string;
}

export interface CookbookRecipe {
  id: string;
  slug: string;
  title: string;
  summary: string;
  icon: string;
  steps: CookbookStep[];
  template_kind: WorkflowTemplateKind;
  catalog_version: string;
  capabilities: string[];
  prerequisites: string[];
  activation_requires_review: boolean;
  manifest_template: WorkflowManifest;
  readiness: {
    status: "ready" | "configuration_required";
    checks: CookbookReadinessCheck[];
  };
}

export interface CookbookCatalog {
  schema_version: number;
  catalog_version: string;
  recipes: CookbookRecipe[];
}

export interface CookbookInstallResult {
  recipe: Omit<CookbookRecipe, "manifest_template" | "readiness">;
  workflow: Workflow;
  version: WorkflowVersion;
  activation_requires_review: boolean;
}

export interface PlatformCapabilities {
  workflow_runs: WorkflowRunCapabilities;
  sync_workflow_version_run: boolean;
  // Mirrors CAPABILITY_FIELDS in caliber/artifact_capabilities.py. `rollback` is
  // the field that makes this worth reading: four families report
  // `rollbackable: true` and each means something different by it — an alias
  // restore, a checkpoint-stack pop, a derivation from activation history, or a
  // prior snapshot written back as a *new* version. Rendering the boolean alone
  // is what makes one shared version-history panel imply one shared guarantee.
  artifact_families: Record<
    string,
    {
      kind: 'runtime_asset' | 'evidence_asset' | 'scoring_asset' | 'anchor_record';
      history: string;
      live_target: string;
      promotable: boolean;
      rollbackable: boolean;
      rollback:
        | 'alias_restore'
        | 'checkpoint_stack_pop'
        | 'derived_from_activation_history'
        | 'snapshot_restored_as_new_version'
        | 'none';
      evidence_bearing: boolean;
      gate_mode: string;
      calibration: string;
    }
  >;
}

export interface ToolUsage {
  tool_id: string;
  name: string;
  usage: Array<{
    workflow_id: string;
    version_id: string;
    version_number: number;
    status: string;
  }>;
}

/** Reflected implementation of a registered tool's callable (GET /tools/{id}/source). */
export interface ToolSource {
  module_path: string;
  callable_name: string;
  /** True when the real source was read off disk. */
  available: boolean;
  /** Python call signature, e.g. `extract_document(document_path, *, max_chars=200_000)`. */
  signature: string;
  /** Docstring of the callable (empty when none). */
  doc: string;
  /** The actual source code of the callable. */
  source: string;
  /** Why source/signature could not be reflected (null when available). */
  error: string | null;
}

// --- Object Store (MinIO / S3 console) --------------------------------------

export interface ObjectStoreStatus {
  connected: boolean;
  endpoint: string;
  bucket_count?: number;
  error?: string;
}

export interface ObjectStoreBucket {
  name: string;
  creation_date: string | null;
}

export interface ObjectStoreObject {
  key: string;
  size: number;
  created_at?: string | null;
  last_modified: string | null;
  etag: string;
}

export interface ObjectStoreListing {
  bucket: string;
  prefix: string;
  /** "folder" common-prefixes at this level (each ends with '/'). */
  prefixes: string[];
  objects: ObjectStoreObject[];
  next_token: string | null;
  is_truncated: boolean;
}

export interface ObjectStoreDeleteResult {
  deleted: number;
  errors: string[];
}

export interface ObjectStorePreview {
  bucket: string;
  key: string;
  size: number;
  created_at?: string | null;
  last_modified: string | null;
  etag: string;
  content_type: string;
  preview_bytes: number;
  truncated: boolean;
  is_text: boolean;
  text: string | null;
}

/** One worksheet of an extracted spreadsheet. */
export interface ObjectStoreSheet {
  name: string;
  rows: string[][];
}

/**
 * Server-side content extraction for Office documents (the browser can't render
 * the native binary). ``kind`` discriminates the payload:
 * - ``document``: Word/PowerPoint extracted to ``text``.
 * - ``sheet``: Excel extracted to ``sheets``.
 * - ``unsupported``: nothing to show inline; ``error`` explains why.
 */
export interface ObjectStoreExtract {
  bucket: string;
  key: string;
  format: string;
  size: number;
  kind: "document" | "sheet" | "unsupported";
  text?: string | null;
  sheets?: ObjectStoreSheet[];
  truncated?: boolean;
  error?: string | null;
}

export interface WorkflowDeployment {
  deployment_id: string;
  workflow_id: string;
  alias: string;
  version_id: string;
  environment: string | null;
  status: string;
  deployed_by: string | null;
  deployed_at: string;
}

export interface WorkflowService {
  service_id: string;
  workflow_id: string;
  alias: string;
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown>;
  enabled: boolean;
  auth_required: boolean;
  endpoint: string;
  created_by: string;
  created_at: string;
  updated_at: string;
  token_count: number;
}

export interface WorkflowServicePublishPayload {
  alias?: string;
  input_schema?: Record<string, unknown>;
  output_schema?: Record<string, unknown>;
  enabled?: boolean;
  auth_required?: boolean;
}

export interface WorkflowServiceToken {
  token_id: string;
  name: string;
  prefix: string;
  scopes: string[];
  created_by: string;
  created_at: string;
  expires_at: string | null;
  revoked_at: string | null;
}

/** Token creation returns the plaintext exactly once. */
export interface WorkflowServiceTokenCreated extends WorkflowServiceToken {
  token: string;
}

export interface WorkflowServiceTokenCreatePayload {
  name: string;
  scopes?: string[];
  expires_at?: string;
}

export interface WorkflowPromotion {
  promotion_id: string;
  workflow_id: string;
  alias: string;
  version_id: string;
  status: "pending" | "approved" | "rejected" | "superseded";
  gate_result: Record<string, unknown> | null;
  requested_by: string;
  requested_at: string;
  decided_by: string | null;
  decided_at: string | null;
  decision_reason: string | null;
}

export interface ToolDefinition {
  tool_id: string;
  name: string;
  version: string;
  description: string;
  module_path: string;
  callable_name: string;
  input_schema: Record<string, unknown> | null;
  output_schema: Record<string, unknown> | null;
  side_effect_level: "read" | "write" | "external_action";
  requires_approval: boolean;
  allow_in_preview: boolean;
  secret_refs: string[];
  test_cases: CalibrationCase[];
  last_calibration: CalibrationResult | null;
  owner: string;
  status: "active" | "deprecated" | "archived";
  deprecated_at: string | null;
  successor_tool_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface ValidationIssue {
  code: string;
  path: string;
  message: string;
  severity: "error" | "warning" | "info";
}

export interface ValidationReport {
  valid: boolean;
  errors: ValidationIssue[];
  warnings: ValidationIssue[];
}

export interface CompileResult {
  version_id: string;
  compiled_artifact_uri: string | null;
  compiler_version: string | null;
  manifest_hash: string;
  report: Record<string, unknown>;
  generated_python?: string;
  requirements?: string[];
  compile_ms?: number;
  cached?: boolean;
}

export interface PreviewStep {
  node_id: string;
  node_type: string;
  status: string;
  output: string;
  tokens?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  cached_prompt_tokens?: number;
  cost_usd?: number;
  model?: string | null;
  prompt_version?: string | null;
  tool_calls: Array<Record<string, unknown>>;
  handoff_target: string | null;
  detail: string;
  duration_ms?: number;
  input_by_port?: Record<string, unknown> | null;
  output_by_port?: Record<string, unknown> | null;
}

export type WorkflowRunStep = PreviewStep;

export interface PreviewResult {
  workflow_run_id?: string;
  status: "completed" | "blocked" | "error";
  output: string;
  error: string | null;
  tokens: number;
  tags: Record<string, string>;
  steps: PreviewStep[];
  guardrail_results: Array<Record<string, unknown>>;
  preview: boolean;
}

export interface WorkflowRunResult extends PreviewResult {
  workflow_run_id: string;
}

export interface PromoteResult {
  rotated: boolean;
  deployment: WorkflowDeployment | null;
  promotion: WorkflowPromotion | null;
  gate: Record<string, unknown>;
}

export interface ToolTestRunResult {
  tool_id: string;
  output: unknown;
  mocked: boolean;
  duration_ms: number;
  error: string | null;
}

/* ── Component calibration (tools + MCP tools) ───────────────────────────── */

export type CalibrationAssertionType =
  | "no_error"
  | "output_contains"
  | "equals";

export interface CalibrationAssertion {
  type: CalibrationAssertionType;
  value?: string | null;
}

export interface CalibrationCase {
  name: string;
  input: Record<string, unknown>;
  assertion: CalibrationAssertion;
}

export interface CalibrationCaseResult {
  name: string;
  passed: boolean;
  output?: unknown;
  error: string | null;
  duration_ms: number;
}

export interface CalibrationResult {
  pass_rate: number;
  total: number;
  passed: number;
  cases: CalibrationCaseResult[];
  ran_at: string | null;
}

export interface ToolTestCasesResult {
  tool_id: string;
  test_cases: CalibrationCase[];
}

export interface ToolCalibrationResult extends CalibrationResult {
  tool_id: string;
}

export interface ToolCalibrationJob {
  job_id: string;
  tool_id?: string;
  status: "queued" | "running" | "completed" | "failed";
  requested_by: string;
  result?: CalibrationResult | null;
  error?: string | null;
  created_at: string | null;
  claimed_at: string | null;
  claimed_by: string | null;
  finished_at: string | null;
  pass_rate?: number | null;
  retry_of_job_id: string | null;
  resolution: "retry" | "abandon" | null;
  resolution_reason: string | null;
  resolved_by: string | null;
  resolved_at: string | null;
}

export interface ToolCalibrationJobsResponse {
  jobs: ToolCalibrationJob[];
  total: number;
}

export interface McpToolTestCasesResult {
  server_id: string;
  tool_name: string;
  test_cases: CalibrationCase[];
}

export interface McpToolCalibrationResult extends CalibrationResult {
  server_id: string;
  tool_name: string;
}

export type ToolUpdatePayload = Partial<
  Pick<
    ToolDefinition,
    | "description"
    | "status"
    | "side_effect_level"
    | "requires_approval"
    | "allow_in_preview"
    | "owner"
    | "successor_tool_id"
  >
>;

export interface GraphDiff {
  added_nodes: Array<{ id: string; type?: string }>;
  removed_nodes: Array<{ id: string; type?: string }>;
  modified_nodes: Array<{
    id: string;
    changes: Array<{ field: string; from: unknown; to: unknown }>;
  }>;
  added_edges: string[];
  removed_edges: string[];
  modified_edges: Array<{
    id: string;
    changes: Array<{ field: string; from: unknown; to: unknown }>;
  }>;
  artifact_changes: Array<Record<string, unknown>>;
  deploy_gate_changes: Array<Record<string, unknown>>;
  empty: boolean;
}

export interface WorkflowPatch {
  patch_id: string;
  job_id: string | null;
  workflow_id: string;
  base_version_id: string;
  candidate_manifest: WorkflowManifest;
  semantic_ops: Array<Record<string, unknown>>;
  patch_summary: string;
  graph_diff: GraphDiff | null;
  risk_summary: string;
  created_at: string;
}

export interface ProposePatchResult {
  patch_id: string;
  diagnosis: {
    root_cause: string;
    affected_components: string[];
    localized_to: Record<string, unknown>;
    recommended_patch_type: string;
    confidence: number;
  };
  patch_kind: "workflow_manifest" | "prompt";
  semantic_ops: Array<Record<string, unknown>>;
  summary: string;
  prompt_suggestion: string | null;
  graph_diff: GraphDiff;
  candidate_manifest: WorkflowManifest;
  candidate_valid: boolean;
  candidate_validation: ValidationReport;
}

/** Result of the in-canvas copilot edit (POST …/copilot-edit). */
export interface CopilotEditResult {
  /** The full proposed manifest (modify-in-place); apply to the canvas on accept. */
  proposed_manifest: WorkflowManifest;
  summary: string;
  rationale: string;
  /** Base → proposed graph diff, rendered as an accept/reject overlay. */
  graph_diff: GraphDiff;
  /** Semantic validity of the proposal (surfaced, not enforced). */
  valid: boolean;
  report: ValidationReport;
  /** Registry artifacts the edit was grounded in. */
  grounding: { tools: string[]; skills: string[]; eval_datasets: string[] };
  usage: { input_tokens: number; output_tokens: number; cost_usd: number };
}

export type WorkflowCalibrationObjective =
  | "quality"
  | "tool_correctness"
  | "tool_adherence";

export interface WorkflowCalibrationDatasetSummary {
  available: boolean;
  dataset_ref?: string;
  dataset_name?: string;
  dataset_id?: string | null;
  active?: boolean;
  example_count?: number;
  reason?: string;
  checked?: Array<Record<string, unknown>>;
}

export interface WorkflowCalibrationJudgeStatus {
  available: boolean;
  provider?: string | null;
  model?: string | null;
  reason?: string;
}

export interface WorkflowCalibrationOptions {
  supported_objectives: WorkflowCalibrationObjective[];
  supported_move_set: string[];
  scorer_options: string[];
  default_budget: {
    max_candidates: number;
    max_eval_examples: number;
    min_examples: number;
  };
  data: {
    workflow_version_id?: string;
    deploy_gate_dataset?: WorkflowCalibrationDatasetSummary;
    judge?: WorkflowCalibrationJudgeStatus;
    available?: boolean;
    reason?: string;
  };
}

export interface WorkflowCalibrationRunPayload {
  agent_id: string;
  objective?: {
    maximize: WorkflowCalibrationObjective;
    epsilon: number;
  };
  budget?: {
    max_candidates: number;
    max_eval_examples?: number;
    min_examples?: number;
  };
  judge?: {
    enabled: boolean;
  };
}

export interface WorkflowCalibrationRunResult {
  item: VerificationItem;
  job: RefinementJob;
}

export interface McpServerDiscoveredTool {
  name: string;
  description: string;
  input_schema?: {
    type: string;
    properties?: Record<string, { type: string; description?: string }>;
    required?: string[];
  };
  output_schema?: Record<string, unknown> | null;
}

export interface McpServer {
  server_id: string;
  name: string;
  description: string;
  transport: "stdio" | "sse" | "streamable-http";
  uri: string;
  command: string;
  args: string[];
  env: Record<string, string>;
  headers: Record<string, string>;
  auth_type: "none" | "token" | "basic" | "custom";
  auth_config: Record<string, unknown>;
  discovered_tools: McpServerDiscoveredTool[];
  tool_policies: Record<string, McpToolPolicy>;
  tool_test_cases: Record<string, CalibrationCase[]>;
  tool_calibrations: Record<string, CalibrationResult>;
  icon: string;
  status: "active" | "error" | "disabled";
  last_connected_at: string | null;
  connection_error: string | null;
  execution?: McpExecutionReadiness;
  owner: string;
  created_at: string;
  updated_at: string;
}

export interface McpExecutionReadiness {
  ready: boolean;
  transport_ready: boolean;
  status_ready: boolean;
  boundary:
    | "none"
    | "local_containment"
    | "external_wrapper"
    | "bubblewrap"
    | "remote_transport"
    | "remote_https"
    | "managed_sidecar"
    | string;
  production_isolated: boolean;
  command_allowed: boolean | null;
  executable_available: boolean | null;
  remote_host_allowed: boolean | null;
  controls: string[];
  blockers: string[];
  warnings: string[];
}

export interface McpServerCreatePayload {
  name: string;
  description?: string;
  transport?: "stdio" | "sse" | "streamable-http";
  uri?: string;
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  headers?: Record<string, string>;
  auth_type?: "none" | "token" | "basic" | "custom";
  auth_config?: Record<string, unknown>;
  icon?: string;
  owner?: string;
  /** Seed the server with a known toolset (e.g. from a catalog template). */
  discovered_tools?: McpServerDiscoveredTool[];
  /** Explicit deploy-time classifications for seeded tools. */
  tool_policies?: Record<string, McpToolPolicy>;
}

export type McpServerUpdatePayload = Partial<
  Pick<
    McpServer,
    | "description"
    | "transport"
    | "uri"
    | "command"
    | "args"
    | "env"
    | "headers"
    | "auth_type"
    | "auth_config"
    | "icon"
    | "owner"
    | "status"
  >
>;

export interface McpTestConnectionResult {
  server_id: string;
  success: boolean;
  error: string | null;
  tools: McpServerDiscoveredTool[];
}

export interface McpToolInvocationResult {
  server_id: string;
  tool_name: string;
  success: boolean;
  error: string | null;
  result: unknown;
  duration_ms: number;
}

export interface McpToolPolicy {
  allowed: boolean;
  side_effect_level: "read" | "write" | "external_action";
  requires_approval: boolean;
  rate_limit_per_minute?: number | null;
}

export interface McpToolPolicyUpdatePayload {
  allowed?: boolean;
  side_effect_level?: "read" | "write" | "external_action";
  requires_approval?: boolean;
  rate_limit_per_minute?: number | null;
}

export interface McpDiscoveredToolWithPolicy extends McpServerDiscoveredTool {
  policy: McpToolPolicy;
  classified: boolean;
}

export interface McpDiscoverToolsResult {
  server_id: string;
  tools: McpServerDiscoveredTool[];
  tool_count: number;
  discovered_at: string | null;
}

export interface McpServerToolsResult {
  server_id: string;
  tools: McpDiscoveredToolWithPolicy[];
}

export interface SkillRenderResult {
  skill_id: string;
  skill_name: string;
  rendered_content: string;
  original_content: string;
  detected_variables: string[];
  unresolved_variables: string[];
  variables_applied: Record<string, string>;
  summary: string;
  word_count: number;
  char_count: number;
  duration_ms: number;
}

export interface PromptRenderResult {
  agent_id: string;
  agent_name: string;
  rendered_content: string;
  original_template: string;
  detected_variables: string[];
  unresolved_variables: string[];
  variables_applied: Record<string, string>;
  version: number | null;
  word_count: number;
  char_count: number;
  duration_ms: number;
}

/** A file/workspace storage record (storage doc §4.7 response projection). */
export interface WorkflowFile {
  file_id: string;
  file_ref: string;
  name: string;
  kind: string;
  relative_path: string;
  media_type: string | null;
  size_bytes: number;
  sha256: string | null;
  etag?: string | null;
  object_version_id?: string | null;
  version?: number;
  immutable_ref?: ManagedFileReference;
  status: string;
  storage_backend?: string;
  producer_node_id: string | null;
  created_at: string | null;
  metadata?: Record<string, unknown>;
  workflow_run_id?: string | null;
  playground_run_id?: string | null;
}

export interface ProjectDirectory {
  path: string;
  name: string;
  file_ref: string;
  storage_backend?: string | null;
  created_at?: string | null;
}

export interface WorkflowFileList {
  items: WorkflowFile[];
  directories?: ProjectDirectory[];
  next_cursor: string | null;
}

/** A project / workspace that groups uploaded files. */
export interface Project {
  project_id: string;
  name: string;
  description: string;
  owner: string;
  status: string;
  storage_backend?: string | null;
  created_at: string | null;
  updated_at: string | null;
  file_count?: number;
}

export interface ProjectStorageBackendOption {
  id: "local" | "s3";
  label: string;
  active: boolean;
  configured: boolean;
  reason?: string | null;
}

export interface ProjectStorageConfig {
  backend: "local" | "s3";
  backend_label: string;
  available_backends: ProjectStorageBackendOption[];
  base_uri?: string;
  bucket?: string | null;
  prefix?: string;
  public_endpoint_url?: string | null;
}
