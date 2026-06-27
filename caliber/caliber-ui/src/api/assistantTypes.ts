/* Assistant authoring types — Caliber Assistant agentic authoring surface. */

// ---------- Artifact type ----------

export type ArtifactType =
  | "tool"
  | "skill"
  | "prompt"
  | "workflow"
  | "mcp_server";
export type SkillRuntimeMode = "auto" | "manual" | "off";

// ---------- Interaction mode (Chat / Build / Plan toggle) ----------

export type AssistantMode = "chat" | "build" | "plan";

export const ASSISTANT_MODES: AssistantMode[] = ["chat", "build", "plan"];

export const ASSISTANT_MODE_LABELS: Record<AssistantMode, string> = {
  chat: "Chat",
  build: "Design",
  plan: "Plan",
};

export const ASSISTANT_MODE_HINTS: Record<AssistantMode, string> = {
  chat: "Ask questions — Aria answers without creating artifacts.",
  build: "Aria designs, authors, and edits artifacts.",
  plan: "Aria outlines an approach before building.",
};

export const ASSISTANT_MODE_CAPTIONS: Record<AssistantMode, string> = {
  chat: "Discuss and iterate",
  build: "Create or refine artifacts",
  plan: "Map the approach first",
};

// ---------- Approval mode (how Aria's actions get approved) ----------

export type AssistantApprovalMode = "manual" | "auto_safe" | "auto_all";

export const ASSISTANT_APPROVAL_MODES: AssistantApprovalMode[] = [
  "manual",
  "auto_safe",
  "auto_all",
];

export const ASSISTANT_APPROVAL_MODE_LABELS: Record<
  AssistantApprovalMode,
  string
> = {
  manual: "Ask first",
  auto_safe: "Approve for me",
  auto_all: "Full access",
};

export const ASSISTANT_APPROVAL_MODE_HINTS: Record<
  AssistantApprovalMode,
  string
> = {
  manual:
    "Aria proposes changes and asks before every validate, test, approve, or publish action.",
  auto_safe:
    "Aria can auto-run safe validation and test steps, but still asks before approval or publish.",
  auto_all:
    "Aria can validate, test, approve, and publish a passing draft without pausing for approval.",
};

export const ASSISTANT_APPROVAL_MODE_CAPTIONS: Record<
  AssistantApprovalMode,
  string
> = {
  manual: "You approve every gate",
  auto_safe: "Aria handles safe steps",
  auto_all: "Aria runs the full lane",
};

export const ARTIFACT_TYPES: ArtifactType[] = [
  "tool",
  "skill",
  "prompt",
  "workflow",
  "mcp_server",
];

export const ARTIFACT_TYPE_LABELS: Record<ArtifactType, string> = {
  tool: "Tool",
  skill: "Skill",
  prompt: "Prompt",
  workflow: "Workflow",
  mcp_server: "MCP Server",
};

// ---------- Draft status ----------

export type DraftStatus =
  | "draft"
  | "validating"
  | "validated"
  | "validation_failed"
  | "testing"
  | "tested"
  | "test_failed"
  | "approved"
  | "publishing"
  | "published"
  | "publish_failed";

// ---------- Session ----------

