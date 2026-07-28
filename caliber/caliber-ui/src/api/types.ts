/**
 * Response shapes that mirror the backend Pydantic schemas in
 * `caliber/src/caliber/schemas.py`. These are intentionally hand-typed
 * (rather than generated) for the first slice — when the API surface stabilizes
 * we can swap in `openapi-typescript` against an auto-generated spec.
 */

/** Envelope every CALIBER endpoint wraps its payload in. */
export interface Envelope<T> {
  data: T;
  next_cursor?: string | null;
}

export interface AssistantSloSummary {
  intent_confidence_avg: number | null;
  plans_total: number;
  plans_ready: number;
  plan_readiness_rate: number;
  clarification_rate: number;
  executions_total: number;
  executions_completed: number;
  executions_failed: number;
  executions_blocked: number;
  execution_success_rate: number;
  adapter_error_classes: Record<string, number>;
  publish_total: number;
  publish_success: number;
  publish_failed: number;
  publish_success_rate: number;
}

export interface DashboardSummary {
  agents_total: number;
  agents_enabled: number;
  verification_pending: number;
  verification_pending_critical: number;
  jobs_queued: number;
  jobs_running: number;
  jobs_awaiting_approval: number;
  jobs_completed: number;
  jobs_failed: number;
  jobs_rejected: number;
  approvals_pending: number;
  assistant_slo: AssistantSloSummary;
  generated_at: string;
}

/** Structured error body returned by the backend's exception handlers. */
export interface ApiErrorBody {
  detail: string;
  status_code: number;
  errors?: Array<{ loc: string[]; msg: string; type: string }>;
}

/**
 * Response of ``GET /caliber/csrf``.
 *
 * ``enabled=false`` is the common deployment shape (CSRF is handled by
 * an upstream auth proxy). When ``enabled=true``, the ``token`` must
 * accompany every state-changing request via ``X-CALIBER-CSRF`` and
 * is refreshed roughly every ``ttl_seconds`` minus a safety margin.
 */
export interface CSRFTokenResponse {
  enabled: boolean;
  token: string | null;
  ttl_seconds: number;
}

export interface CurrentUserInfo {
  user_id: string;
  scopes: string[];
  is_admin: boolean;
}

/** Current LLM credential + gateway setup (no secrets — only presence). */
export interface LlmSetupStatus {
  llm_provider: string;
  gateway_url: string;
  openai_key_env: string;
  openai_key_present: boolean;
  anthropic_key_present: boolean;
  assistant_engine: string;
  /**
   * Masked `••••<last 4>` hints so an operator can confirm *which* key is
   * live. Empty when no key resolves. The API deliberately never returns the
   * resolved values — the fields are write-only.
   */
  openai_key_fingerprint: string;
  anthropic_key_fingerprint: string;
}

/** Runtime override applied to the running server. Omit a key to leave it as-is. */
export interface LlmSetupUpdate {
  openai_api_key?: string;
  anthropic_api_key?: string;
  gateway_url?: string;
}

export type RuntimeSettingSource = "environment" | "configured" | "default";
export type RuntimeSettingControl = "live" | "environment";

export interface RuntimeConfigurationSetting {
  key: string;
  env_var: string;
  label: string;
  description: string;
  display_value: string;
  value_type: string;
  source: RuntimeSettingSource;
  control: RuntimeSettingControl;
  restart_required: boolean;
  sensitive: boolean;
}

export interface RuntimeConfigurationGroup {
  id: string;
  title: string;
  description: string;
  configured_count: number;
  live_editable_count: number;
  settings: RuntimeConfigurationSetting[];
}

export interface RuntimeConfigurationSummary {
  total: number;
  live_editable: number;
  environment_managed: number;
  configured: number;
  defaults: number;
  secret_sources: number;
}

export interface RuntimeConfigurationInventory {
  summary: RuntimeConfigurationSummary;
  groups: RuntimeConfigurationGroup[];
}

/* -------------------------------------------------------------------------- */
/* Verification queue                                                          */
/* -------------------------------------------------------------------------- */

export type VerificationStatus =
  | "pending"
  | "verified"
  | "dismissed"
  | "duplicate";
export type Severity = "critical" | "standard";

export interface VerificationItem {
  item_id: string;
  agent_id: string;
  assessment_id: string | null;
  trace_id: string | null;
  experiment_id: string | null;
  session_id: string | null;
  workflow_id: string | null;
  category: string;
  free_text: string;
  severity: Severity;
  artifact_type_hint: string | null;
  artifact_ref: string | null;
  submitted_context: Record<string, unknown> | null;
  status: VerificationStatus;
  priority: number;
  assigned_to: string | null;
  verified_by: string | null;
  verified_at: string | null;
  verification_notes: string | null;
  refinement_target: string | null;
  duplicate_of_id: string | null;
  created_at: string;
}

export interface VerificationItemCreatePayload {
  agent_id: string;
  category: string;
  free_text: string;
  severity?: Severity;
  artifact_type_hint?: string | null;
  artifact_ref?: string | null;
  submitted_context?: Record<string, unknown> | null;
  session_id?: string | null;
  workflow_id?: string | null;
}

export interface VerificationBatchResult {
  action: "verify" | "dismiss";
  requested: number;
  succeeded: number;
  failed: number;
  results: Array<{
    item_id: string;
    status: "succeeded" | "failed";
    reason: string | null;
    linked_job_id: string | null;
  }>;
}

export interface VerificationListFilters {
  status?: VerificationStatus;
  severity?: Severity;
  agent_id?: string;
}

/* -------------------------------------------------------------------------- */
/* Refinement jobs                                                             */
/* -------------------------------------------------------------------------- */

export type JobStatus =
  | "queued"
  | "running"
  | "candidate_ready"
  | "applied"
  | "completed"
  | "rejected"
  | "failed"
  | "cancelled";

