"""Pydantic request / response models for the assistant engine and routes."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from caliber.assistant.task_context import AssistantTaskContext, TaskContextRef, TaskKind

# ---------------------------------------------------------------------------
# Artifact type literals
# ---------------------------------------------------------------------------

ArtifactType = Literal["tool", "skill", "prompt", "workflow", "mcp_server"]
SkillRuntimeMode = Literal["auto", "manual", "off"]

ARTIFACT_TYPES: set[str] = {"tool", "skill", "prompt", "workflow", "mcp_server"}

# ---------------------------------------------------------------------------
# Interaction mode (UI-selectable, like a code assistant's Chat/Plan toggle)
# ---------------------------------------------------------------------------
# ``chat``  — answer questions / converse; never emit drafts.
# ``build`` — actively author or modify artifacts (the default authoring flow).
# ``plan``  — outline an approach without writing artifacts.
AssistantMode = Literal["chat", "build", "plan"]
ASSISTANT_MODES: set[str] = {"chat", "build", "plan"}
DEFAULT_ASSISTANT_MODE: str = "build"

# ---------------------------------------------------------------------------
# Approval mode — how far Aria may auto-advance a draft through the
# validate -> test -> approve -> publish gates without an operator click.
# ---------------------------------------------------------------------------
# ``manual``    — Aria proposes; the operator runs each gate (default).
# ``auto_safe`` — Aria auto-validates and auto-tests new drafts; approve/publish
#                 still wait for the operator.
# ``auto_all``  — legacy name: auto-runs safe/mutation tools, but no longer
#                 bypasses the approval boundary.
# ``agent_review`` — an independent approver-scoped agent reviews passing drafts.
# ``full_autonomy`` — agent review followed by a distinct release service.
AssistantApprovalMode = Literal["manual", "auto_safe", "auto_all", "agent_review", "full_autonomy"]
ASSISTANT_APPROVAL_MODES: set[str] = {
    "manual",
    "auto_safe",
    "auto_all",
    "agent_review",
    "full_autonomy",
}
DEFAULT_APPROVAL_MODE: str = "manual"

# ---------------------------------------------------------------------------
# Message queue ("add to queue" + "steer")
# ---------------------------------------------------------------------------
# ``queued`` — a plain follow-up turn dispatched in order after the current one.
# ``steer``  — a priority course-correction that jumps to the front of the queue.
QueuedMessageKind = Literal["queued", "steer"]
QUEUED_MESSAGE_KINDS: set[str] = {"queued", "steer"}

# ---------------------------------------------------------------------------
# Context attachments ("+ add files")
# ---------------------------------------------------------------------------
AttachmentKind = Literal["object_file", "upload", "library_resource", "text_snippet"]
ATTACHMENT_KINDS: set[str] = {"object_file", "upload", "library_resource", "text_snippet"}

LibraryResourceType = Literal["prompt", "skill", "tool", "workflow", "knowledge_base"]
LIBRARY_RESOURCE_TYPES: set[str] = {"prompt", "skill", "tool", "workflow", "knowledge_base"}

# Per-attachment plain-text snapshot cap (characters) injected into the prompt.
ATTACHMENT_TEXT_MAX_CHARS: int = 50_000

# ---------------------------------------------------------------------------
# Draft status state machine
# ---------------------------------------------------------------------------

DraftStatus = Literal[
    "draft",
    "validating",
    "validated",
    "validation_failed",
    "testing",
    "tested",
    "test_failed",
    "reviewing",
    "review_rejected",
    "review_failed",
    "approved",
    "publishing",
    "published",
    "publish_failed",
]

TERMINAL_DRAFT_STATUSES: set[str] = {"published"}

# ---------------------------------------------------------------------------
# Session status
# ---------------------------------------------------------------------------

SessionStatus = Literal["active", "completed", "archived"]

# ---------------------------------------------------------------------------
# Intent-driven planning
# ---------------------------------------------------------------------------

MutationType = Literal["none", "assistant_metadata", "domain_write", "publish_or_promote"]

IntentName = Literal[
    "create_tool",
    "create_skill",
    "create_workflow",
    "create_mcp_server",
    "create_prompt",
    "edit_prompt",
    "generate_test_cases",
    "save_eval_dataset",
    "run_prompt_optimization",
    "review_optimization_result",
    "run_workflow_calibration",
    "review_workflow_calibration_result",
    "propose_promotion",
]

INTENT_NAMES: set[str] = {
    "create_tool",
    "create_skill",
    "create_workflow",
    "create_mcp_server",
    "create_prompt",
    "edit_prompt",
    "generate_test_cases",
    "save_eval_dataset",
    "run_prompt_optimization",
    "review_optimization_result",
    "run_workflow_calibration",
    "review_workflow_calibration_result",
    "propose_promotion",
}

INTENT_DOMAINS: dict[str, str] = {
    "create_tool": "tool",
    "create_skill": "skill",
    "create_workflow": "workflow",
    "create_mcp_server": "mcp_server",
    "create_prompt": "prompt",
    "edit_prompt": "prompt",
    "generate_test_cases": "prompt",
    "save_eval_dataset": "eval_dataset",
    "run_prompt_optimization": "prompt",
    "review_optimization_result": "prompt",
    "run_workflow_calibration": "workflow",
    "review_workflow_calibration_result": "workflow",
    "propose_promotion": "prompt",
}

ASSISTANT_DOMAINS: set[str] = {
    "prompt",
    "eval_dataset",
    "tool",
    "skill",
    "workflow",
    "mcp_server",
}

# ---------------------------------------------------------------------------
# Engine protocol models
# ---------------------------------------------------------------------------


class AssistantTurnRequest(BaseModel):
    """Input to ``AssistantEngine.run_turn``."""

    session_id: str
    user_message: str
    history: list[dict[str, Any]] = Field(default_factory=list)
    drafts: list[dict[str, Any]] = Field(default_factory=list)
    artifact_type: ArtifactType | None = None
    goal: str = ""
    selected_skills: list[dict[str, Any]] = Field(default_factory=list)
    skill_runtime_mode: SkillRuntimeMode = "auto"
    skill_playground: bool = False
    mode: AssistantMode = "build"
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    steer: bool = False
    # Acting user + approval policy let the engine's tool layer enforce RBAC and
    # decide which execute tools are in scope this turn.
    user: str = ""
    approval_mode: AssistantApprovalMode = "manual"
    task_context: AssistantTaskContext = Field(default_factory=AssistantTaskContext)


class AssistantToolCall(BaseModel):
    """One tool the engine invoked during a turn, surfaced for transparency."""

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result_summary: str = ""
    ok: bool = True


class ClarifyingQuestion(BaseModel):
    question: str
    field: str = ""
    options: list[str] = Field(default_factory=list)


class DraftDelta(BaseModel):
    """Incremental change the engine wants to apply to a draft."""

    draft_id: str | None = None
    artifact_type: ArtifactType | None = None
    title: str = ""
    summary: str = ""
    spec: dict[str, Any] = Field(default_factory=dict)
    artifact: dict[str, Any] = Field(default_factory=dict)


class AssistantTurnResult(BaseModel):
    """Output from ``AssistantEngine.run_turn``."""

    reply: str = ""
    questions: list[ClarifyingQuestion] = Field(default_factory=list)
    draft_deltas: list[DraftDelta] = Field(default_factory=list)
    done: bool = False
    error: str | None = None
    trace_id: str | None = None
    tool_calls: list[AssistantToolCall] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Route request / response schemas
# ---------------------------------------------------------------------------


class SessionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="", max_length=256)
    goal: str = Field(default="", max_length=4096)
    metadata_: dict[str, Any] = Field(default_factory=dict)
    artifact_type: ArtifactType | None = None
    skill_mode: SkillRuntimeMode | None = None
    pinned_skill_names: list[str] = Field(default_factory=list)
    mode: AssistantMode | None = None
    approval_mode: AssistantApprovalMode | None = None


class SessionUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=256)
    status: SessionStatus | None = None
    metadata_: dict[str, Any] | None = None
    skill_mode: SkillRuntimeMode | None = None
    pinned_skill_names: list[str] | None = None
    disabled_skill_names: list[str] | None = None
    mode: AssistantMode | None = None
    approval_mode: AssistantApprovalMode | None = None


class MessageSendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=32_000)
    artifact_type: ArtifactType | None = None
    skill_mode: SkillRuntimeMode | None = None
    skill_names: list[str] = Field(default_factory=list)
    mode: AssistantMode | None = None
    steer: bool = False
    approval_mode: AssistantApprovalMode | None = None
    constraints: dict[str, Any] = Field(default_factory=dict)
    done_when: list[str] = Field(default_factory=list)
    context_refs: list[TaskContextRef] = Field(default_factory=list)
    current_surface: str | None = Field(default=None, max_length=64)
    task_kind: TaskKind | None = None
    selected_resources: list[TaskContextRef] = Field(default_factory=list)
    resume_from_plan_id: str | None = Field(default=None, max_length=64)


class AttachmentCreateRequest(BaseModel):
    """JSON body to attach existing context to a session.

    Direct file *uploads* use the multipart ``/attachments/upload`` route instead;
    this schema covers the three by-reference / inline kinds.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["object_file", "library_resource", "text_snippet"]
    # object_file
    bucket: str | None = Field(default=None, max_length=256)
    key: str | None = Field(default=None, max_length=1024)
    # library_resource
    resource_type: LibraryResourceType | None = None
    resource_id: str | None = Field(default=None, max_length=128)
    # text_snippet
    name: str | None = Field(default=None, max_length=512)
    text: str | None = Field(default=None, max_length=200_000)


class AttachmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    attachment_id: str
    session_id: str
    kind: str
    ref_type: str
    ref_id: str
    name: str
    content_text: str
    bytes_size: int
    truncated: bool
    metadata_: dict[str, Any] = Field(default_factory=dict)
    created_by: str
    created_at: datetime


class QueuedMessageCreateRequest(BaseModel):
    """Enqueue a follow-up turn (``queued``) or a priority steer (``steer``)."""

    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=32_000)
    mode: AssistantMode | None = None
    kind: QueuedMessageKind = "queued"


class QueuedMessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    queue_id: str
    session_id: str
    content: str
    mode: str
    kind: str
    position: int
    status: str
    created_by: str
    created_at: datetime


class DraftUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=256)
    summary: str | None = None
    spec: dict[str, Any] | None = None
    artifact: dict[str, Any] | None = None
    version: int = Field(description="Optimistic-concurrency guard")


class IntentResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=32_000)
    context: dict[str, Any] = Field(default_factory=dict)


class IntentPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str | None = Field(default=None, max_length=32_000)
    intent_name: IntentName | None = None
    slot_overrides: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)


class IntentExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str | None = Field(default=None, max_length=64)
    confirm: bool = False


# ---------------------------------------------------------------------------
# Response schemas (serialized from ORM → Pydantic)
# ---------------------------------------------------------------------------


class SessionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    session_id: str
    title: str
    owner: str
    status: str
    goal: str
    metadata_: dict[str, Any] = Field(default_factory=dict)
    active_draft_id: str | None
    created_at: datetime
    updated_at: datetime


class MessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    message_id: str
    session_id: str
    role: str
    content: str
    metadata_: dict[str, Any] = Field(default_factory=dict, alias="metadata_")
    sequence_number: int
    created_at: datetime


class DraftResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    draft_id: str
    session_id: str
    artifact_type: str
    status: str
    title: str
    summary: str
    spec: dict[str, Any]
    artifact: dict[str, Any]
    validation_report: dict[str, Any] | None
    test_report: dict[str, Any] | None
    review_report: dict[str, Any] | None = None
    target_registry_id: str | None
    version: int
    created_by: str
    updated_by: str
    created_at: datetime
    updated_at: datetime


class RunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    run_id: str
    session_id: str
    draft_id: str | None
    status: str
    engine: str
    model: str
    input_summary: str
    output_summary: str
    trace_id: str | None
    mlflow_run_id: str | None
    error: str | None
    started_at: datetime
    completed_at: datetime | None


class IntentCandidate(BaseModel):
    name: str
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = ""


class IntentSlot(BaseModel):
    name: str
    value: Any | None = None
    required: bool = False
    source: Literal["user", "inferred", "default", "memory", "system"] = "inferred"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    needs_confirmation: bool = False


class PlanAction(BaseModel):
    action: str
    description: str
    status: Literal["pending", "blocked", "ready"] = "pending"
    mutation_type: MutationType = "domain_write"
    result_type: str = ""
    required_scopes: list[str] = Field(default_factory=list)


class IntentResolveResponse(BaseModel):
    mode: Literal["intent_plan"] = "intent_plan"
    intent: IntentCandidate
    alternatives: list[IntentCandidate] = Field(default_factory=list)
    slots: list[IntentSlot] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class IntentPlanResponse(BaseModel):
    mode: Literal["intent_plan"] = "intent_plan"
    plan_id: str
    intent: IntentCandidate
    actions: list[PlanAction] = Field(default_factory=list)
    slots: list[IntentSlot] = Field(default_factory=list)
    missing_slots: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    ready: bool = False
    requires_confirmation: bool = True


class IntentExecuteResponse(BaseModel):
    operation_id: str
    plan_id: str
    intent_name: str
    status: str
    executed_action: str
    result: dict[str, Any] = Field(default_factory=dict)
    run: RunResponse | None = None


class OperationStatusResponse(BaseModel):
    operation_id: str
    session_id: str
    plan_id: str | None = None
    intent_name: str
    status: str
    created_at: datetime
    updated_at: datetime | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    run: RunResponse | None = None


class ValidationReport(BaseModel):
    """Result of ``validate_*_draft``."""

    valid: bool = False
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class TestReport(BaseModel):
    """Result of ``run_draft_tests``."""

    passed: bool = False
    total: int = 0
    failures: int = 0
    details: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None


class TurnResponse(BaseModel):
    """Wrapper returned by ``POST .../messages``."""

    assistant_message: MessageResponse
    questions: list[ClarifyingQuestion] = Field(default_factory=list)
    draft_updates: list[DraftResponse] = Field(default_factory=list)
    run: RunResponse | None = None
    tool_calls: list[AssistantToolCall] = Field(default_factory=list)