export interface AssistantSession {
  session_id: string;
  title: string;
  owner: string;
  status: string;
  goal: string;
  metadata_: Record<string, unknown>;
  active_draft_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface AssistantSelectedSkill {
  skill_id: string;
  name: string;
  version: number;
  content_included: boolean;
  selection_reason: string;
}

export interface AssistantSkillRuntimeMetadata {
  mode: SkillRuntimeMode;
  pinned_skill_names: string[];
  disabled_skill_names: string[];
  last_selected_skills: AssistantSelectedSkill[];
}

export interface SessionCreateBody {
  title?: string;
  goal?: string;
  metadata_?: Record<string, unknown>;
  artifact_type?: ArtifactType;
  skill_mode?: SkillRuntimeMode;
  pinned_skill_names?: string[];
  mode?: AssistantMode;
  approval_mode?: AssistantApprovalMode;
}

export interface SessionUpdateBody {
  title?: string;
  status?: "active" | "completed" | "archived";
  metadata_?: Record<string, unknown>;
  skill_mode?: SkillRuntimeMode;
  pinned_skill_names?: string[];
  disabled_skill_names?: string[];
  mode?: AssistantMode;
  approval_mode?: AssistantApprovalMode;
}

// ---------- Message ----------

export interface AssistantMessage {
  message_id: string;
  session_id: string;
  role: "user" | "assistant";
  content: string;
  metadata_: Record<string, unknown>;
  sequence_number: number;
  created_at: string;
}

export type AssistantTaskKind =
  | "answer"
  | "clarify"
  | "build"
  | "plan"
  | "resume";

export interface AssistantTaskContextRef {
  ref_type: string;
  ref_id: string;
  label?: string;
  metadata_?: Record<string, unknown>;
}

export interface AssistantMessageSendBody {
  content: string;
  artifact_type?: string;
  skill_mode?: SkillRuntimeMode;
  skill_names?: string[];
  mode?: AssistantMode;
  steer?: boolean;
  approval_mode?: AssistantApprovalMode;
  constraints?: Record<string, unknown>;
  done_when?: string[];
  context_refs?: AssistantTaskContextRef[];
  current_surface?: string;
  task_kind?: AssistantTaskKind;
  selected_resources?: AssistantTaskContextRef[];
  resume_from_plan_id?: string;
}

// ---------- Message queue ("add to queue" + "steer") ----------

export type QueuedMessageKind = "queued" | "steer";

export interface AssistantQueuedMessage {
  queue_id: string;
  session_id: string;
  content: string;
  mode: AssistantMode;
  kind: QueuedMessageKind;
  position: number;
  status: string;
  created_by: string;
  created_at: string;
}

export interface QueuedMessageCreateBody {
  content: string;
  mode?: AssistantMode;
  kind?: QueuedMessageKind;
}

// ---------- Context attachments ("+ add files") ----------

export type AttachmentKind =
  | "object_file"
  | "upload"
  | "library_resource"
  | "text_snippet";

export type LibraryResourceType =
  | "prompt"
  | "skill"
  | "tool"
  | "workflow"
  | "knowledge_base";

export const LIBRARY_RESOURCE_TYPES: LibraryResourceType[] = [
  "prompt",
  "skill",
  "tool",
  "workflow",
  "knowledge_base",
];

export const ATTACHMENT_KIND_LABELS: Record<AttachmentKind, string> = {
  object_file: "Object file",
  upload: "Upload",
  library_resource: "Library",
  text_snippet: "Text",
};

export interface AssistantAttachment {
  attachment_id: string;
  session_id: string;
  kind: AttachmentKind;
  ref_type: string;
  ref_id: string;
  name: string;
  content_text: string;
  bytes_size: number;
  truncated: boolean;
  metadata_: Record<string, unknown>;
  created_by: string;
  created_at: string;
}

export interface AttachmentCreateBody {
  kind: "object_file" | "library_resource" | "text_snippet";
  bucket?: string;
  key?: string;
  resource_type?: LibraryResourceType;
  resource_id?: string;
  name?: string;
  text?: string;
}

export interface ClarifyingQuestion {
  question: string;
  field: string;
  options: string[];
}

// ---------- Draft ----------

export interface AssistantDraft {
  draft_id: string;
  session_id: string;
  artifact_type: ArtifactType;
  status: DraftStatus;
  title: string;
  summary: string;
  spec: Record<string, unknown>;
  artifact: Record<string, unknown>;
  validation_report: ValidationReport | null;
  test_report: TestReport | null;
  target_registry_id: string | null;
  version: number;
  created_by: string;
  updated_by: string;
  created_at: string;
  updated_at: string;
}

export interface DraftUpdateBody {
  title?: string;
  summary?: string;
  spec?: Record<string, unknown>;
  artifact?: Record<string, unknown>;
  version: number;
}

// ---------- Run ----------

export interface AssistantRun {
  run_id: string;
  session_id: string;
  draft_id: string | null;
  status: string;
  engine: string;
  model: string;
  input_summary: string;
  output_summary: string;
  trace_id: string | null;
  mlflow_run_id: string | null;
  error: string | null;
  started_at: string;
  completed_at: string | null;
}

// ---------- Reports ----------

export interface ValidationReport {
  valid: boolean;
  errors: string[];
  warnings: string[];
}

export interface TestReport {
  passed: boolean;
  total: number;
  failures: number;
  details: Record<string, unknown>[];
  error: string | null;
}

// ---------- Turn response ----------

// ---------- Agent tool calls (the actions Aria took this turn) ----------

export interface AssistantToolCall {
  name: string;
  arguments: Record<string, unknown>;
  result_summary: string;
  ok: boolean;
}

export interface AssistantProcessStep {
  key: string;
  label: string;
  tone?: "neutral" | "success" | "warning" | "error";
}

export interface TurnResponse {
  assistant_message: AssistantMessage;
  questions: ClarifyingQuestion[];
  draft_updates: AssistantDraft[];
  run: AssistantRun | null;
  tool_calls?: AssistantToolCall[];
}

// ---------- Assistant config ----------

export interface AssistantModelOption {
  id: string;
  name: string;
  provider: "openai" | "anthropic" | "ollama";
}

export interface AssistantConfig {
  engine: string;
  model: string;
  provider: "openai" | "anthropic" | "ollama";
  reasoning: string;
  enabled: boolean;
  disabled_intents: string[];
  disabled_domains: string[];
  available_models: AssistantModelOption[];
}

// ---------- Intent workbench ----------

export type AssistantIntentName =
  | "create_tool"
  | "create_skill"
  | "create_workflow"
  | "create_mcp_server"
  | "create_prompt"
  | "edit_prompt"
  | "generate_test_cases"
  | "save_eval_dataset"
  | "run_prompt_optimization"
  | "review_optimization_result"
  | "propose_promotion";

export interface AssistantIntentCandidate {
  name: AssistantIntentName | string;
  confidence: number;
  rationale: string;
}

export interface AssistantIntentSlot {
  name: string;
  value: unknown;
  required: boolean;
  source: "user" | "inferred" | "default" | "memory" | "system";
  confidence: number;
  needs_confirmation: boolean;
}

export interface AssistantPlanAction {
  action: string;
  description: string;
  status: "pending" | "blocked" | "ready";
  mutation_type?:
    | "none"
    | "assistant_metadata"
    | "domain_write"
    | "publish_or_promote";
  result_type?: string;
  required_scopes?: string[];
}

export interface AssistantResultLink {
  label: string;
  resource_type: string;
  id: string;
  path: string;
}

export interface AssistantNextAction {
  intent_name: string;
  label: string;
  slot_overrides: Record<string, unknown>;
  requires_confirmation: boolean;
}

export interface AssistantResultEnvelope {
  result_type?: string;
  status?: string;
  summary?: string;
  ids?: Record<string, unknown>;
  links?: AssistantResultLink[];
  warnings?: string[];
  next_actions?: AssistantNextAction[];
  mlflow_trace_id?: string | null;
  mlflow_run_id?: string | null;
  trace_id?: string;
  correlation_id?: string;
  [key: string]: unknown;
}

export interface AssistantIntentResolveBody {
  content: string;
  context?: Record<string, unknown>;
}

export interface AssistantIntentResolveResult {
  mode: "intent_plan";
  intent: AssistantIntentCandidate;
  alternatives: AssistantIntentCandidate[];
  slots: AssistantIntentSlot[];
  assumptions: string[];
  questions: string[];
  evidence: string[];
}

export interface AssistantIntentPlanBody {
  content?: string;
  intent_name?: AssistantIntentName;
  slot_overrides?: Record<string, unknown>;
  context?: Record<string, unknown>;
}

export interface AssistantIntentPlanResult {
  mode: "intent_plan";
  plan_id: string;
  intent: AssistantIntentCandidate;
  actions: AssistantPlanAction[];
  slots: AssistantIntentSlot[];
  missing_slots: string[];
  assumptions: string[];
  questions: string[];
  ready: boolean;
  requires_confirmation: boolean;
}

export interface AssistantIntentExecuteBody {
  plan_id?: string;
  confirm: boolean;
}

export interface AssistantIntentExecuteResult {
  operation_id: string;
  plan_id: string;
  intent_name: string;
  status: string;
  executed_action: string;
  result: AssistantResultEnvelope;
  run: AssistantRun | null;
}

export interface AssistantOperationStatus {
  operation_id: string;
  session_id: string;
  plan_id: string | null;
  intent_name: string;
  status: string;
  created_at: string;
  updated_at: string | null;
  result: AssistantResultEnvelope;
  run: AssistantRun | null;
}