export type JobStage =
  | "triage"
  | "evidence"
  | "diagnosis"
  | "candidate"
  | "eval"
  | "done";

export interface RefinementJob {
  job_id: string;
  agent_id: string;
  workflow_id: string | null;
  primary_item_id: string;
  mlflow_run_id: string | null;
  artifact_type: string;
  optimizer_type: string | null;
  skill_name?: string | null;
  status: JobStatus;
  current_stage: JobStage;
  attempt_count: number;
  error_message: string | null;
  total_tokens: number;
  cost_usd: number;
  bundle_targets: Array<Record<string, unknown>>;
  bundle_expansion_count: number;
  diagnosis: Record<string, unknown> | null;
  candidate: Record<string, unknown> | null;
  eval_results: Record<string, unknown> | null;
  calibration_spec: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
}

/** Eval-dataset example harvested from a verified correction (gap-analysis R2.3). */
export interface HarvestedExample {
  dataset_id: string;
  dataset_name: string;
  example_id: string;
  dataset_version: number;
}

/** Verify endpoint returns the updated item + the newly created job. */
export interface VerifyResponse {
  item: VerificationItem;
  job: RefinementJob;
  /** The correction captured as an eval example, when harvesting is enabled. */
  harvested?: HarvestedExample | null;
}

/* -------------------------------------------------------------------------- */
/* Prompt registry and calibration                                             */
/* -------------------------------------------------------------------------- */

export type PromptSource = "caliber" | "mlflow" | "both";

export interface PromptInfo {
  agent_id: string;
  agent_name: string;
  agent_enabled: boolean | null;
  prompt_name: string | null;
  version: number | null;
  alias: string;
  available_aliases: string[];
  template_preview: string | null;
  template_length: number;
  approval_id: string | null;
  artifact_ref: string | null;
  has_prompt: boolean;
  /** True when this asset has no deployed registry prompt yet (the backlog). */
  needs_prompt: boolean;
  description?: string | null;
  creation_timestamp?: number | string | null;
  source: PromptSource;
}

export interface PromptCreateResult {
  name: string;
  version: number;
  uri: string;
  template_preview: string;
  template_length: number;
  alias_changed: boolean;
  active_alias: string;
}

export interface PromptDetail {
  name: string;
  version: number | null;
  alias: string;
  template: string;
  template_length: number;
  artifact_ref: string;
}

/** Compact summary of the latest prompt-test run, as returned in the workspace. */
export interface PromptWorkspaceLastRun {
  test_run_id: string;
  overall_score: number | null;
  test_set_size: number;
  passed_count: number;
  failed_count: number;
  partial_count: number;
  created_at: string;
}

/**
 * Body of ``GET /caliber/prompts/{name}/workspace`` — the runtime facts the
 * per-prompt Workspace header surfaces. ``status`` is the computed lifecycle
 * stage (one of "Draft", "Has test set", "Tested", "Calibrated", "Bound").
 */
export interface PromptWorkspaceResponse {
  model: string | null;
  version: number | null;
  status: string;
  bound_to: Record<string, unknown> | null;
  dataset_id: string | null;
  last_run: PromptWorkspaceLastRun | null;
  /** The run pinned as the comparison baseline, and a cheap summary of it. */
  baseline_run_id: string | null;
  baseline_run: PromptWorkspaceLastRun | null;
}

/** Body of ``POST /caliber/prompts/{name}/bind`` — where a prompt is wired in. */
export interface PromptBindPayload {
  kind: "agent" | "workflow_node" | "standalone";
  agent_id?: string;
  workflow_id?: string;
  node_id?: string;
}

export interface PromptVersionInfo {
  name: string;
  version: number;
  aliases: string[];
  creation_timestamp: number | string | null;
  updated_timestamp: number | string | null;
  run_id: string | null;
  source: string | null;
  commit_message: string | null;
  current: boolean;
}

export interface PromptVersionDetail {
  name: string;
  version: number;
  template: string;
  template_length: number;
  artifact_ref: string;
}

export interface PromptAliasResult {
  name: string;
  alias: string;
  version: number;
}

export interface PromptTemplateVariableSpec {
  name: string;
  label: string;
  description: string;
  required?: boolean;
  default?: string;
  value?: string;
}

export interface PromptTemplateRuntimeVariableSpec {
  name: string;
  label: string;
  description: string;
  required?: boolean;
}

export interface PromptTemplateSections {
  instruction?: string | null;
  context?: string | null;
  examples?: string | null;
  input?: string | null;
  output_indicator?: string | null;
}

export interface PromptTemplateDefinition {
  id: string;
  kind: "base" | "modifier";
  source_kind?: "library" | "core" | "system";
  title: string;
  summary: string;
  domain: string;
  technique: string;
  recommended_modifiers: string[];
  recommended_scorers: string[];
  variables: PromptTemplateVariableSpec[];
  runtime_variables: PromptTemplateRuntimeVariableSpec[];
  compatible_base_ids: string[];
  incompatible_modifier_ids: string[];
  sections?: PromptTemplateSections;
  output_format?: string | null;
  sampling_policy?: string | null;
  composable_with?: string[];
  source_url?: string | null;
  owner?: string | null;
  status?: string | null;
  version?: string | null;
  execution_note?: string | null;
  is_wrapper?: boolean;
}

export interface PromptTemplateStarterRecipe {
  id: string;
  title: string;
  summary: string;
  domain: string;
  technique: string;
  support_level: "builder" | "workflow_only";
  support_reason: string;
  base_template_id: string | null;
  modifier_ids: string[];
  builder_values: Record<string, string>;
  runtime_variables: string[];
  preview_variables: Record<string, string>;
  template_override?: string | null;
  suggested_modifier_ids: string[];
  source_label: string;
  source_url: string;
  composable_with?: string[];
  execution_note?: string | null;
}

export interface PromptTemplateCatalog {
  catalog_version: string;
  base_templates: PromptTemplateDefinition[];
  modifiers: PromptTemplateDefinition[];
  starter_recipes: PromptTemplateStarterRecipe[];
}

/** Result of the assistant-backed "Describe it" prompt draft. */
export interface PromptDraftResult {
  /** The assistant's short reply / rationale. */
  reply: string;
  /** Suggested prompt name (may be empty). */
  name: string;
  /** Drafted prompt template text (may be empty if the engine declined). */
  template: string;
  /** Runtime variables the draft references. */
  variables: string[];
  /** One-line summary of the draft. */
  summary: string;
}

export interface PromptTemplatePreviewPayload {
  base_template_id: string;
  modifier_ids?: string[];
  builder_values?: Record<string, string>;
  preview_variables?: Record<string, string>;
  runtime_variables?: string[];
  template_override?: string;
  /**
   * Per-element overrides keyed by the canonical element name
   * (instruction/context/examples/input/output_indicator). Each value
   * replaces just that element after base + behaviors are composed.
   */
  section_overrides?: Record<string, string>;
}

/** The canonical prompt elements, in render order. */
export type PromptElementName =
  | "instruction"
  | "context"
  | "examples"
  | "input"
  | "output_indicator";

export interface PromptTemplateValidationReport {
  valid: boolean;
  errors: string[];
  warnings: string[];
}

export interface PromptTemplatePreviewResult {
  catalog_version: string;
  base_template: PromptTemplateDefinition;
  modifiers: PromptTemplateDefinition[];
  generated_template: string;
  compiled_template: string;
  rendered_preview: string;
  /**
   * The composed (post-behavior, pre-override) text of each element, keyed by
   * element name. This is what the element editor seeds from and what "Reset"
   * restores an element to. Placeholders are left intact.
   */
  composed_sections: Record<string, string>;
  /** Element names the caller has overridden, echoed back. */
  overridden_sections: string[];
  builder_variables: PromptTemplateVariableSpec[];
  runtime_variables: PromptTemplateRuntimeVariableSpec[];
  detected_variables: string[];
  unresolved_variables: string[];
  preview_variables_applied: Record<string, string>;
  validation_report: PromptTemplateValidationReport;
  word_count: number;
  char_count: number;
  recommended_scorers: string[];
}

export interface PromptCalibrationScorerOption {
  name: string;
  label: string;
  description: string;
  provider: string;
  category: string;
  default_enabled: boolean;
  requires_config: boolean;
  config_template: Record<string, unknown> | null;
  available: boolean;
  install_command: string | null;
  unavailable_reason: string | null;
}

export interface PromptCalibrationRuntimeStatus {
  available: boolean;
  package: string;
  install_policy: string;
  install_command: string;
  reason: string | null;
}

export interface PromptCalibrationOptions {
  optimizers: string[];
  default_optimizer: string;
  scorers: PromptCalibrationScorerOption[];
  default_scorers: string[];
  default_gate: {
    min_aggregate_score: number;
    max_regression_delta: number;
  };
  runtime: {
    deepeval: PromptCalibrationRuntimeStatus;
  };
}

export interface PromptCalibrationScorerSelection {
  name: string;
  weight: number;
  config?: Record<string, unknown>;
}

export interface PromptCalibrationRunPayload {
  agent_id: string;
  eval_dataset_id: string;
  /**
   * Pinned eval-dataset version for reproducibility. Defaults to the selected
   * dataset's current version at launch so a later dataset edit can't silently
   * change what the run scored against.
   */
  eval_dataset_version?: number;
  optimizer_type: string;
  scorers: PromptCalibrationScorerSelection[];
  prompt_alias?: string;
  gate?: {
    min_aggregate_score?: number;
    max_regression_delta?: number;
  };
  notes?: string;
}

export interface PromptCalibrationRunResult {
  item: VerificationItem;
  job: RefinementJob;
}

export type PromptOptimizationOptions = PromptCalibrationOptions;
export type PromptOptimizationRunPayload = PromptCalibrationRunPayload;
export type PromptOptimizationRunResult = PromptCalibrationRunResult;

/** One judged test case inside a persisted ad-hoc prompt-test run. */
export interface PromptTestCaseResult {
  testCaseId: string;
  input: string;
  expectedBehavior: string;
  actualResponse: string;
  verdict: "pass" | "fail" | "partial";
  score: number;
  reasoning: string;
}

/** Body of POST /prompts/test-runs — identity snapshot + per-case results. */
export interface PromptTestRunCreatePayload {
  agent_id: string;
  prompt_name?: string;
  prompt_alias?: string | null;
  prompt_version?: number | null;
  model?: string | null;
  eval_dataset_id?: string | null;
  results: PromptTestCaseResult[];
  trace_id?: string | null;
  mlflow_run_id?: string | null;
}

/** History-list row for a persisted prompt-test run (no per-case array). */
export interface PromptTestRunSummary {
  test_run_id: string;
  agent_id: string;
  prompt_name: string;
  prompt_alias: string | null;
  prompt_version: number | null;
  model: string | null;
  eval_dataset_id: string | null;
  test_set_size: number;
  passed_count: number;
  failed_count: number;
  partial_count: number;
  overall_score: number | null;
  trace_id: string | null;
  mlflow_run_id: string | null;
  created_by: string;
  status: string;
  created_at: string;
  completed_at: string | null;
}

/** Full prompt-test run including the per-case results array. */
export interface PromptTestRunDetail extends PromptTestRunSummary {
  results: PromptTestCaseResult[];
}

/* ── Tool test-runs + workspace (mirror the prompt equivalents) ──────────── */

/** Lifecycle a tool test run belongs to. */
export type ToolTestRunKind = "sandbox" | "suite" | "hardening";

/**
 * One judged case inside a persisted tool-test run. Mirrors the backend
 * ``ToolTestCaseResult`` (snake_case on the wire, unlike the prompt camelCase).
 */
export interface ToolTestRunResultCase {
  name: string;
  input: Record<string, unknown>;
  output?: unknown;
  error?: string | null;
  verdict: "pass" | "fail" | "partial";
  score: number;
  duration_ms?: number;
  reasoning?: string;
}

/** Body of POST /tools/test-runs — server recomputes counts/score. */
export interface ToolTestRunCreatePayload {
  tool_id: string;
  kind?: ToolTestRunKind;
  tool_version?: string | null;
  results: ToolTestRunResultCase[];
  trace_id?: string | null;
  mlflow_run_id?: string | null;
}

/** History-list row for a persisted tool-test run (no per-case array). */
export interface ToolTestRunSummary {
  test_run_id: string;
  tool_id: string;
  tool_version: string | null;
  kind: string;
  test_set_size: number;
  passed_count: number;
  failed_count: number;
  partial_count: number;
  overall_score: number | null;
  trace_id: string | null;
  mlflow_run_id: string | null;
  created_by: string;
  status: string;
  created_at: string;
  completed_at: string | null;
}

/** Full tool-test run including the per-case results array. */
export interface ToolTestRunDetail extends ToolTestRunSummary {
  results: ToolTestRunResultCase[];
}

/** Compact summary of the latest tool-test run, as returned in the workspace. */
export interface ToolWorkspaceLastRun {
  test_run_id: string;
  kind: string;
  overall_score: number | null;
  test_set_size: number;
  passed_count: number;
  failed_count: number;
  partial_count: number;
  created_at: string;
}

/**
 * Body of ``GET /caliber/tools/{tool_id}/workspace`` — the runtime facts the
 * per-tool Workspace header surfaces. ``lifecycle`` is the computed stage (one
 * of "Draft", "Has fixtures", "Tested", "Hardened", "Published").
 */
export interface ToolWorkspaceResponse {
  version: string;
  side_effect_level: string;
  status: string;
  lifecycle: string;
  last_run: ToolWorkspaceLastRun | null;
  baseline_run_id: string | null;
  baseline_run: ToolWorkspaceLastRun | null;
  has_fixtures: boolean;
  last_calibration_score: number | null;
}

export interface JobListFilters {
  status?: JobStatus;
  stage?: JobStage;
  agent_id?: string;
  workflow_id?: string;
}

export interface JobTarget {
  agent_id: string;
  artifact_type: string;
  artifact_ref?: string | null;
  role?: string | null;
  [key: string]: unknown;
}

export interface JobTargetsResponse {
  job_id: string;
  agent_id: string;
  artifact_type: string;
  bundle_size: number;
  targets: JobTarget[];
}

/**
 * Result of POST /jobs/{id}/apply — an operator promoting a
 * `candidate_ready` job's candidate. Replaces the removed approval
 * governance: `status` is the job's post-apply status (`applied`) and
 * `promotion` carries any registry/deployment promotion metadata.
 */
export interface JobApplyResult {
  job_id: string;
  status: string;
  promotion?: Record<string, unknown>;
}

/* -------------------------------------------------------------------------- */
/* Agents                                                                      */
/* -------------------------------------------------------------------------- */

export interface AgentConfig {
  agent_id: string;
  experiment_id: string;
  name: string;
  owner: string;
  artifact_types: string[];
  eval_thresholds: Record<string, unknown>;
  optimizer_config: Record<string, unknown>;
  approval_policy: Record<string, unknown>;
  optimize_for: string;
  collaboration_mode: string | null;
  enabled: boolean;
  required_approvals: number;
  created_at: string;
  updated_at: string;
}

export interface AgentRegisterPayload {
  agent_id: string;
  experiment_id: string;
  name: string;
  /** Ownership is derived from the authenticated actor. */
  owner?: string;
  artifact_types?: string[];
  eval_thresholds?: Record<string, unknown>;
  optimizer_config?: Record<string, unknown>;
  approval_policy?: Record<string, unknown>;
  optimize_for?: string;
  collaboration_mode?: string | null;
  enabled?: boolean;
  required_approvals?: number;
}

export interface AgentUpdatePayload {
  name?: string;
  owner?: string;
  artifact_types?: string[];
  eval_thresholds?: Record<string, unknown>;
  optimizer_config?: Record<string, unknown>;
  approval_policy?: Record<string, unknown>;
  optimize_for?: string;
  collaboration_mode?: string | null;
  enabled?: boolean;
  required_approvals?: number;
}

/* -------------------------------------------------------------------------- */
/* Rollback checkpoints                                                        */
/* -------------------------------------------------------------------------- */

export interface RollbackCheckpoint {
  checkpoint_id: string;
  approval_id: string;
  agent_id: string;
  artifact_type: string;
  artifact_name: string;
  artifact_ref_before: string | null;
  artifact_ref_after: string;
  version_before: number | null;
  version_after: number | null;
  rolled_back_at: string | null;
  rolled_back_by: string | null;
  created_at: string;
}

export interface RollbackResponse {
  checkpoint: RollbackCheckpoint;
  rotated_to: string;
  rotated_at: string;
}

/* -------------------------------------------------------------------------- */
/* Skills (Phase 4)                                                            */
/* -------------------------------------------------------------------------- */

export type ResourceStatus = "active" | "archived";

// Keep in sync with backend SKILL_CATEGORIES (caliber/schemas.py).
export type SkillCategory =
  | "document_creation"
  | "data_analysis"
  | "data_extraction"
  | "code_generation"
  | "content_writing"
  | "summarization"
  | "classification"
  | "research"
  | "customer_support"
  | "communication"
  | "reasoning_planning"
  | "tool_integration"
  | "compliance_safety"
  | "workflow_automation"
  | "mcp_enhancement"
  | "custom";

export interface Skill {
  skill_id: string;
  name: string;
  description: string;
  summary: string;
  content: string;
  owner: string;
  category: SkillCategory;
  tags: string[];
  skill_metadata: Record<string, unknown>;
  allowed_tools: string | null;
  depends_on: string[];
  status: ResourceStatus;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface SkillCreatePayload {
  name: string;
  description?: string;
  summary?: string;
  content: string;
  owner: string;
  category?: SkillCategory;
  tags?: string[];
  skill_metadata?: Record<string, unknown>;
  allowed_tools?: string | null;
  depends_on?: string[];
}

/** One uploaded file in a skill package import ({path, content} text records). */
export interface SkillPackageImportFile {
  path: string;
  content: string;
}

/** Body of POST /skills/import-package. name/description/content are derived
 *  server-side from the uploaded SKILL.md, so they are NOT sent here. */
export interface SkillPackageImportPayload {
  owner: string;
  category?: SkillCategory;
  tags?: string[];
  skill_metadata?: Record<string, unknown>;
  allowed_tools?: string | null;
  depends_on?: string[];
  files: SkillPackageImportFile[];
}

export interface SkillUpdatePayload {
  description?: string;
  summary?: string;
  content?: string;
  owner?: string;
  category?: SkillCategory;
  tags?: string[];
  skill_metadata?: Record<string, unknown>;
  allowed_tools?: string | null;
  depends_on?: string[];
  status?: ResourceStatus;
}

export interface AgentSkillsResponse {
  skills: Skill[];
  missing: string[];
}

export interface SkillPackageFile {
  path: string;
  kind: string;
  content: string;
}

export interface SkillPackage {
  root: string;
  format: string;
  files: SkillPackageFile[];
  resource_counts: {
    scripts: number;
    references: number;
    assets: number;
    [key: string]: number;
  };
  warnings: string[];
  is_valid: boolean;
}

/* ── Skill test-runs + workspace (mirror the prompt/tool equivalents) ────── */

/** Lifecycle a skill test run belongs to. */
export type SkillTestRunKind = "selection" | "render" | "scenario";

/**
 * One judged case inside a persisted skill-test run. Mirrors the backend
 * ``SkillTestCaseResult`` (snake_case on the wire, like the tool shape). A
 * selection-trigger case carries the user message in ``input`` and the selection
 * decision in ``output``.
 */
export interface SkillTestRunResultCase {
  name: string;
  input: Record<string, unknown>;
  output?: unknown;
  error?: string | null;
  verdict: "pass" | "fail" | "partial";
  score: number;
  duration_ms?: number;
  reasoning?: string;
}

/** Body of POST /skills/test-runs — server recomputes counts/score. */
export interface SkillTestRunCreatePayload {
  skill_id: string;
  kind?: SkillTestRunKind;
  skill_version?: number | null;
  host_agent_id?: string | null;
  results: SkillTestRunResultCase[];
  trace_id?: string | null;
  mlflow_run_id?: string | null;
}

/** History-list row for a persisted skill-test run (no per-case array). */
export interface SkillTestRunSummary {
  test_run_id: string;
  skill_id: string;
  skill_version: number | null;
  kind: string;
  test_set_size: number;
  passed_count: number;
  failed_count: number;
  partial_count: number;
  overall_score: number | null;
  host_agent_id: string | null;
  trace_id: string | null;
  mlflow_run_id: string | null;
  created_by: string;
  status: string;
  created_at: string;
  completed_at: string | null;
}

/** Full skill-test run including the per-case results array. */
export interface SkillTestRunDetail extends SkillTestRunSummary {
  results: SkillTestRunResultCase[];
}

/** Compact summary of the latest skill-test run, as returned in the workspace. */
export interface SkillWorkspaceLastRun {
  test_run_id: string;
  kind: string;
  overall_score: number | null;
  test_set_size: number;
  passed_count: number;
  failed_count: number;
  partial_count: number;
  created_at: string;
}

/**
 * Body of ``GET /caliber/skills/{skill_id}/workspace`` — the runtime facts the
 * per-skill Workspace header surfaces. ``lifecycle`` is the computed stage (one
 * of "Draft", "Has scenarios", "Tested", "Calibrated", "Bound").
 */
export interface SkillWorkspaceResponse {
  version: number | null;
  category: string | null;
  status: string;
  lifecycle: string;
  last_run: SkillWorkspaceLastRun | null;
  baseline_run_id: string | null;
  baseline_run: SkillWorkspaceLastRun | null;
  bound_to: Record<string, unknown> | null;
}

/** Body of ``POST /caliber/skills/{skill_id}/bind`` — where a skill is wired in. */
export interface SkillBindPayload {
  kind: "agent" | "workflow_node" | "standalone";
  agent_id?: string;
  workflow_id?: string;
  node_id?: string;
}

/** Body of ``POST /caliber/skills/{skill_id}/calibrate`` — agent-free calibrate. */
export interface SkillCalibratePayload {
  optimizer_type?: string;
  notes?: string;
}

/** Result of ``POST /caliber/skills/{skill_id}/test-selection``. */
export interface SkillSelectionResult {
  skill_id: string;
  skill_name: string;
  is_selected: boolean;
  selection_score: number;
  selection_reason: string;
}

/* -------------------------------------------------------------------------- */
/* Eval datasets                                                              */
/* -------------------------------------------------------------------------- */

export interface EvalDataset {
  dataset_id: string;
  name: string;
  description: string;
  owner: string;
  tags: string[];
  status: ResourceStatus;
  version: number;
  created_at: string;
  updated_at: string;
  /** MLflow GenAI dataset sync linkage (null until first synced). */
  mlflow_dataset_id: string | null;
  mlflow_synced_at: string | null;
  mlflow_synced_version: number | null;
  mlflow_record_count: number | null;
  mlflow_digest: string | null;
}

export interface EvalDatasetCreatePayload {
  name: string;
  description?: string;
  owner: string;
  tags?: string[];
}

/** A custom LLM judge (MLflow 3.14 make_judge). */
export type JudgeValueType = "bool" | "int" | "float" | "str";

export interface Judge {
  judge_id: string;
  name: string;
  description: string;
  instructions: string;
  model: string | null;
  feedback_value_type: JudgeValueType | null;
  owner: string;
  tags: string[];
  status: ResourceStatus;
  created_at: string;
  updated_at: string;
}

export interface JudgeCreatePayload {
  name: string;
  description?: string;
  instructions: string;
  model?: string | null;
  feedback_value_type?: JudgeValueType | null;
  tags?: string[];
}

export interface JudgeUpdatePayload {
  description?: string;
  instructions?: string;
  model?: string | null;
  feedback_value_type?: JudgeValueType | null;
  tags?: string[];
  status?: ResourceStatus;
}

/** POST /judges/{id}/test-run — the "Try it" playground. */
export interface JudgeTestRunPayload {
  inputs?: Record<string, unknown>;
  outputs: string;
  expectations?: Record<string, unknown>;
}

export interface JudgeTestRunResult {
  /** Unit score in [0,1]. */
  score: number;
  /** Raw judge verdict (bool / number / string). */
  value: unknown;
  rationale: string | null;
}

/** One human-labeled example for a judge-alignment check. */
export interface JudgeAlignmentExampleInput {
  inputs?: Record<string, unknown>;
  outputs: string;
  expectations?: Record<string, unknown>;
  /** The human verdict (pass/fail) the judge is measured against. */
  label: boolean;
}

/** POST /judges/{id}/alignment */
export interface JudgeAlignmentPayload {
  examples: JudgeAlignmentExampleInput[];
  threshold?: number;
}

export interface JudgeAlignmentPerExample {
  outputs: string;
  human_label: boolean;
  judge_label: boolean | null;
  judge_score: number | null;
  agree: boolean;
  error: string | null;
}

/** Agreement rate + Cohen's kappa between a judge and human labels. */
export interface JudgeAlignmentResult {
  n: number;
  scored: number;
  agreement_rate: number;
  cohen_kappa: number;
  threshold: number;
  confusion: Record<string, number>;
  per_example: JudgeAlignmentPerExample[];
}

/** Structured human-review queues (CALIBER-native, on OSS assessment primitives). */
export type ReviewQuestionType =
  | "pass_fail"
  | "categorical"
  | "numeric"
  | "text";
export type ReviewQuestionTarget = "feedback" | "expectation";

export interface ReviewQuestion {
  key: string;
  title: string;
  type: ReviewQuestionType;
  options: string[];
  required: boolean;
  target: ReviewQuestionTarget;
}

export interface ReviewQueue {
  queue_id: string;
  name: string;
  description: string;
  questions: ReviewQuestion[];
  reviewers: string[];
  owner: string;
  status: ResourceStatus;
  created_at: string;
  updated_at: string;
  item_count: number | null;
  pending_count: number | null;
}

export interface ReviewItem {
  item_id: string;
  queue_id: string;
  trace_id: string;
  experiment_id: string | null;
  status: "pending" | "completed" | "skipped";
  assigned_to: string | null;
  answers: Record<string, unknown>;
  assessment_ids: string[];
  created_at: string;
  completed_at: string | null;
  completed_by: string | null;
}

export interface ReviewQueueDetail {
  queue: ReviewQueue;
  items: ReviewItem[];
}

export interface ReviewQueueCreatePayload {
  name: string;
  description?: string;
  questions: ReviewQuestion[];
  reviewers?: string[];
}

export interface ReviewQueueUpdatePayload {
  description?: string;
  questions?: ReviewQuestion[];
  reviewers?: string[];
  status?: ResourceStatus;
}

/** Aria goal-plans (agentic orchestration). */
export type AriaAutonomy = "ask_each" | "approve_plan" | "auto_guarded";
export type AriaPlanStatus =
  | "draft"
  | "approved"
  | "running"
  | "paused"
  | "completed"
  | "failed"
  | "cancelled";
export type AriaStepStatus =
  | "pending"
  | "blocked"
  | "running"
  | "waiting_input"
  | "waiting_job"
  | "done"
  | "failed"
  | "skipped";

export interface AriaTaskContextRef {
  ref_type: string;
  ref_id: string;
  label?: string;
  metadata_?: Record<string, unknown>;
}

export interface AriaPlan {
  plan_id: string;
  session_id: string | null;
  project_id: string | null;
  goal: string;
  status: AriaPlanStatus;
  autonomy: AriaAutonomy;
  owner: string;
  constraints: Record<string, unknown>;
  done_when: string[];
  context_refs: AriaTaskContextRef[];
  created_at: string;
  updated_at: string;
  step_count: number | null;
}

export interface AriaPlanStep {
  step_id: string;
  plan_id: string;
  seq: number;
  capability_key: string;
  title: string;
  inputs: Record<string, unknown>;
  input_schema?: Record<string, unknown>;
  depends_on: string[];
  status: AriaStepStatus;
  result: Record<string, unknown>;
  evidence: Record<string, unknown>;
  error: string | null;
  draft_id: string | null;
  job_id: string | null;
  approval_id: string | null;
  checkpoint_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface AriaPlanDetail {
  plan: AriaPlan;
  steps: AriaPlanStep[];
}

export interface AriaPlanCreatePayload {
  goal: string;
  session_id?: string | null;
  autonomy?: AriaAutonomy;
  constraints?: Record<string, unknown>;
  done_when?: string[];
  context_refs?: AriaTaskContextRef[];
}

export interface AriaPlanUpdatePayload {
  autonomy?: AriaAutonomy;
  status?: "draft" | "cancelled";
}

export interface AriaInteraction {
  interaction_id: string;
  plan_id: string;
  step_id: string;
  kind: "permission" | "choice" | "input" | "confirm";
  prompt: string;
  options: Array<{ label: string; value: unknown }>;
  evidence: Record<string, unknown>;
  required_scope: string | null;
  status: "pending" | "answered" | "cancelled";
  response: Record<string, unknown>;
  responded_by: string | null;
  responded_at: string | null;
  created_at: string;
}

export interface AriaInteractionAnswerPayload {
  approved?: boolean;
  choice?: string;
  value?: unknown;
  inputs?: Record<string, unknown>;
}

export interface EvalDatasetUpdatePayload {
  description?: string;
  owner?: string;
  tags?: string[];
  status?: ResourceStatus;
}

export interface EvalExample {
  example_id: string;
  dataset_id: string;
  dataset_version: number;
  input: Record<string, unknown>;
  expected: Record<string, unknown>;
  weight: number;
  tags: string[];
  created_at: string;
  superseded_at: string | null;
  superseded_version: number | null;
}

export interface EvalExampleCreatePayload {
  input?: Record<string, unknown>;
  expected?: Record<string, unknown>;
  weight?: number;
  tags?: string[];
}

/** POST /eval-datasets/{id}/examples/from-trace */
export interface EvalExampleFromTracePayload {
  trace_id: string;
  input?: Record<string, unknown>;
  expected?: Record<string, unknown>;
  weight?: number;
  tags?: string[];
}

/** One scored example within an evaluation run's scorecard. */
/**
 * GET /agents/{id}/experiment — whether the agent's MLflow experiment binding
 * actually resolves.
 *
 * `unverified` is a distinct outcome from `missing`: the registry could not be
 * reached, so "we could not check" must not be shown as "it is not there".
 */
export interface AgentExperimentBinding {
  configured_experiment_id: string;
  status: "reachable" | "missing" | "unverified";
  detail: string;
  experiment_id?: string;
  name?: string | null;
  lifecycle_stage?: string;
}

export interface EvalRunResultRow {
  example_id: string;
  input: Record<string, unknown>;
  expected: Record<string, unknown>;
  prediction: string;
  scores: Record<string, number>;
  score: number;
  passed: boolean;
  error: string | null;
  /**
   * Curation metadata carried through from the dataset example. `weight` drives
   * the run's aggregate/`overall`/`pass_rate` means, so a scorecard whose rows
   * are not all weight 1 needs to show it — otherwise the headline number looks
   * inconsistent with the visible rows. Optional because older persisted runs
   * predate these fields.
   */
  weight?: number;
  tags?: string[];
}

/** Summary form of an evaluation run (GET /evaluations list — no rows). */
/** What an evaluation run scores. */
export type EvalPredictTarget = "llm" | "prompt" | "skill" | "workflow";

export interface EvalRunSummary {
  run_id: string;
  dataset_id: string;
  dataset_version: number;
  label: string;
  predict_target: string;
  subject_ref: string | null;
  model: string | null;
  scorers: string[];
  pass_threshold: number;
  n_examples: number;
  passed_count: number;
  failed_count: number;
  overall_score: number | null;
  pass_rate: number | null;
  aggregate: Record<string, number>;
  status: string;
  error_message: string | null;
  created_by: string;
  created_at: string;
  completed_at: string | null;
}

/**
 * The run's immutable evidence bundle. Written once with the run, so a pinned run
 * is checkable rather than reproducible-by-convention: `digests.dataset` answers
 * "was this the same data?", `digests.content` answers "is this the same result?",
 * `sampling` states how much of the dataset was actually graded, `denominators`
 * gives every aggregate mean the row/weight count behind it, and `slices` groups
 * by dataset tag.
 *
 * `null` for runs created before the contract existed — deliberately not
 * backfilled, since the digests can only be computed from the inputs at the time
 * the run executed.
 */
export interface EvalRunEvidence {
  schema_version: number;
  digests: { dataset: string; content: string };
  sampling: {
    available_examples: number;
    evaluated_examples: number;
    cap: number | null;
    truncated: boolean;
    order: string;
  };
  denominators: Record<string, { valid_rows: number; weight_sum: number }>;
  slices: Record<
    string,
    {
      n_examples: number;
      weight_sum: number;
      passed_count: number;
      errored_count: number;
      overall: number | null;
      pass_rate: number | null;
    }
  >;
  policy: { scorers: string[]; pass_threshold: number; incomplete_row_policy: string };
  resolved: Record<string, unknown>;
  cost: {
    avg_latency_ms: number | null;
    max_latency_ms: number | null;
    total_latency_ms: number | null;
  };
}

/** Detail form — adds the heavy per-example results array plus the evidence. */
export interface EvalRun extends EvalRunSummary {
  results: EvalRunResultRow[];
  evidence?: EvalRunEvidence | null;
}

/** POST /evaluations */
export interface EvalRunCreatePayload {
  dataset_id: string;
  dataset_version?: number;
  label?: string;
  scorers?: string[];
  pass_threshold?: number;
  max_examples?: number;
  /** What to score: a generic completion (default) or a real artifact. */
  predict_target?: EvalPredictTarget;
  /** The artifact under test for non-"llm" targets (prompt ref / skill id). */
  subject_ref?: string;
}

/** One LLM endpoint exposed by the MLflow AI Gateway. */
export interface GatewayEndpoint {
  name: string;
  endpoint_type: string;
  provider: string;
  model: string;
  endpoint_url: string;
  limit: Record<string, unknown> | null;
}

/** GET /gateway — MLflow AI Gateway discovery + routing status. */
export interface GatewayStatus {
  configured: boolean;
  reachable: boolean;
  gateway_uri: string;
  routing_through_gateway: boolean;
  llm_base_url: string;
  endpoints: GatewayEndpoint[];
  error: string | null;
}

/** One gateway guardrail (MLflow scorer-backed). */
export interface GatewayGuardrail {
  guardrail_id: string;
  name: string;
  stage: string; // BEFORE | AFTER
  action: string; // VALIDATION | SANITIZATION
  scorer: string | null;
  action_endpoint_name: string | null;
}

/** A guardrail as attached to an endpoint (with its execution order). */
export interface GatewayEndpointGuardrail {
  guardrail_id: string;
  name: string;
  execution_order: number | null;
  enabled: boolean;
}

/** The guardrails protecting one endpoint, in execution order. */
export interface GatewayGuardrailCoverage {
  endpoint: string;
  endpoint_id: string;
  guardrails: GatewayEndpointGuardrail[];
}

/** GET /gateway/guardrails — guardrails + per-endpoint coverage. */
export interface GatewayGuardrailsStatus {
  configured: boolean;
  reachable: boolean;
  guardrails: GatewayGuardrail[];
  coverage: GatewayGuardrailCoverage[];
  error: string | null;
}

/** One config input a guardrail scorer template exposes (drives the create form). */
export interface GatewayScorerField {
  name: string;
  label: string;
  type: "text" | "textarea" | "select" | "multiselect" | "boolean";
  required: boolean;
  help: string | null;
  placeholder: string | null;
  options: string[];
}

/** A buildable guardrail kind backed by a native (no-extra-deps) MLflow scorer. */
export interface GatewayScorerTemplate {
  type: string; // pii | toxicity | guidelines | regex
  label: string;
  summary: string;
  scorer_class: string;
  deterministic: boolean;
  default_stage: string;
  default_action: string;
  fields: GatewayScorerField[];
}

/** An already-registered scorer that can back a guardrail directly. */
export interface GatewayRegisteredScorer {
  name: string;
  scorer_id: string;
  version: number;
}

/** GET /gateway/guardrails/catalog — what the create form can offer. */
export interface GatewayGuardrailCatalog {
  configured: boolean;
  reachable: boolean;
  templates: GatewayScorerTemplate[];
  scorers: GatewayRegisteredScorer[];
  error: string | null;
}

/** POST /gateway/guardrails — body to define a new guardrail. */
export interface GatewayGuardrailCreateRequest {
  name: string;
  stage: string; // BEFORE | AFTER
  action: string; // VALIDATION | SANITIZATION
  action_endpoint_id?: string | null;
  scorer_type?: string | null;
  config?: Record<string, unknown>;
  scorer_id?: string | null;
  scorer_version?: number | null;
}

/** One time bucket of trace-derived usage. */
export interface GatewayUsageBucket {
  ts: number;
  count: number;
  error_count: number;
  error_rate: number;
  p50_ms: number | null;
  p95_ms: number | null;
  tokens: number;
  cost_usd: number;
}

/** Per-model usage rollup. */
export interface GatewayUsageByModel {
  model: string;
  calls: number;
  tokens: number;
  cost_usd: number;
}

/** GET /gateway/usage — trace-derived metrics over time + by-model. */
export interface GatewayUsage {
  buckets: GatewayUsageBucket[];
  bucket_ms: number;
  totals: {
    count: number;
    error_rate: number;
    p50_ms: number | null;
    p95_ms: number | null;
    tokens: number;
    cost_usd: number;
  };
  by_model: GatewayUsageByModel[];
}

/** A per-model token-pricing row (gateway cost config). */
export interface LlmPricing {
  pricing_id: string;
  provider: string;
  model_id: string;
  prompt_price: number;
  completion_price: number;
  cached_prompt_price: number | null;
  owner: string;
  tags: string[];
  status: ResourceStatus;
  created_at: string;
  updated_at: string;
}

export interface LlmPricingCreatePayload {
  provider: string;
  model_id: string;
  prompt_price: number;
  completion_price: number;
  cached_prompt_price?: number | null;
  tags?: string[];
}

export interface LlmPricingUpdatePayload {
  provider?: string;
  model_id?: string;
  prompt_price?: number;
  completion_price?: number;
  cached_prompt_price?: number | null;
  tags?: string[];
  status?: ResourceStatus;
}

/** One backing platform service (Settings → Services). */
export interface SystemService {
  key: string;
  name: string;
  description: string;
  category: string;
  url: string | null;
  target: string;
  healthy: boolean | null;
  detail: string;
  latency_ms: number | null;
}

/** GET /system/services — backing-service health for the Services tab. */
export interface SystemServicesResponse {
  services: SystemService[];
  checked_at_ms: number;
}

/** One row of the append-only audit trail (GET /audit-log). */
export interface AuditLogEntry {
  log_id: number;
  /** ISO-8601 timestamp. */
  timestamp: string;
  actor: string;
  action: string;
  entity_type: string;
  entity_id: string;
  details: Record<string, unknown> | null;
}

/** A filtered page of audit entries plus the total match count (GET /audit-log). */
export interface AuditLogPage {
  entries: AuditLogEntry[];
  /** Total rows matching the active filters, ignoring limit/offset. */
  total: number;
  limit: number;
  offset: number;
}

/** Filters accepted by the audit-log list + export endpoints. */
export interface AuditLogFilters {
  actor?: string;
  action?: string;
  entity_type?: string;
  entity_id?: string;
  since?: string;
  until?: string;
  limit?: number;
  offset?: number;
}

/** GET /readiness — provider-honesty surface (no secrets). */
export interface ProviderReadiness {
  providers: {
    llm: string;
    eval: string;
    promoter: string;
    artifact_store: string;
  };
  simulated: string[];
  all_real: boolean;
  tracing_enabled: boolean;
  tracing_autolog_enabled: boolean;
  workflow_llm_judge_enabled: boolean;
}
