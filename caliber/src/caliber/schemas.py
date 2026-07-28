"""Pydantic models for HTTP request/response shapes.

These are deliberately separated from the ORM models in ``caliber.db.models``.
Two reasons:

1. The API surface is part of the public contract; ORM internals are not.
   Routes serialize via these schemas so a DB-column rename can't silently
   change the wire format.
2. Pydantic gives us free validation on request bodies (Phase 2 will need it
   for ``POST /caliber/verification-queue``).

The :class:`Envelope` wrapper standardizes responses as ``{"data": ...}``,
matching the convention documented.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from caliber.assistant.task_context import TaskContextRef

# ---------------------------------------------------------------------------
# Generic envelope shared by every endpoint.
# ---------------------------------------------------------------------------

T = TypeVar("T")


class Envelope(BaseModel, Generic[T]):
    """Standard ``{"data": ...}`` response wrapper used everywhere.

    Picked up from the convention. Pages of items
    add ``next_cursor``; single-item responses use only ``data``.
    """

    model_config = ConfigDict(from_attributes=True)

    data: T
    next_cursor: str | None = None


# ---------------------------------------------------------------------------
# Agent config
# ---------------------------------------------------------------------------


class AgentConfigSchema(BaseModel):
    """Serialized form of :class:`caliber.db.models.CaliberAgentConfig`."""

    model_config = ConfigDict(from_attributes=True)

    agent_id: str
    experiment_id: str
    name: str
    owner: str
    artifact_types: list[str] = Field(default_factory=list)
    eval_thresholds: dict[str, object] = Field(default_factory=dict)
    optimizer_config: dict[str, object] = Field(default_factory=dict)
    approval_policy: dict[str, object] = Field(default_factory=dict)
    optimize_for: str
    collaboration_mode: str | None
    enabled: bool
    required_approvals: int
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Agent config — request bodies
# ---------------------------------------------------------------------------


class AgentRegisterRequest(BaseModel):
    """Body of ``POST /caliber/agents``.

    Operator-facing path for first-time agent registration. Fields default to
    the same shape :class:`AgentConfigSchema` reports back so a round-trip
    GET → POST is a no-op.
    """

    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1, max_length=64)
    experiment_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=256)
    # Ownership is derived from the authenticated actor. Kept in the request
    # shape for backward compatibility, but callers no longer need to send a
    # placeholder value that the route ignores.
    owner: str = Field(default="", max_length=256)
    artifact_types: list[str] = Field(default_factory=list)
    eval_thresholds: dict[str, object] = Field(default_factory=dict)
    optimizer_config: dict[str, object] = Field(default_factory=dict)
    approval_policy: dict[str, object] = Field(default_factory=dict)
    optimize_for: str = Field(default="quality", max_length=16)
    collaboration_mode: str | None = Field(default=None, max_length=32)
    enabled: bool = True
    required_approvals: int = Field(default=1, ge=1)


class AgentUpdateRequest(BaseModel):
    """Body of ``PATCH /caliber/agents/{agent_id}``.

    All fields are optional — clients send only the keys they want to change.
    ``agent_id`` and ``experiment_id`` are intentionally absent: agent identity
    is fixed at registration time. Re-keying an agent means deleting and
    re-creating it (so the audit trail stays clean).
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=256)
    owner: str | None = Field(default=None, min_length=1, max_length=256)
    artifact_types: list[str] | None = None
    eval_thresholds: dict[str, object] | None = None
    optimizer_config: dict[str, object] | None = None
    approval_policy: dict[str, object] | None = None
    optimize_for: str | None = Field(default=None, max_length=16)
    collaboration_mode: str | None = Field(default=None, max_length=32)
    enabled: bool | None = None
    required_approvals: int | None = Field(default=None, ge=1)


# ---------------------------------------------------------------------------
# Verification queue item
# ---------------------------------------------------------------------------


class VerificationItemSchema(BaseModel):
    """Serialized form of :class:`caliber.db.models.CaliberVerificationItem`."""

    model_config = ConfigDict(from_attributes=True)

    item_id: str
    agent_id: str
    assessment_id: str | None
    trace_id: str | None
    experiment_id: str | None
    session_id: str | None
    workflow_id: str | None
    category: str
    free_text: str
    severity: str
    artifact_type_hint: str | None
    artifact_ref: str | None
    submitted_context: dict[str, object] | None
    status: str
    priority: int
    assigned_to: str | None
    verified_by: str | None
    verified_at: datetime | None
    verification_notes: str | None
    refinement_target: str | None
    duplicate_of_id: str | None
    created_at: datetime


# ---------------------------------------------------------------------------
# Audit log — read surface (explorer + export)
# ---------------------------------------------------------------------------


class AuditLogEntrySchema(BaseModel):
    """Serialized form of :class:`caliber.db.models.CaliberAuditLog`."""

    model_config = ConfigDict(from_attributes=True)

    log_id: int
    timestamp: datetime
    actor: str
    action: str
    entity_type: str
    entity_id: str
    details: dict[str, object] | None


class AuditLogPageSchema(BaseModel):
    """A filtered page of audit entries plus the total match count.

    ``total`` reflects every row matching the active filters (ignoring
    ``limit``/``offset``) so the UI can render "showing 100 of 4,213".
    """

    model_config = ConfigDict(extra="forbid")

    entries: list[AuditLogEntrySchema]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Verification queue — request bodies
# ---------------------------------------------------------------------------

_VALID_SEVERITIES = frozenset({"critical", "standard"})


class VerificationItemCreateRequest(BaseModel):
    """Body of ``POST /caliber/verification-queue``.

    The operator path — used when a reviewer wants to flag a concern that is
    not tied to a specific MLflow trace. Trace-linked feedback flows through
    the assessment poller and never hits this endpoint.
    """

    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1, max_length=64)
    category: str = Field(min_length=1, max_length=64)
    free_text: str = Field(min_length=1)
    severity: str = Field(default="standard")
    artifact_type_hint: str | None = Field(default=None, max_length=32)
    artifact_ref: str | None = Field(default=None, max_length=512)
    submitted_context: dict[str, object] | None = None
    session_id: str | None = Field(default=None, max_length=128)
    workflow_id: str | None = Field(default=None, max_length=128)

    @field_validator("severity", mode="before")
    @classmethod
    def _normalize_severity(cls, value: str) -> str:
        """Lower-case + allowlist the severity at parse time.

        Running this through Pydantic's validation pipeline (rather than a
        post-parse helper) means an invalid value surfaces as a structured
        400 via the ValidationError handler, not a 500.
        """
        if not isinstance(value, str):
            raise TypeError("severity must be a string")
        lowered = value.lower()
        if lowered not in _VALID_SEVERITIES:
            raise ValueError(f"severity must be one of {sorted(_VALID_SEVERITIES)}, got {value!r}")
        return lowered


class VerificationItemVerifyRequest(BaseModel):
    """Body of ``POST /caliber/verification-queue/{item_id}/verify``.

    All fields are optional — the minimal verify request is ``{}``. Anything
    the reviewer wanted to record (refinement target override, notes, severity
    reclassification) goes in here.
    """

    model_config = ConfigDict(extra="forbid")

    refinement_target: str | None = Field(default=None, max_length=32)
    verification_notes: str | None = None
    severity: str | None = None


class VerificationItemDismissRequest(BaseModel):
    """Body of ``POST /caliber/verification-queue/{item_id}/dismiss``."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = None
    duplicate_of_id: str | None = Field(default=None, max_length=64)


class VerificationItemDuplicateRequest(BaseModel):
    """Body of ``POST /caliber/verification-queue/{item_id}/duplicate``.

    Dedicated endpoint for the "this is a duplicate of X" decision — same
    underlying mutation as ``dismiss`` with ``duplicate_of_id`` set, but
    a distinct route makes the operator intent unambiguous in audit logs
    and lets the UI use a cleaner copy on the button.
    """

    model_config = ConfigDict(extra="forbid")

    duplicate_of_id: str = Field(min_length=1, max_length=64)
    reason: str | None = None


_VALID_BATCH_ACTIONS = frozenset({"verify", "dismiss"})


class VerificationBatchRequest(BaseModel):
    """Body of ``POST /caliber/verification-queue/batch``.

    Operator-facing convenience for processing a list of pending items in
    one round-trip — used by the Verification Queue UI when a reviewer
    selects multiple rows and clicks "Verify all" or "Dismiss all."

    The response surface is intentionally permissive: per-item failures
    don't fail the whole batch. Each item lands in either ``succeeded``
    or ``failed`` with a per-item reason. Atomic-or-nothing semantics
    would force the reviewer to retry the entire batch on a single bad
    row, which is the wrong UX trade-off here.
    """

    model_config = ConfigDict(extra="forbid")

    action: str = Field(min_length=1)
    item_ids: list[str] = Field(min_length=1, max_length=200)
    # Optional fields, forwarded into the per-item action when present.
    reason: str | None = None
    refinement_target: str | None = Field(default=None, max_length=32)

    @field_validator("action", mode="before")
    @classmethod
    def _normalize_action(cls, value: str) -> str:
        if not isinstance(value, str):
            raise TypeError("action must be a string")
        lowered = value.lower()
        if lowered not in _VALID_BATCH_ACTIONS:
            raise ValueError(f"action must be one of {sorted(_VALID_BATCH_ACTIONS)}, got {value!r}")
        return lowered


class VerificationBatchItemResult(BaseModel):
    """One row in :class:`VerificationBatchResponse.results`."""

    item_id: str
    status: str
    # "succeeded" → the item was verified/dismissed
    # "failed"    → see ``reason`` for why
    reason: str | None = None
    linked_job_id: str | None = None


class VerificationBatchResponse(BaseModel):
    """Response envelope for the batch endpoint."""

    action: str
    requested: int
    succeeded: int
    failed: int
    results: list[VerificationBatchItemResult]


# ---------------------------------------------------------------------------
# Refinement job
# ---------------------------------------------------------------------------


class RefinementJobSchema(BaseModel):
    """Serialized form of :class:`caliber.db.models.CaliberRefinementJob`."""

    model_config = ConfigDict(from_attributes=True)

    job_id: str
    agent_id: str
    workflow_id: str | None
    primary_item_id: str
    mlflow_run_id: str | None
    artifact_type: str
    optimizer_type: str | None
    skill_name: str | None = None
    status: str
    current_stage: str
    attempt_count: int
    error_message: str | None
    total_tokens: int
    cost_usd: float
    bundle_targets: list[dict[str, object]]
    bundle_expansion_count: int
    diagnosis: dict[str, object] | None
    candidate: dict[str, object] | None
    eval_results: dict[str, object] | None
    calibration_spec: dict[str, object] | None = None
    created_at: datetime
    updated_at: datetime


class HarvestedExampleSchema(BaseModel):
    """Eval-dataset example harvested from a verified correction (R2.3)."""

    model_config = ConfigDict(extra="forbid")

    dataset_id: str
    dataset_name: str
    example_id: str
    dataset_version: int


class VerifyResponse(BaseModel):
    """Combined response from the verify endpoint: the updated item + new job.

    ``harvested`` is the eval-dataset example created from the human
    correction (gap-analysis R2.3), or ``None`` when feedback harvesting is
    disabled or produced nothing.
    """

    model_config = ConfigDict(from_attributes=True)

    item: VerificationItemSchema
    job: RefinementJobSchema
    harvested: HarvestedExampleSchema | None = None


# ---------------------------------------------------------------------------
# Workflow calibration
# ---------------------------------------------------------------------------


_WORKFLOW_CALIBRATION_MOVES = frozenset(
    {
        "add_grounding_guardrail",
        "update_tool_constraint",
        "reroute_handoff",
        "relax_soft_guardrail",
    }
)
_WORKFLOW_CALIBRATION_OBJECTIVES = frozenset({"quality", "tool_correctness", "tool_adherence"})


class WorkflowCalibrationObjective(BaseModel):
    model_config = ConfigDict(extra="forbid")

    maximize: str = Field(default="quality")
    epsilon: float = Field(default=0.02, ge=0, le=1)

    @field_validator("maximize")
    @classmethod
    def _known_objective(cls, value: str) -> str:
        if value not in _WORKFLOW_CALIBRATION_OBJECTIVES:
            raise ValueError(
                f"unknown objective {value!r}; expected one of {sorted(_WORKFLOW_CALIBRATION_OBJECTIVES)}"
            )
        return value


class WorkflowCalibrationProtected(BaseModel):
    model_config = ConfigDict(extra="forbid")

    completion_rate: float = Field(default=0.0, ge=0, le=1)
    tool_adherence: float = Field(default=0.0, ge=0, le=1)
    safety: float = Field(default=0.0, ge=0, le=1)


class WorkflowCalibrationBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_candidates: int = Field(default=3, ge=1, le=5)
    max_eval_examples: int = Field(default=20, ge=1, le=50)
    min_examples: int = Field(default=2, ge=1, le=50)


class WorkflowCalibrationDatasetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["deploy_gate"] = "deploy_gate"
    dataset_ref: str | None = Field(default=None, max_length=128)


class WorkflowCalibrationScorerSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Literal["quality_match", "tool_adherence", "completion", "safety"]
    weight: float = Field(default=1.0, ge=0)


class WorkflowCalibrationJudgeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False


class WorkflowCalibrationRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1, max_length=64)
    objective: WorkflowCalibrationObjective = Field(default_factory=WorkflowCalibrationObjective)
    protected: WorkflowCalibrationProtected = Field(default_factory=WorkflowCalibrationProtected)
    budget: WorkflowCalibrationBudget = Field(default_factory=WorkflowCalibrationBudget)
    dataset: WorkflowCalibrationDatasetSpec = Field(default_factory=WorkflowCalibrationDatasetSpec)
    move_set: list[str] = Field(
        default_factory=lambda: [
            "add_grounding_guardrail",
            "update_tool_constraint",
            "reroute_handoff",
            "relax_soft_guardrail",
        ]
    )
    scorers: list[WorkflowCalibrationScorerSpec] = Field(
        default_factory=lambda: [
            WorkflowCalibrationScorerSpec(name="quality_match", weight=1.0),
            WorkflowCalibrationScorerSpec(name="tool_adherence", weight=1.0),
        ]
    )
    judge: WorkflowCalibrationJudgeSpec = Field(default_factory=WorkflowCalibrationJudgeSpec)

    @field_validator("move_set")
    @classmethod
    def _known_moves(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - _WORKFLOW_CALIBRATION_MOVES)
        if unknown:
            raise ValueError(
                f"unknown calibration move(s): {', '.join(unknown)}; "
                f"expected one of {sorted(_WORKFLOW_CALIBRATION_MOVES)}"
            )
        return value


class WorkflowCalibrationOptionsResponse(BaseModel):
    supported_objectives: list[str]
    supported_move_set: list[str]
    scorer_options: list[str]
    default_budget: dict[str, int]
    data: dict[str, object]


class WorkflowCalibrationRunResponse(BaseModel):
    item: VerificationItemSchema
    job: RefinementJobSchema


# ---------------------------------------------------------------------------
# Approval requests
# ---------------------------------------------------------------------------


class ApprovalRequestSchema(BaseModel):
    """Serialized form of :class:`caliber.db.models.CaliberApprovalRequest`."""

    model_config = ConfigDict(from_attributes=True)

    approval_id: str
    job_id: str
    agent_id: str
    workflow_id: str | None = None
    artifact_type: str | None = None
    calibration_spec: dict[str, object] | None = None
    status: str
    eval_results: dict[str, object] | None
    candidate_snapshot: dict[str, object] | None
    diagnosis_snapshot: dict[str, object] | None
    approved_by: str | None
    approved_at: datetime | None
    rejection_reason: str | None
    required_approvals: int = 1
    approve_votes: int = 0
    remaining_approvals: int = 1
    current_user_vote: str | None = None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Approval — request bodies
# ---------------------------------------------------------------------------


class ApprovalApproveRequest(BaseModel):
    """Body of ``POST /caliber/approvals/{id}/approve``.

    Empty by default — the action is "approve as-is." The optional
    ``notes`` field is recorded on the audit log for "approved with caveats"
    cases where the reviewer wants to flag something for the next iteration.
    """

    model_config = ConfigDict(extra="forbid")

    notes: str | None = None


class ApprovalRejectRequest(BaseModel):
    """Body of ``POST /caliber/approvals/{id}/reject``.

    ``reason`` is required because a reject without an explanation is the
    canonical "regret it tomorrow" failure mode. Missing reasons surface
    as a structured 400 via the existing :class:`ValidationError` handler.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1)


class ApprovalRequestChangesRequest(BaseModel):
    """Body of ``POST /caliber/approvals/{id}/request-changes``.

    Like reject, ``notes`` is required. Until a proper retry mechanism
    lands, this endpoint behaves like a reject with a different status —
    the reviewer's feedback is recorded but the job is terminal.
    """

    model_config = ConfigDict(extra="forbid")

    notes: str = Field(min_length=1)


class ApprovalCommentSchema(BaseModel):
    """Serialized form of :class:`caliber.db.models.CaliberApprovalComment`."""

    model_config = ConfigDict(from_attributes=True)

    comment_id: int
    approval_id: str
    author: str
    body: str
    created_at: datetime


class ApprovalCommentCreateRequest(BaseModel):
    """Body of ``POST /caliber/approvals/{id}/comments``."""

    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1)


class RollbackCheckpointSchema(BaseModel):
    """Serialized form of :class:`caliber.db.models.CaliberRollbackCheckpoint`."""

    model_config = ConfigDict(from_attributes=True)

    checkpoint_id: str
    approval_id: str
    agent_id: str
    artifact_type: str
    artifact_name: str
    artifact_ref_before: str | None
    artifact_ref_after: str
    version_before: int | None
    version_after: int | None
    rolled_back_at: datetime | None
    rolled_back_by: str | None
    created_at: datetime


class RollbackResponse(BaseModel):
    """Response payload from a rollback action."""

    checkpoint: RollbackCheckpointSchema
    rotated_to: str
    rotated_at: datetime


class JobTargetSchema(BaseModel):
    """One row in :attr:`JobTargetsResponse.targets`.

    Each row points at one impacted agent + the bundle artifact ref
    that's being optimized for it. The shape mirrors the JSON stored
    in :class:`caliber.db.models.CaliberRefinementJob.bundle_targets`,
    promoted to a typed schema so the UI can build the impacted-agent
    graph without inventing its own keys.
    """

    model_config = ConfigDict(extra="allow")

    agent_id: str
    artifact_type: str
    artifact_ref: str | None = None
    role: str | None = None


class JobTargetsResponse(BaseModel):
    """Body of ``GET /caliber/jobs/{job_id}/targets``.

    The endpoint exists for bundle jobs — single-agent jobs return one
    target (the job's own agent) so the response shape is uniform on the
    UI side. ``bundle_size`` echoes the count for cheap rendering.
    """

    job_id: str
    agent_id: str
    artifact_type: str
    bundle_size: int
    targets: list[JobTargetSchema]


class CSRFTokenResponse(BaseModel):
    """Body of ``GET /caliber/csrf``.

    ``enabled=false`` means CSRF protection is off in this deployment
    (the common path behind an auth-handling proxy). The SPA reads
    this flag on boot and decides whether to send the
    ``X-CALIBER-CSRF`` header on subsequent writes.
    """

    enabled: bool
    token: str | None
    ttl_seconds: int


class AssistantSloSummarySchema(BaseModel):
    """Assistant intent/runtime quality metrics for the dashboard."""

    intent_confidence_avg: float | None = None
    plans_total: int = 0
    plans_ready: int = 0
    plan_readiness_rate: float = 0.0
    clarification_rate: float = 0.0
    executions_total: int = 0
    executions_completed: int = 0
    executions_failed: int = 0
    executions_blocked: int = 0
    execution_success_rate: float = 0.0
    adapter_error_classes: dict[str, int] = Field(default_factory=dict)
    publish_total: int = 0
    publish_success: int = 0
    publish_failed: int = 0
    publish_success_rate: float = 0.0


class DashboardSummarySchema(BaseModel):
    """Aggregated counts for the Overview page (top of the SPA).

    All counts are computed in a single read transaction so they're
    point-in-time consistent — the UI never renders "5 pending approvals,
    0 jobs running" when both should reflect the same moment.
    """

    agents_total: int
    agents_enabled: int

    verification_pending: int
    verification_pending_critical: int

    jobs_queued: int
    jobs_running: int
    jobs_awaiting_approval: int
    jobs_completed: int
    jobs_failed: int
    jobs_rejected: int

    approvals_pending: int

    assistant_slo: AssistantSloSummarySchema = Field(default_factory=AssistantSloSummarySchema)
    generated_at: datetime


class BatchApproveRequest(BaseModel):
    """Body of ``POST /caliber/approvals/batch-approve``.

    Admin-only fast path for resolving a list of approvals in one
    round-trip. Bypasses the quorum check (admins are trusted to
    override). ``notes`` is recorded on every audit row so the trail
    explains the override after the fact.
    """

    model_config = ConfigDict(extra="forbid")

    approval_ids: list[str] = Field(min_length=1, max_length=100)
    notes: str | None = None


class BatchApproveItemResult(BaseModel):
    """One row in :class:`BatchApproveResponse.results`."""

    approval_id: str
    status: str  # "succeeded" | "skipped" | "failed"
    reason: str | None = None
    artifact_ref: str | None = None
    job_id: str | None = None


class BatchApproveResponse(BaseModel):
    """Aggregate result of a batch-approve call."""

    requested: int
    succeeded: int
    skipped: int
    failed: int
    results: list[BatchApproveItemResult]


class ApprovalActionResponse(BaseModel):
    """Combined response from approve/reject/request-changes endpoints.

    Returns the updated approval, the updated job, and (only on approve)
    the :class:`PromotionResult` summary so callers can confirm the
    artifact ref the promoter rotated to.
    """

    model_config = ConfigDict(from_attributes=True)

    approval: ApprovalRequestSchema
    job: RefinementJobSchema
    promotion: dict[str, object] | None = None


class RegressionRunSchema(BaseModel):
    """Serialized form of a persisted regression replay run."""

    model_config = ConfigDict(from_attributes=True)

    run_id: str
    job_id: str
    approval_id: str | None
    agent_id: str
    candidate_hash: str
    status: str
    required_for_approval: bool
    failure_reason: str | None
    dataset_ids: list[str] = Field(default_factory=list)
    trace_sample_ids: list[str] = Field(default_factory=list)
    baseline_scores: dict[str, object] | None
    candidate_scores: dict[str, object]
    deltas: dict[str, object]
    regressions: list[dict[str, object]] = Field(default_factory=list)
    gate: dict[str, object]
    created_at: datetime
    completed_at: datetime | None


class PromptOptimizationScorerRequest(BaseModel):
    """One scorer requested for a manual prompt calibration run."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    weight: float = Field(default=1.0, ge=0)
    config: dict[str, object] = Field(default_factory=dict)


class PromptOptimizationGateRequest(BaseModel):
    """Optional gate overrides for a manual prompt calibration run."""

    model_config = ConfigDict(extra="forbid")

    min_aggregate_score: float | None = Field(default=None, ge=0, le=1)
    max_regression_delta: float | None = Field(default=None, ge=0)


class PromptOptimizationRunRequest(BaseModel):
    """Body of ``POST /caliber/prompts/{optimization,calibration}/runs``."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1, max_length=64)
    eval_dataset_id: str = Field(min_length=1, max_length=64)
    # Pin the eval-dataset version so the run stays reproducible: a later edit
    # to the dataset (which bumps ``CaliberEvalDataset.version``) won't change
    # what "the dataset" meant for this run. ``None`` means "pin the dataset's
    # current version at launch"; an explicit value must be in range.
    eval_dataset_version: int | None = Field(default=None, ge=1, le=2**31 - 1)
    optimizer_type: str = Field(min_length=1, max_length=32)
    scorers: list[PromptOptimizationScorerRequest] = Field(min_length=1)
    prompt_alias: str | None = Field(default=None, max_length=32)
    gate: PromptOptimizationGateRequest | None = None
    notes: str | None = None


class PromptOptimizationRunResponse(BaseModel):
    """Created verification item and refinement job for a prompt calibration run."""

    item: VerificationItemSchema
    job: RefinementJobSchema


# ---------------------------------------------------------------------------
# Ad-hoc prompt-test runs (durable run history for the Prompts tab runner)
# ---------------------------------------------------------------------------


class PromptTestCaseResult(BaseModel):
    """One judged test case inside a persisted prompt-test run.

    Mirrors the browser-side ``TestResult`` shape. ``verdict`` is constrained to
    the LLM-judge vocabulary and ``score`` to a normalized 0-1 range so a
    corrupt client payload can't poison the durable history.
    """

    model_config = ConfigDict(extra="forbid")

    # camelCase intentionally mirrors the browser ``TestResult`` shape so the
    # FE payload round-trips byte-for-byte without an aliasing layer.
    testCaseId: str = Field(min_length=1, max_length=256)  # noqa: N815
    input: str
    expectedBehavior: str = ""  # noqa: N815
    actualResponse: str = ""  # noqa: N815
    verdict: Literal["pass", "fail", "partial"]
    score: float = Field(ge=0, le=1)
    reasoning: str = ""


class PromptTestRunCreateRequest(BaseModel):
    """Body of ``POST /caliber/prompts/test-runs``.

    The client sends only the prompt-identity snapshot and the per-case
    ``results``; the server recomputes ``test_set_size``, the pass/fail/partial
    counts, and ``overall_score`` from ``results`` rather than trusting any
    client-supplied aggregates.
    """

    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1, max_length=64)
    prompt_name: str = Field(default="", max_length=256)
    prompt_alias: str | None = Field(default=None, max_length=64)
    prompt_version: int | None = Field(default=None, ge=0, le=2**31 - 1)
    model: str | None = Field(default=None, max_length=256)
    eval_dataset_id: str | None = Field(default=None, max_length=64)
    results: list[PromptTestCaseResult]
    trace_id: str | None = Field(default=None, max_length=64)
    mlflow_run_id: str | None = Field(default=None, max_length=64)


class PromptTestRunSummary(BaseModel):
    """History-list row for a persisted prompt-test run (no per-case array)."""

    model_config = ConfigDict(from_attributes=True)

    test_run_id: str
    agent_id: str
    prompt_name: str
    prompt_alias: str | None
    prompt_version: int | None
    model: str | None
    eval_dataset_id: str | None
    test_set_size: int
    passed_count: int
    failed_count: int
    partial_count: int
    overall_score: float | None
    trace_id: str | None
    mlflow_run_id: str | None
    created_by: str
    status: str
    created_at: datetime
    completed_at: datetime | None


class PromptTestRunDetail(PromptTestRunSummary):
    """Full prompt-test run including the per-case ``results`` array."""

    results: list[PromptTestCaseResult] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Prompt workspace + bind ("pytest for prompts")
# ---------------------------------------------------------------------------


class PromptWorkspaceLastRun(BaseModel):
    """Compact summary of the latest prompt-test run for a prompt."""

    model_config = ConfigDict(from_attributes=True)

    test_run_id: str
    overall_score: float | None = None
    test_set_size: int = 0
    passed_count: int = 0
    failed_count: int = 0
    partial_count: int = 0
    created_at: datetime


class PromptWorkspaceResponse(BaseModel):
    """Body of ``GET /caliber/prompts/{name}/workspace``.

    Surfaces the auto-provisioned target's runtime facts plus the computed
    lifecycle ``status`` (Bound > Calibrated > Tested > Has test set > Draft).
    """

    model_config = ConfigDict(from_attributes=True)

    model: str | None = None
    version: int | None = None
    status: str
    bound_to: dict[str, Any] | None = None
    dataset_id: str | None = None
    last_run: PromptWorkspaceLastRun | None = None
    baseline_run_id: str | None = None
    baseline_run: PromptWorkspaceLastRun | None = None


class PromptBaselineRequest(BaseModel):
    """Body of ``POST /caliber/prompts/{name}/baseline``.

    Pins one persisted prompt-test run as the comparison baseline for the prompt.
    The run must belong to this prompt (its ``agent_id`` must equal ``name``).
    """

    model_config = ConfigDict(extra="forbid")

    test_run_id: str = Field(min_length=1, max_length=64)


class PromptBindRequest(BaseModel):
    """Body of ``POST /caliber/prompts/{name}/bind``.

    ``kind`` selects the bind target; the matching ids are required for each
    kind (``agent`` needs ``agent_id``; ``workflow_node`` needs ``workflow_id``
    and ``node_id``; ``standalone`` needs nothing).
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["agent", "workflow_node", "standalone"]
    agent_id: str | None = Field(default=None, max_length=64)
    workflow_id: str | None = Field(default=None, max_length=128)
    node_id: str | None = Field(default=None, max_length=128)


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------


TOOL_SIDE_EFFECT_PATTERN = "^(read|write|external_action)$"
TOOL_STATUS_PATTERN = "^(active|deprecated|archived)$"


class ToolSchema(BaseModel):
    """Serialized form of :class:`caliber.db.models.CaliberToolRegistry`."""

    model_config = ConfigDict(from_attributes=True)

    tool_id: str
    name: str
    version: str
    description: str
    module_path: str
    callable_name: str
    input_schema: dict[str, object] | None
    output_schema: dict[str, object] | None
    side_effect_level: str
    requires_approval: bool
    allow_in_preview: bool
    secret_refs: list[str] = Field(default_factory=list)
    test_cases: list[dict[str, object]] = Field(default_factory=list)
    last_calibration: dict[str, object] | None = None
    owner: str
    status: str
    deprecated_at: datetime | None
    successor_tool_id: str | None
    created_at: datetime
    updated_at: datetime


class ToolRegisterRequest(BaseModel):
    """Body of ``POST /caliber/tools``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=32)
    description: str = Field(default="", max_length=2048)
    module_path: str = Field(min_length=1, max_length=512)
    callable_name: str = Field(min_length=1, max_length=128)
    input_schema: dict[str, object] | None = None
    output_schema: dict[str, object] | None = None
    side_effect_level: str = Field(default="read", pattern=TOOL_SIDE_EFFECT_PATTERN)
    requires_approval: bool = False
    allow_in_preview: bool = False
    secret_refs: list[str] = Field(default_factory=list)
    owner: str = Field(default="", max_length=256)


class ToolUpdateRequest(BaseModel):
    """Body of ``PATCH /caliber/tools/{tool_id}``."""

    model_config = ConfigDict(extra="forbid")

    description: str | None = Field(default=None, max_length=2048)
    side_effect_level: str | None = Field(default=None, pattern=TOOL_SIDE_EFFECT_PATTERN)
    requires_approval: bool | None = None
    allow_in_preview: bool | None = None
    owner: str | None = Field(default=None, max_length=256)
    status: str | None = Field(default=None, pattern=TOOL_STATUS_PATTERN)
    successor_tool_id: str | None = Field(default=None, max_length=64)


# ---------------------------------------------------------------------------
# Durable tool-test runs (run history for the Tools tab) + workspace/baseline
# ---------------------------------------------------------------------------
#
# The tool analog of the prompt test-run/workspace/baseline shapes above. One
# persisted run can come from any of three surfaces (``kind``): the Sandbox
# single-invoke, the saved-fixtures suite, or the LLM-judged hardening pass.

TOOL_TEST_RUN_KIND_PATTERN = "^(sandbox|suite|hardening)$"


class ToolTestCaseResult(BaseModel):
    """One judged case inside a persisted tool-test run.

    ``verdict`` is constrained to the judge vocabulary and ``score`` to a
    normalized 0-1 range so a corrupt client payload can't poison the durable
    history. ``input``/``output`` are free-form objects; ``output`` may be any
    JSON value (or null on error).
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=256)
    input: dict[str, Any] = Field(default_factory=dict)
    output: Any | None = None
    error: str | None = None
    verdict: Literal["pass", "fail", "partial"]
    score: float = Field(ge=0, le=1)
    duration_ms: float = 0.0
    reasoning: str = ""


class ToolTestRunCreateRequest(BaseModel):
    """Body of ``POST /caliber/tools/test-runs``.

    The client sends the tool id, an optional ``kind``/``tool_version``
    snapshot, and the per-case ``results``; the server recomputes
    ``test_set_size``, the pass/fail/partial counts, and ``overall_score`` from
    ``results`` rather than trusting any client-supplied aggregates.
    """

    model_config = ConfigDict(extra="forbid")

    tool_id: str = Field(min_length=1, max_length=64)
    kind: str = Field(default="suite", pattern=TOOL_TEST_RUN_KIND_PATTERN)
    tool_version: str | None = Field(default=None, max_length=32)
    results: list[ToolTestCaseResult]
    trace_id: str | None = Field(default=None, max_length=64)
    mlflow_run_id: str | None = Field(default=None, max_length=64)


class ToolTestRunSummary(BaseModel):
    """History-list row for a persisted tool-test run (no per-case array)."""

    model_config = ConfigDict(from_attributes=True)

    test_run_id: str
    tool_id: str
    tool_version: str | None
    kind: str
    test_set_size: int
    passed_count: int
    failed_count: int
    partial_count: int
    overall_score: float | None
    trace_id: str | None
    mlflow_run_id: str | None
    created_by: str
    status: str
    created_at: datetime
    completed_at: datetime | None


class ToolTestRunDetail(ToolTestRunSummary):
    """Full tool-test run including the per-case ``results`` array."""

    results: list[ToolTestCaseResult] = Field(default_factory=list)


class ToolWorkspaceLastRun(BaseModel):
    """Compact summary of the latest tool-test run for a tool."""

    model_config = ConfigDict(from_attributes=True)

    test_run_id: str
    kind: str = "suite"
    overall_score: float | None = None
    test_set_size: int = 0
    passed_count: int = 0
    failed_count: int = 0
    partial_count: int = 0
    created_at: datetime


class ToolWorkspaceResponse(BaseModel):
    """Body of ``GET /caliber/tools/{tool_id}/workspace``.

    Surfaces the tool's runtime facts plus the computed lifecycle pill
    (Published > Hardened > Tested > Has fixtures > Draft).
    """

    model_config = ConfigDict(from_attributes=True)

    version: str
    side_effect_level: str
    status: str
    lifecycle: str
    last_run: ToolWorkspaceLastRun | None = None
    baseline_run_id: str | None = None
    baseline_run: ToolWorkspaceLastRun | None = None
    has_fixtures: bool = False
    last_calibration_score: float | None = None


class ToolBaselineRequest(BaseModel):
    """Body of ``POST /caliber/tools/{tool_id}/baseline``.

    Pins one persisted tool-test run as the comparison baseline for the tool.
    The run must belong to this tool (its ``tool_id`` must equal the path id).
    """

    model_config = ConfigDict(extra="forbid")

    test_run_id: str = Field(min_length=1, max_length=64)


# ---------------------------------------------------------------------------
# Component calibration (tools + MCP tools)
# ---------------------------------------------------------------------------
#
# A calibration runs a saved set of test cases through the component's existing
# invocation path, scores each, and reports an aggregate pass-rate. The same
# shapes back both the tool registry and the MCP tool calibration endpoints.


class CalibrationAssertion(BaseModel):
    """How a single calibration case is judged against the invocation output.

    * ``no_error`` — passes when the invocation returned no error (default).
    * ``output_contains`` — passes when the stringified output contains ``value``.
    * ``equals`` — passes when the stringified output equals ``value``.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["no_error", "output_contains", "equals"] = "no_error"
    value: str | None = None

    @model_validator(mode="after")
    def _require_value_when_comparing(self) -> CalibrationAssertion:
        if self.type in ("output_contains", "equals") and self.value is None:
            raise ValueError(f"assertion type {self.type!r} requires a 'value'")
        return self


class CalibrationCase(BaseModel):
    """One saved calibration test case for a tool or MCP tool."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=256)
    input: dict[str, object] = Field(default_factory=dict)
    assertion: CalibrationAssertion = Field(default_factory=CalibrationAssertion)


class CalibrationCaseResult(BaseModel):
    """Scored outcome for one calibration case."""

    model_config = ConfigDict(extra="forbid")

    name: str
    passed: bool
    output: object | None = None
    error: str | None = None
    duration_ms: float = 0.0


class CalibrationResult(BaseModel):
    """Aggregate scored result of a calibration run."""

    model_config = ConfigDict(extra="forbid")

    pass_rate: float = Field(ge=0.0, le=1.0)
    total: int = Field(ge=0)
    passed: int = Field(ge=0)
    cases: list[CalibrationCaseResult] = Field(default_factory=list)
    ran_at: datetime | None = None


class ToolTestCasesUpdateRequest(BaseModel):
    """Body of ``PUT /caliber/tools/{tool_id}/test-cases``."""

    model_config = ConfigDict(extra="forbid")

    test_cases: list[CalibrationCase] = Field(default_factory=list, max_length=200)


class ToolTestCasesResponse(BaseModel):
    """Body of ``PUT /caliber/tools/{tool_id}/test-cases``."""

    model_config = ConfigDict(extra="forbid")

    tool_id: str
    test_cases: list[CalibrationCase] = Field(default_factory=list)


class ToolCalibrationResponse(CalibrationResult):
    """Body of ``POST /caliber/tools/{tool_id}/calibrate``."""

    tool_id: str


class McpToolTestCasesUpdateRequest(BaseModel):
    """Body of ``PUT /caliber/mcp-servers/{id}/tools/{tool}/test-cases``."""

    model_config = ConfigDict(extra="forbid")

    test_cases: list[CalibrationCase] = Field(default_factory=list, max_length=200)


class McpToolTestCasesResponse(BaseModel):
    """Body of ``PUT /caliber/mcp-servers/{id}/tools/{tool}/test-cases``."""

    model_config = ConfigDict(extra="forbid")

    server_id: str
    tool_name: str
    test_cases: list[CalibrationCase] = Field(default_factory=list)


class McpToolCalibrationResponse(CalibrationResult):
    """Body of ``POST /caliber/mcp-servers/{id}/tools/{tool}/calibrate``."""

    server_id: str
    tool_name: str


# ---------------------------------------------------------------------------
# Workflow Studio
# ---------------------------------------------------------------------------


WORKFLOW_STATUS_PATTERN = "^(active|paused|archived)$"
WORKFLOW_VERSION_STATUS_PATTERN = "^(draft|published|archived)$"
WORKFLOW_DEPLOYMENT_STATUS_PATTERN = "^(active|archived)$"
WORKFLOW_PROMOTION_STATUS_PATTERN = "^(pending|approved|rejected)$"
WORKFLOW_BENCHMARK_REPORT_STATUS_PATTERN = "^(draft|completed|archived)$"
WORKFLOW_BAKEOFF_SCENARIO_STATUS_PATTERN = "^(not_started|in_progress|passed|blocked)$"
WORKFLOW_BAKEOFF_RUBRIC_SCORE_PATTERN = "^(|1|2|3|4|5)$"


class WorkflowSchema(BaseModel):
    """Serialized form of :class:`caliber.db.models.CaliberWorkflow`."""

    model_config = ConfigDict(from_attributes=True)

    workflow_id: str
    project_id: str | None = None
    name: str
    description: str
    owner: str
    status: str
    default_experiment_id: str | None
    created_at: datetime
    updated_at: datetime


class WorkflowCreateRequest(BaseModel):
    """Body of ``POST /caliber/workflows``."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str | None = Field(default=None, min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    description: str = Field(default="", max_length=4096)
    owner: str = Field(default="", max_length=256)
    default_experiment_id: str | None = Field(default=None, max_length=64)


class WorkflowUpdateRequest(BaseModel):
    """Body of ``PATCH /caliber/workflows/{workflow_id}``."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=4096)
    owner: str | None = Field(default=None, max_length=256)
    status: str | None = Field(default=None, pattern=WORKFLOW_STATUS_PATTERN)
    default_experiment_id: str | None = Field(default=None, max_length=64)


class WorkflowImportRequest(BaseModel):
    """Body of ``POST /caliber/workflows/import``."""

    model_config = ConfigDict(extra="forbid")

    manifest: dict[str, object] | None = None
    manifest_yaml: str | None = None
    name: str | None = Field(default=None, min_length=1, max_length=256)
    owner: str | None = Field(default=None, max_length=256)


class WorkflowVersionSchema(BaseModel):
    """Serialized form of :class:`caliber.db.models.CaliberWorkflowVersion`."""

    model_config = ConfigDict(from_attributes=True)

    version_id: str
    workflow_id: str
    version_number: int
    status: str
    manifest: dict[str, object]
    manifest_hash: str
    compiler_version: str | None
    compiled_artifact_uri: str | None
    validation_report: dict[str, object] | None
    compiled_bundle: dict[str, object] | None
    created_by: str
    created_at: datetime
    published_by: str | None
    published_at: datetime | None


class WorkflowVersionCreateRequest(BaseModel):
    """Body of ``POST /caliber/workflows/{workflow_id}/versions``."""

    model_config = ConfigDict(extra="forbid")

    manifest: dict[str, object] = Field(default_factory=dict)


class WorkflowVersionUpdateRequest(BaseModel):
    """Body of ``PATCH /caliber/workflow-versions/{version_id}``."""

    model_config = ConfigDict(extra="forbid")

    manifest: dict[str, object] = Field(default_factory=dict)
    manifest_hash: str = Field(min_length=1, max_length=64)


class PreviewRunRequest(BaseModel):
    """Body of ``POST /caliber/workflow-versions/{version_id}/preview-run``."""

    model_config = ConfigDict(extra="forbid")

    input: object = None
    session_id: str | None = Field(default=None, max_length=128)
    # Optional in-memory manifest to preview instead of the stored version —
    # lets the copilot iterate loop run an unsaved canvas edit. Not persisted.
    manifest: dict[str, object] | None = None


class WorkflowRunRequest(BaseModel):
    """Body of ``POST /caliber/workflow-versions/{version_id}/run``."""

    model_config = ConfigDict(extra="forbid")

    input: object = None
    session_id: str | None = Field(default=None, max_length=128)
    alias: str | None = Field(default="manual", max_length=64)
    manifest: dict[str, object] | None = None
    # File inputs (storage doc §4.8). Each item references either a staged
    # upload (``{"file_id": ...}``) or a read-only dataset file
    # (``{"file_ref": ...}``). Runtime binds them into the run ``input/``.
    input_files: list[dict[str, str]] | None = Field(default=None)


class WorkflowRunCreateRequest(BaseModel):
    """Body of ``POST /caliber/workflow-runs`` (queue-based async submission)."""

    model_config = ConfigDict(extra="forbid")

    workflow_version_id: str | None = Field(default=None, max_length=64)
    workflow_id: str | None = Field(default=None, max_length=128)
    alias: str | None = Field(default="manual", max_length=64)
    input: object = None
    session_id: str | None = Field(default=None, max_length=128)
    source: str = Field(default="manual", max_length=32)
    priority: int = Field(default=0, ge=0, le=1000)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    manifest: dict[str, object] | None = None
    input_files: list[dict[str, str]] | None = Field(default=None)

    @model_validator(mode="after")
    def _validate_target(self) -> WorkflowRunCreateRequest:
        if self.workflow_version_id:
            return self
        if self.workflow_id and self.alias:
            return self
        raise ValueError(
            "provide either workflow_version_id, or (workflow_id + alias) for async runs"
        )


class WorkflowTriggerRequest(BaseModel):
    """Body of ``POST /caliber/workflows/{id}/trigger`` (event-triggered run)."""

    model_config = ConfigDict(extra="forbid")

    alias: str | None = Field(default=None, min_length=1, max_length=64)
    event_name: str | None = Field(default=None, max_length=128)
    input: object = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


class WorkflowRunCancelRequest(BaseModel):
    """Body of ``POST /caliber/workflow-runs/{run_id}/cancel``."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = None


class WorkflowRunRetryRequest(BaseModel):
    """Body of ``POST /caliber/workflow-runs/{run_id}/retry``."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = None
    checkpoint_id: str | None = Field(default=None, min_length=1, max_length=64)


class WorkflowRunResumeRequest(BaseModel):
    """Body of ``POST /caliber/workflow-runs/{run_id}/resume``."""

    model_config = ConfigDict(extra="forbid")

    event_name: str | None = Field(default=None, max_length=128)
    event_payload: object = None


class WorkflowRunResumeByEventRequest(BaseModel):
    """Body of ``POST /caliber/workflow-runs/resume-by-event``."""

    model_config = ConfigDict(extra="forbid")

    event_name: str = Field(min_length=1, max_length=128)
    event_payload: object = None
    workflow_id: str | None = Field(default=None, min_length=1, max_length=64)


class WorkflowRunApprovalDecisionRequest(BaseModel):
    """Body of runtime approval decision routes for workflow runs."""

    model_config = ConfigDict(extra="forbid")

    runtime_approval_id: str | None = Field(default=None, min_length=1, max_length=64)
    reason: str | None = None


class ProposePatchRequest(BaseModel):
    """Body of ``POST /caliber/workflow-versions/{version_id}/propose-patch``."""

    model_config = ConfigDict(extra="forbid")

    evidence: dict[str, object] = Field(default_factory=dict)
    job_id: str | None = Field(default=None, max_length=64)


class CopilotEditRequest(BaseModel):
    """Body of ``POST /caliber/workflow-versions/{version_id}/copilot-edit``.

    ``instruction`` is the user's natural-language request. ``manifest`` is the
    optional in-progress canvas state — when present it is used as the base
    (so unsaved edits are respected); otherwise the stored version manifest is.
    """

    model_config = ConfigDict(extra="forbid")

    instruction: str = Field(min_length=1, max_length=4000)
    manifest: dict[str, object] | None = None


class PlanBuildRequest(BaseModel):
    """Body of ``POST …/workflow-versions/{version_id}/plan-build``.

    ``goal`` is the user's plain-language description of the workflow to author
    (plan-to-build). ``manifest`` is the optional current canvas — used as the
    diff base (so the proposal shows what plan-to-build *adds*) and for identity
    fields; when absent the stored version manifest is used.
    """

    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, max_length=4000)
    manifest: dict[str, object] | None = None


class WorkflowPatchSchema(BaseModel):
    """Serialized form of :class:`caliber.db.models.CaliberWorkflowPatch`."""

    model_config = ConfigDict(from_attributes=True)

    patch_id: str
    job_id: str | None
    workflow_id: str
    base_version_id: str
    candidate_manifest: dict[str, object]
    semantic_ops: list[dict[str, object]] = Field(default_factory=list)
    patch_summary: str
    graph_diff: dict[str, object] | None
    risk_summary: str
    created_at: datetime


class WorkflowRunSchema(BaseModel):
    """Serialized form of :class:`caliber.db.models.CaliberWorkflowRun`."""

    model_config = ConfigDict(from_attributes=True)

    workflow_run_id: str
    workflow_id: str
    project_id: str | None = None
    tenant_id: str | None = None
    workflow_version_id: str | None
    deployment_alias: str | None
    mlflow_run_id: str | None
    trace_id: str | None
    session_id: str | None
    status: str
    source: str | None = None
    priority: int | None = None
    queued_at: datetime | None = None
    started_at: datetime | None
    completed_at: datetime | None
    claimed_by: str | None = None
    claimed_at: datetime | None = None
    lease_expires_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    attempt_number: int | None = None
    parent_run_id: str | None = None
    cancel_requested_at: datetime | None = None
    cancel_requested_by: str | None = None
    cancel_reason: str | None = None
    current_node_id: str | None = None
    idempotency_key: str | None = None
    input_file_ref: str | None = None
    error_code: str | None = None
    error_summary: str | None = None
    summary: dict[str, object] | None


class WorkflowRunHistoryArtifactStatsSchema(BaseModel):
    """Exact artifact-persistence counts across workflow run history."""

    model_config = ConfigDict(extra="forbid")

    failed: int = 0
    persisted: int = 0


class WorkflowRunHistoryStatsSchema(BaseModel):
    """Exact counts for one workflow's run-history triage surface."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    total_runs: int = 0
    matching_runs: int = 0
    waiting_event_runs: int = 0
    artifact_persistence: WorkflowRunHistoryArtifactStatsSchema = Field(
        default_factory=WorkflowRunHistoryArtifactStatsSchema
    )


class WorkflowRunLineageSchema(BaseModel):
    """Canonical retry lineage for one workflow run."""

    model_config = ConfigDict(extra="forbid")

    workflow_run_id: str
    root_run_id: str
    total_attempts: int = 1
    parent_count: int = 0
    child_count: int = 0
    missing_parent_id: str | None = None
    truncated: bool = False
    runs: list[WorkflowRunSchema] = Field(default_factory=list)


class WorkflowRunManifestSchema(BaseModel):
    """Effective manifest used by one workflow run."""

    model_config = ConfigDict(extra="forbid")

    workflow_run_id: str
    workflow_id: str
    workflow_version_id: str | None = None
    manifest_mode: Literal["saved_version", "snapshot"]
    manifest_hash: str
    manifest: dict[str, object]


class WorkflowRunTraceSpanSchema(BaseModel):
    """One span in a workflow run's MLflow trace (in-app trace viewer)."""

    model_config = ConfigDict(extra="forbid")

    span_id: str | None = None
    parent_id: str | None = None
    name: str
    span_type: str
    start_time_ms: float | None = None
    end_time_ms: float | None = None
    duration_ms: float | None = None
    status: str
    inputs: object = None
    outputs: object = None
    attributes: dict[str, object] = Field(default_factory=dict)


class WorkflowRunTraceSchema(BaseModel):
    """Response body of ``GET /caliber/workflow-runs/{run_id}/trace``.

    ``spans`` is empty (and ``trace_id`` ``None``) when the run has no MLflow
    trace — e.g. the fake provider / tracing off / MLflow not installed — so the
    viewer renders a friendly empty state rather than failing.
    """

    model_config = ConfigDict(extra="forbid")

    trace_id: str | None = None
    spans: list[WorkflowRunTraceSpanSchema] = Field(default_factory=list)
    mlflow_url: str | None = None


class WorkflowRunEventSchema(BaseModel):
    """Serialized form of :class:`caliber.db.models.CaliberWorkflowRunEvent`."""

    model_config = ConfigDict(from_attributes=True)

    event_id: int
    workflow_run_id: str
    project_id: str | None
    sequence: int
    event_type: str
    node_id: str | None
    payload: dict[str, object] | None
    created_at: datetime


class WorkflowRunCheckpointSchema(BaseModel):
    """Serialized form of :class:`caliber.db.models.CaliberWorkflowRunCheckpoint`."""

    model_config = ConfigDict(from_attributes=True)

    checkpoint_id: str
    workflow_run_id: str
    project_id: str | None
    sequence: int
    node_id: str
    state_blob: dict[str, object] | None
    created_at: datetime


class WorkflowSessionMemoryEntrySchema(BaseModel):
    """Persisted conversation history for one workflow agent session."""

    model_config = ConfigDict(from_attributes=True)

    workflow_id: str
    node_id: str
    session_id: str
    message_history: list[dict[str, str]] = Field(default_factory=list)
    message_count: int = 0
    turn_count: int = 0
    created_at: datetime
    updated_at: datetime
    last_user_message: str | None = None
    last_assistant_message: str | None = None


class WorkflowSessionMemoryClearResultSchema(BaseModel):
    """Summary returned after clearing workflow session memory rows."""

    model_config = ConfigDict(from_attributes=True)

    workflow_id: str
    session_id: str
    node_id: str | None = None
    deleted_entries: int = 0
    deleted_messages: int = 0


class WorkflowRuntimeApprovalSchema(BaseModel):
    """Serialized form of :class:`caliber.db.models.CaliberRuntimeApprovalRequest`."""

    model_config = ConfigDict(from_attributes=True)

    runtime_approval_id: str
    workflow_run_id: str
    project_id: str | None
    node_id: str
    status: str
    requested_at: datetime
    decided_at: datetime | None
    decided_by: str | None
    decision_reason: str | None
    policy_snapshot: dict[str, object] | None


class WorkflowRunCapabilitySchema(BaseModel):
    """Capability flags for workflow run execution and controls."""

    queue_enabled: bool
    supports_async_submit: bool
    supports_cancel: bool
    supports_retry: bool
    supports_resume: bool
    runtime_approvals_enabled: bool
    checkpointing_enabled: bool
    event_backend: str


class PlatformCapabilitiesSchema(BaseModel):
    """Response payload for ``GET /caliber/capabilities``."""

    workflow_runs: WorkflowRunCapabilitySchema
    sync_workflow_version_run: bool = True


class WorkflowComponentFieldSchema(BaseModel):
    """One designer-visible configuration field for a workflow node type."""

    key: str
    label: str
    type: str
    required: bool = False
    default: object = None
    description: str | None = None
    constraints: dict[str, object] = Field(default_factory=dict)
    examples: list[object] = Field(default_factory=list)


class WorkflowComponentSetupCheckSchema(BaseModel):
    """Declarative setup-check metadata for a workflow component."""

    label: str
    help: str
    kind: str
    field: str | None = None
    fields: list[str] = Field(default_factory=list)
    minimum: float | None = None


class WorkflowComponentSchema(BaseModel):
    """Server-backed metadata for one workflow component in the designer."""

    type: str
    label: str
    category: str
    description: str
    docs: list[str] = Field(default_factory=list)
    default_inputs: dict[str, dict[str, object]] = Field(default_factory=dict)
    default_outputs: dict[str, dict[str, object]] = Field(default_factory=dict)
    starter_node: dict[str, Any] | None = None
    fields: list[WorkflowComponentFieldSchema] = Field(default_factory=list)
    setup_checks: list[WorkflowComponentSetupCheckSchema] = Field(default_factory=list)


class WorkflowComponentCatalogSchema(BaseModel):
    """Catalog of workflow components exposed to the designer UI."""

    schema_version: int = 1
    components: list[WorkflowComponentSchema] = Field(default_factory=list)


class WorkflowTemplateSchema(BaseModel):
    """Server-backed metadata + starter manifest for a new workflow template."""

    kind: str
    label: str
    description: str
    icon: str
    gradient: str
    manifest_template: dict[str, Any] = Field(default_factory=dict)


class WorkflowBakeoffScenarioSchema(BaseModel):
    """Runnable benchmark scenario mapped to one workflow starter."""

    id: str
    title: str
    starter_kind: str
    capabilities: list[str] = Field(default_factory=list)
    evidence_to_capture: list[str] = Field(default_factory=list)


class WorkflowBakeoffRubricSectionSchema(BaseModel):
    """Operator rubric section for consistent workflow-product comparisons."""

    title: str
    checks: list[str] = Field(default_factory=list)


class WorkflowBakeoffScenarioWorksheetEntrySchema(BaseModel):
    """Operator-captured benchmark notes for one bakeoff scenario."""

    model_config = ConfigDict(extra="forbid")

    status: str = Field(
        default="not_started",
        pattern=WORKFLOW_BAKEOFF_SCENARIO_STATUS_PATTERN,
    )
    minutes_to_first_success: str = Field(default="", max_length=64)
    evidence_links: str = Field(default="", max_length=4096)
    notes: str = Field(default="", max_length=20000)


class WorkflowBakeoffRubricWorksheetEntrySchema(BaseModel):
    """Operator score + notes for one bakeoff rubric section."""

    model_config = ConfigDict(extra="forbid")

    score: str = Field(default="", pattern=WORKFLOW_BAKEOFF_RUBRIC_SCORE_PATTERN)
    notes: str = Field(default="", max_length=20000)


class WorkflowBakeoffWorksheetSchema(BaseModel):
    """Persisted workflow-product bakeoff worksheet."""

    model_config = ConfigDict(extra="forbid")

    product_name: str = Field(default="Caliber", max_length=256)
    evaluator: str = Field(default="", max_length=256)
    environment: str = Field(default="", max_length=2048)
    summary: str = Field(default="", max_length=20000)
    updated_at: datetime | None = None
    scenarios: dict[str, WorkflowBakeoffScenarioWorksheetEntrySchema] = Field(default_factory=dict)
    rubric: dict[str, WorkflowBakeoffRubricWorksheetEntrySchema] = Field(default_factory=dict)


class WorkflowTemplateCatalogSchema(BaseModel):
    """Catalog of create-new-workflow templates exposed to the UI."""

    schema_version: int = 1
    templates: list[WorkflowTemplateSchema] = Field(default_factory=list)
    bakeoff_scenarios: list[WorkflowBakeoffScenarioSchema] = Field(default_factory=list)
    operator_rubric: list[WorkflowBakeoffRubricSectionSchema] = Field(default_factory=list)


class WorkflowBenchmarkReportSchema(BaseModel):
    """Saved workflow benchmark scorecard and worksheet."""

    model_config = ConfigDict(from_attributes=True)

    report_id: str
    name: str
    owner: str
    status: str = Field(pattern=WORKFLOW_BENCHMARK_REPORT_STATUS_PATTERN)
    product_name: str
    evaluator: str
    environment: str
    summary: str
    scenario_count: int = Field(ge=0)
    captured_count: int = Field(ge=0)
    passed_count: int = Field(ge=0)
    blocked_count: int = Field(ge=0)
    worksheet: WorkflowBakeoffWorksheetSchema
    created_at: datetime
    updated_at: datetime


class WorkflowBenchmarkReportCreateRequest(BaseModel):
    """Body of ``POST /caliber/workflow-benchmark-reports``."""

    model_config = ConfigDict(extra="forbid")

    report_id: str | None = Field(default=None, min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=256)
    status: str = Field(default="draft", pattern=WORKFLOW_BENCHMARK_REPORT_STATUS_PATTERN)
    worksheet: WorkflowBakeoffWorksheetSchema


class WorkflowBenchmarkReportUpdateRequest(BaseModel):
    """Body of ``PATCH /caliber/workflow-benchmark-reports/{report_id}``."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=256)
    status: str | None = Field(default=None, pattern=WORKFLOW_BENCHMARK_REPORT_STATUS_PATTERN)
    worksheet: WorkflowBakeoffWorksheetSchema | None = None


class WorkflowDeploymentSchema(BaseModel):
    """Serialized form of :class:`caliber.db.models.CaliberWorkflowDeployment`."""

    model_config = ConfigDict(from_attributes=True)

    deployment_id: str
    workflow_id: str
    alias: str
    version_id: str
    environment: str | None
    status: str
    deployed_by: str | None
    deployed_at: datetime
    rollback_checkpoint: list[dict[str, object]] = Field(default_factory=list)


class PromoteRequest(BaseModel):
    """Body of ``POST /caliber/workflows/{workflow_id}/deployments/{alias}/promote``."""

    model_config = ConfigDict(extra="forbid")

    version_id: str = Field(min_length=1, max_length=64)
    # Optimistic-concurrency guard: the version_id the caller believes the alias
    # currently serves. When provided and stale, the promote is refused (409).
    # ``None`` (explicit) asserts the alias is not yet deployed.
    expected_version_id: str | None = Field(default=None, max_length=64)


class WorkflowPromotionSchema(BaseModel):
    """Serialized form of :class:`caliber.db.models.CaliberWorkflowPromotion`."""

    model_config = ConfigDict(from_attributes=True)

    promotion_id: str
    workflow_id: str
    alias: str
    version_id: str
    status: str
    gate_result: dict[str, object] | None
    requested_by: str
    requested_at: datetime
    decided_by: str | None
    decided_at: datetime | None
    decision_reason: str | None


class PromotionDecisionRequest(BaseModel):
    """Body of workflow promotion approve/reject endpoints."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = None


class ImpactAgentSchema(BaseModel):
    """One agent touched by the candidate or its shared dependencies."""

    agent_id: str
    name: str | None = None
    role: str | None = None


class ImpactReferenceSchema(BaseModel):
    """Versioned dependency referenced by an approval preview."""

    id: str
    name: str
    status: str | None = None
    version: int | None = None


class ImpactDiffSchema(BaseModel):
    """Candidate-vs-current artifact preview."""

    current_available: bool
    candidate_hash: str
    diff_summary: str | None = None
    unified_diff: str


class ImpactRollbackSchema(BaseModel):
    """Rollback-readiness preview before promotion creates a checkpoint."""

    will_create_checkpoint: bool
    latest_checkpoint_id: str | None = None
    latest_checkpoint_created_at: datetime | None = None
    rollback_available: bool


class ImpactPreviewResponse(BaseModel):
    """Blast-radius preview for an approval."""

    approval_id: str
    job_id: str
    agent_id: str
    artifact_type: str
    impacted_agents: list[ImpactAgentSchema] = Field(default_factory=list)
    impacted_skills: list[ImpactReferenceSchema] = Field(default_factory=list)
    eval_datasets: list[ImpactReferenceSchema] = Field(default_factory=list)
    diff: ImpactDiffSchema
    rollback: ImpactRollbackSchema
    risk_flags: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Skills (Phase 4 — reusable prompt fragments)
# ---------------------------------------------------------------------------


# Skill use-case categories. The first three + ``custom`` come from the
# Anthropic skill standard; the rest are practical buckets covering the common
# things teams build skills for. Keep this the single source of truth — the
# request validators below derive their pattern from it, and the frontend
# ``SkillCategory`` union mirrors it.
SKILL_CATEGORIES = (
    "document_creation",
    "data_analysis",
    "data_extraction",
    "code_generation",
    "content_writing",
    "summarization",
    "classification",
    "research",
    "customer_support",
    "communication",
    "reasoning_planning",
    "tool_integration",
    "compliance_safety",
    "workflow_automation",
    "mcp_enhancement",
    "custom",
)

# Regex used by the skill request validators (derived so categories live in one
# place). Category names are ``[a-z_]+`` so they need no escaping.
SKILL_CATEGORY_PATTERN = r"^(?:" + "|".join(SKILL_CATEGORIES) + r")$"

# Reserved name prefixes that must not be used for skills.
_RESERVED_PREFIXES = ("claude", "anthropic")

# Kebab-case: lowercase letters, digits, and hyphens only.
SKILL_NAME_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"

# Which surface produced a durable skill-test run.
SKILL_TEST_RUN_KIND_PATTERN = "^(selection|render|scenario)$"


class SkillSchema(BaseModel):
    """Serialized form of :class:`caliber.db.models.CaliberSkill`."""

    model_config = ConfigDict(from_attributes=True)

    skill_id: str
    name: str
    description: str
    summary: str = ""
    content: str
    owner: str
    category: str = "custom"
    tags: list[str] = Field(default_factory=list)
    skill_metadata: dict[str, object] = Field(default_factory=dict)
    allowed_tools: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    status: str
    version: int
    created_at: datetime
    updated_at: datetime


class SkillCreateRequest(BaseModel):
    """Body of ``POST /caliber/skills``.

    The ``skill_id`` is generated server-side (via :func:`caliber.ids.new_skill_id`)
    so two operators racing to create skills with the same human-facing
    ``name`` get a clean 409 rather than a colliding primary key.
    ``name`` must be kebab-case and unique — that uniqueness is what
    makes the skill citable from an agent's optimizer_config.

    ``summary`` is the progressive-disclosure level-1 text — always
    loaded so the agent knows *when* to activate the skill.  Keep it
    under ~200 words.

    ``description`` carries the WHAT + WHEN trigger-phrase semantics
    from the Anthropic skill standard.

    ``category`` classifies the skill into one of the standard
    use-case buckets (document_creation, workflow_automation,
    mcp_enhancement, custom).

    ``metadata`` is an open JSON bag for extra key-value pairs
    (author, version, mcp-server, etc.).

    ``allowed_tools`` restricts which tools the skill may invoke.

    ``depends_on`` lists other skill names this skill composes with.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128, pattern=SKILL_NAME_PATTERN)
    description: str = Field(default="", max_length=2048)
    summary: str = Field(default="", max_length=1024)
    content: str = Field(min_length=1)
    owner: str = Field(min_length=1, max_length=256)
    category: str = Field(default="custom", pattern=SKILL_CATEGORY_PATTERN)
    tags: list[str] = Field(default_factory=list)
    skill_metadata: dict[str, object] = Field(default_factory=dict)
    allowed_tools: str | None = Field(default=None, max_length=512)
    depends_on: list[str] = Field(default_factory=list)


class EvalDatasetSchema(BaseModel):
    """Serialized form of :class:`caliber.db.models.CaliberEvalDataset`."""

    model_config = ConfigDict(from_attributes=True)

    dataset_id: str
    name: str
    description: str
    owner: str
    tags: list[str] = Field(default_factory=list)
    status: str
    version: int
    created_at: datetime
    updated_at: datetime

    # MLflow GenAI dataset sync linkage (null until first synced). The UI shows a
    # "synced" badge + deep link, and flags staleness when
    # ``mlflow_synced_version < version``.
    mlflow_dataset_id: str | None = None
    mlflow_synced_at: datetime | None = None
    mlflow_synced_version: int | None = None
    mlflow_record_count: int | None = None
    mlflow_digest: str | None = None


class EvalDatasetCreateRequest(BaseModel):
    """Body of ``POST /caliber/eval-datasets``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2048)
    owner: str = Field(min_length=1, max_length=256)
    tags: list[str] = Field(default_factory=list)


class EvalDatasetUpdateRequest(BaseModel):
    """Body of ``PATCH /caliber/eval-datasets/{dataset_id}``."""

    model_config = ConfigDict(extra="forbid")

    description: str | None = Field(default=None, max_length=2048)
    owner: str | None = Field(default=None, min_length=1, max_length=256)
    tags: list[str] | None = None
    status: str | None = Field(default=None, pattern="^(active|archived)$")


class EvalExampleSchema(BaseModel):
    """Serialized form of :class:`caliber.db.models.CaliberEvalDatasetExample`."""

    model_config = ConfigDict(from_attributes=True)

    example_id: str
    dataset_id: str
    dataset_version: int
    input: dict[str, object]
    expected: dict[str, object]
    weight: float
    tags: list[str] = Field(default_factory=list)
    created_at: datetime
    superseded_at: datetime | None = None
    superseded_version: int | None = None


class EvalExampleCreateRequest(BaseModel):
    """Body of ``POST /caliber/eval-datasets/{dataset_id}/examples``.

    Examples are append-only — there's no PATCH on an individual
    example. To replace an example, post a new one and mark the old
    one superseded via the dedicated endpoint.
    """

    model_config = ConfigDict(extra="forbid")

    input: dict[str, object] = Field(default_factory=dict)
    expected: dict[str, object] = Field(default_factory=dict)
    weight: float = Field(default=1.0, ge=0)
    tags: list[str] = Field(default_factory=list)


class EvalExampleReviseRequest(BaseModel):
    """Body of ``POST /caliber/eval-datasets/{id}/examples/{example_id}/revise``.

    A convenience "edit a row" over the append-only model: in one transaction
    it supersedes the target example and appends a replacement carrying the new
    content, bumping the dataset version exactly once. The caller sends the
    complete new row (the editor pre-fills it from the current values), so all
    fields are the replacement's values, not a partial patch.
    """

    model_config = ConfigDict(extra="forbid")

    input: dict[str, object] = Field(default_factory=dict)
    expected: dict[str, object] = Field(default_factory=dict)
    weight: float = Field(default=1.0, ge=0)
    tags: list[str] = Field(default_factory=list)


class EvalExampleFromTraceRequest(BaseModel):
    """Body of ``POST /caliber/eval-datasets/{id}/examples/from-trace``.

    Appends an example built from an observed MLflow trace: the trace's request
    becomes the example ``input`` and its response the ``expected`` answer, so a
    real interaction can be captured as a regression case in one click. Optional
    ``input``/``expected`` overrides let the caller correct the captured pair
    (e.g. fix a wrong answer before saving it as the gold expectation).
    """

    model_config = ConfigDict(extra="forbid")

    trace_id: str = Field(min_length=1, max_length=256)
    input: dict[str, object] | None = None
    expected: dict[str, object] | None = None
    weight: float = Field(default=1.0, ge=0)
    tags: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Custom LLM judges (MLflow 3.14 make_judge) — see caliber.routes.judges.
# ---------------------------------------------------------------------------

# The evaluation-data template variables MLflow's make_judge accepts. Instructions
# must reference at least one so the judge has something to score.
_JUDGE_TEMPLATE_VARS = ("inputs", "outputs", "expectations", "conversation", "trace")
_JUDGE_VALUE_TYPES = frozenset({"bool", "int", "float", "str"})


def _instructions_reference_a_var(instructions: str) -> bool:
    """True when the text contains at least one ``{{ var }}`` template token."""
    normalized = instructions.replace(" ", "")
    return any(f"{{{{{var}}}}}" in normalized for var in _JUDGE_TEMPLATE_VARS)


class JudgeSchema(BaseModel):
    """Serialized form of :class:`caliber.db.models.CaliberJudge`."""

    model_config = ConfigDict(from_attributes=True)

    judge_id: str
    name: str
    description: str
    instructions: str
    model: str | None = None
    feedback_value_type: str | None = None
    owner: str
    tags: list[str] = Field(default_factory=list)
    status: str
    created_at: datetime
    updated_at: datetime


class JudgeCreateRequest(BaseModel):
    """Body of ``POST /caliber/judges``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2048)
    instructions: str = Field(min_length=1, max_length=8192)
    model: str | None = Field(default=None, max_length=128)
    feedback_value_type: str | None = Field(default=None)
    tags: list[str] = Field(default_factory=list)

    @field_validator("instructions")
    @classmethod
    def _must_reference_a_template_var(cls, value: str) -> str:
        if not _instructions_reference_a_var(value):
            raise ValueError(
                "instructions must reference at least one evaluation variable: "
                + ", ".join(f"{{{{ {v} }}}}" for v in _JUDGE_TEMPLATE_VARS)
            )
        return value

    @field_validator("feedback_value_type")
    @classmethod
    def _known_value_type(cls, value: str | None) -> str | None:
        if value is not None and value not in _JUDGE_VALUE_TYPES:
            raise ValueError(f"feedback_value_type must be one of {sorted(_JUDGE_VALUE_TYPES)}")
        return value


class JudgeUpdateRequest(BaseModel):
    """Body of ``PATCH /caliber/judges/{judge_id}``."""

    model_config = ConfigDict(extra="forbid")

    description: str | None = Field(default=None, max_length=2048)
    instructions: str | None = Field(default=None, min_length=1, max_length=8192)
    model: str | None = Field(default=None, max_length=128)
    feedback_value_type: str | None = None
    tags: list[str] | None = None
    status: str | None = Field(default=None, pattern="^(active|archived)$")

    @field_validator("instructions")
    @classmethod
    def _must_reference_a_template_var(cls, value: str | None) -> str | None:
        if value is not None and not _instructions_reference_a_var(value):
            raise ValueError(
                "instructions must reference at least one evaluation variable: "
                + ", ".join(f"{{{{ {v} }}}}" for v in _JUDGE_TEMPLATE_VARS)
            )
        return value

    @field_validator("feedback_value_type")
    @classmethod
    def _known_value_type(cls, value: str | None) -> str | None:
        if value is not None and value not in _JUDGE_VALUE_TYPES:
            raise ValueError(f"feedback_value_type must be one of {sorted(_JUDGE_VALUE_TYPES)}")
        return value


class LlmPricingSchema(BaseModel):
    """Serialized form of :class:`caliber.db.models.CaliberLlmModelPricing`."""

    model_config = ConfigDict(from_attributes=True)

    pricing_id: str
    provider: str
    model_id: str
    prompt_price: float
    completion_price: float
    cached_prompt_price: float | None = None
    owner: str
    tags: list[str] = Field(default_factory=list)
    status: str
    created_at: datetime
    updated_at: datetime


class LlmPricingCreateRequest(BaseModel):
    """Body of ``POST /caliber/llm-pricing`` — per-model token pricing (USD per 1K)."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=64)
    model_id: str = Field(min_length=1, max_length=128)
    prompt_price: float = Field(ge=0.0)
    completion_price: float = Field(ge=0.0)
    cached_prompt_price: float | None = Field(default=None, ge=0.0)
    tags: list[str] = Field(default_factory=list)


class LlmPricingUpdateRequest(BaseModel):
    """Body of ``PATCH /caliber/llm-pricing/{pricing_id}``."""

    model_config = ConfigDict(extra="forbid")

    provider: str | None = Field(default=None, min_length=1, max_length=64)
    model_id: str | None = Field(default=None, min_length=1, max_length=128)
    prompt_price: float | None = Field(default=None, ge=0.0)
    completion_price: float | None = Field(default=None, ge=0.0)
    cached_prompt_price: float | None = Field(default=None, ge=0.0)
    tags: list[str] | None = None
    status: str | None = Field(default=None, pattern="^(active|archived)$")


class JudgeTestRunRequest(BaseModel):
    """Body of ``POST /caliber/judges/{judge_id}/test-run`` — the "Try it" playground.

    Runs the judge once against a sample (without persisting anything) so an
    author can see its verdict + rationale before wiring it into evaluations.
    """

    model_config = ConfigDict(extra="forbid")

    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: str = Field(min_length=1, max_length=20000)
    expectations: dict[str, Any] = Field(default_factory=dict)


class JudgeTestRunResult(BaseModel):
    """Result of a judge "Try it" run: the unit score, raw verdict, and rationale."""

    model_config = ConfigDict(extra="forbid")

    score: float
    value: Any = None
    rationale: str | None = None


class JudgeAlignmentExample(BaseModel):
    """One human-labeled example for a judge-alignment check."""

    model_config = ConfigDict(extra="forbid")

    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: str = Field(min_length=1, max_length=20000)
    expectations: dict[str, Any] = Field(default_factory=dict)
    # The human verdict (pass/fail) the judge is measured against.
    label: bool


class JudgeAlignmentRequest(BaseModel):
    """Body of ``POST /caliber/judges/{judge_id}/alignment``.

    Runs the judge over human-labeled examples and reports how well its verdicts
    agree with the humans — the trust check behind a usable judge.
    """

    model_config = ConfigDict(extra="forbid")

    examples: list[JudgeAlignmentExample] = Field(min_length=1, max_length=100)
    # The judge's unit score is thresholded into a pass/fail label at this cut.
    threshold: float = Field(default=0.5, ge=0, le=1)


class JudgeAlignmentPerExample(BaseModel):
    """Per-example judge-vs-human comparison row."""

    model_config = ConfigDict(extra="forbid")

    outputs: str
    human_label: bool
    judge_label: bool | None
    judge_score: float | None
    agree: bool
    error: str | None = None


class JudgeAlignmentResult(BaseModel):
    """Agreement rate + Cohen's kappa between a judge and human labels."""

    model_config = ConfigDict(extra="forbid")

    n: int
    scored: int
    agreement_rate: float
    cohen_kappa: float
    threshold: float
    confusion: dict[str, int]
    per_example: list[JudgeAlignmentPerExample]


# ---------------------------------------------------------------------------
# Structured human-review queues — see caliber.routes.review_queues.
# ---------------------------------------------------------------------------

_REVIEW_QUESTION_TYPES = frozenset({"pass_fail", "categorical", "numeric", "text"})
_REVIEW_QUESTION_TARGETS = frozenset({"feedback", "expectation"})


class ReviewQuestion(BaseModel):
    """One question in a review queue's label schema."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    title: str = Field(min_length=1, max_length=256)
    type: str = Field(default="pass_fail")
    options: list[str] = Field(default_factory=list)
    required: bool = True
    # feedback → mlflow.log_feedback (assessment); expectation → log_expectation (ground truth).
    target: str = Field(default="feedback")

    @field_validator("type")
    @classmethod
    def _known_type(cls, value: str) -> str:
        if value not in _REVIEW_QUESTION_TYPES:
            raise ValueError(f"type must be one of {sorted(_REVIEW_QUESTION_TYPES)}")
        return value

    @field_validator("target")
    @classmethod
    def _known_target(cls, value: str) -> str:
        if value not in _REVIEW_QUESTION_TARGETS:
            raise ValueError(f"target must be one of {sorted(_REVIEW_QUESTION_TARGETS)}")
        return value

    @model_validator(mode="after")
    def _categorical_needs_options(self) -> ReviewQuestion:
        if self.type == "categorical" and not self.options:
            raise ValueError("categorical questions require a non-empty 'options' list")
        return self


def _validate_questions(questions: list[ReviewQuestion]) -> list[ReviewQuestion]:
    if not questions:
        raise ValueError("a review queue needs at least one question")
    keys = [q.key for q in questions]
    if len(keys) != len(set(keys)):
        raise ValueError("question keys must be unique")
    return questions


class ReviewQueueSchema(BaseModel):
    """Serialized form of :class:`caliber.db.models.CaliberReviewQueue`."""

    model_config = ConfigDict(from_attributes=True)

    queue_id: str
    name: str
    description: str
    questions: list[ReviewQuestion] = Field(default_factory=list)
    reviewers: list[str] = Field(default_factory=list)
    owner: str
    status: str
    created_at: datetime
    updated_at: datetime
    # Populated by the list/detail routes (not stored on the row).
    item_count: int | None = None
    pending_count: int | None = None


class ReviewItemSchema(BaseModel):
    """Serialized form of :class:`caliber.db.models.CaliberReviewItem`."""

    model_config = ConfigDict(from_attributes=True)

    item_id: str
    queue_id: str
    trace_id: str
    experiment_id: str | None = None
    status: str
    assigned_to: str | None = None
    answers: dict[str, Any] = Field(default_factory=dict)
    assessment_ids: list[str] = Field(default_factory=list)
    created_at: datetime
    completed_at: datetime | None = None
    completed_by: str | None = None


class ReviewQueueCreateRequest(BaseModel):
    """Body of ``POST /caliber/review-queues``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2048)
    questions: list[ReviewQuestion] = Field(default_factory=list)
    reviewers: list[str] = Field(default_factory=list)

    @field_validator("questions")
    @classmethod
    def _check_questions(cls, value: list[ReviewQuestion]) -> list[ReviewQuestion]:
        return _validate_questions(value)


class ReviewQueueUpdateRequest(BaseModel):
    """Body of ``PATCH /caliber/review-queues/{queue_id}``."""

    model_config = ConfigDict(extra="forbid")

    description: str | None = Field(default=None, max_length=2048)
    questions: list[ReviewQuestion] | None = None
    reviewers: list[str] | None = None
    status: str | None = Field(default=None, pattern="^(active|archived)$")

    @field_validator("questions")
    @classmethod
    def _check_questions(cls, value: list[ReviewQuestion] | None) -> list[ReviewQuestion] | None:
        return None if value is None else _validate_questions(value)


class ReviewItemsAddRequest(BaseModel):
    """Body of ``POST /caliber/review-queues/{queue_id}/items``."""

    model_config = ConfigDict(extra="forbid")

    trace_ids: list[str] = Field(min_length=1)
    experiment_id: str | None = None
    assigned_to: str | None = None


class ReviewItemSubmitRequest(BaseModel):
    """Body of ``POST /caliber/review-queues/{queue_id}/items/{item_id}/submit``."""

    model_config = ConfigDict(extra="forbid")

    answers: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Aria goal-plans (agentic orchestration) — see caliber.routes.aria_plans.
# ---------------------------------------------------------------------------

_ARIA_AUTONOMY_VALUES = frozenset({"ask_each", "approve_plan", "auto_guarded"})


class AriaPlanStepSchema(BaseModel):
    """Serialized form of :class:`caliber.db.models.CaliberAriaPlanStep`."""

    model_config = ConfigDict(from_attributes=True)

    step_id: str
    plan_id: str
    seq: int
    capability_key: str
    title: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    # Registry-derived (not persisted) schema used to render and validate the
    # plan's no-code input form.
    input_schema: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    status: str
    result: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)
    gate: dict[str, Any] | None = None
    error: str | None = None
    draft_id: str | None = None
    job_id: str | None = None
    approval_id: str | None = None
    checkpoint_id: str | None = None
    created_at: datetime
    updated_at: datetime


class AriaPlanSchema(BaseModel):
    """Serialized form of :class:`caliber.db.models.CaliberAriaPlan`."""

    model_config = ConfigDict(from_attributes=True)

    plan_id: str
    session_id: str | None = None
    project_id: str | None = None
    goal: str
    status: str
    autonomy: str
    owner: str
    constraints: dict[str, object] = Field(default_factory=dict)
    done_when: list[str] = Field(default_factory=list)
    context_refs: list[TaskContextRef] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    # Populated by the detail/list routes (not stored on the row).
    step_count: int | None = None


class AriaPlanCreateRequest(BaseModel):
    """Body of ``POST /caliber/aria/plans`` — decompose a goal into a plan."""

    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, max_length=8192)
    session_id: str | None = None
    autonomy: str = Field(default="approve_plan")
    constraints: dict[str, object] = Field(default_factory=dict)
    done_when: list[str] = Field(default_factory=list)
    context_refs: list[TaskContextRef] = Field(default_factory=list)

    @field_validator("autonomy")
    @classmethod
    def _known_autonomy(cls, value: str) -> str:
        if value not in _ARIA_AUTONOMY_VALUES:
            raise ValueError(f"autonomy must be one of {sorted(_ARIA_AUTONOMY_VALUES)}")
        return value


class AriaPlanUpdateRequest(BaseModel):
    """Body of ``PATCH /caliber/aria/plans/{plan_id}`` — edit before approval."""

    model_config = ConfigDict(extra="forbid")

    autonomy: str | None = None
    status: str | None = Field(default=None, pattern="^(draft|cancelled)$")

    @field_validator("autonomy")
    @classmethod
    def _known_autonomy(cls, value: str | None) -> str | None:
        if value is not None and value not in _ARIA_AUTONOMY_VALUES:
            raise ValueError(f"autonomy must be one of {sorted(_ARIA_AUTONOMY_VALUES)}")
        return value


class AriaInteractionSchema(BaseModel):
    """Serialized form of :class:`caliber.db.models.CaliberAriaInteraction`."""

    model_config = ConfigDict(from_attributes=True)

    interaction_id: str
    plan_id: str
    step_id: str
    kind: str
    prompt: str
    options: list[dict[str, Any]] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    required_scope: str | None = None
    status: str
    response: dict[str, Any] = Field(default_factory=dict)
    responded_by: str | None = None
    responded_at: datetime | None = None
    created_at: datetime


class AriaInteractionAnswerRequest(BaseModel):
    """Body of ``POST /caliber/aria/interactions/{interaction_id}/answer``.

    For a ``permission`` / ``confirm`` ask, set ``approved``. For a ``choice``,
    set ``choice`` to the selected option value. For an ``input``, set the
    capability fields in ``inputs``.
    """

    model_config = ConfigDict(extra="forbid")

    approved: bool | None = None
    choice: str | None = None
    value: Any | None = None
    inputs: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Evaluation runs (scorecard) — see caliber.routes.evaluations.
# ---------------------------------------------------------------------------


EvalPredictTarget = Literal["llm", "prompt", "skill", "workflow"]


class EvalRunCreateRequest(BaseModel):
    """Body of ``POST /caliber/evaluations`` — run a dataset through scorers.

    ``predict_target`` selects *what* is scored:

    * ``llm`` (default) — a generic completion from the configured model, the
      legacy behaviour; ``subject_ref`` is ignored.
    * ``prompt`` — a registered prompt version (``subject_ref`` = ``"<name>@<version>"``
      or ``"<name>"`` for version 1); its template renders as the system
      instruction so the *prompt itself* is the thing under test.
    * ``skill`` — a skill (``subject_ref`` = skill id); its content renders as the
      system instruction.
    * ``workflow`` — a workflow version id; the route compiles it once and runs
      it in preview mode for at most 20 examples. Preview is an execution mode,
      not a guarantee that every integration is side-effect-free.
    """

    model_config = ConfigDict(extra="forbid")

    dataset_id: str = Field(min_length=1, max_length=64)
    # ``None`` scores the dataset's current active set; an int pins the
    # historical example set "as of version N" for a reproducible run.
    dataset_version: int | None = Field(default=None, ge=1)
    label: str = Field(default="", max_length=256)
    scorers: list[str] = Field(default_factory=list)
    pass_threshold: float = Field(default=0.5, ge=0, le=1)
    # Cap on examples scored (the run is synchronous; large sets risk timeouts).
    max_examples: int | None = Field(default=None, ge=1, le=500)
    predict_target: EvalPredictTarget = "llm"
    # The artifact under test for non-``llm`` targets (prompt ref / skill id /
    # workflow version id).
    subject_ref: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def _subject_required_for_artifact_targets(self) -> EvalRunCreateRequest:
        if self.predict_target != "llm" and not (self.subject_ref or "").strip():
            raise ValueError(
                f"predict_target {self.predict_target!r} requires a 'subject_ref' "
                "(the prompt ref / skill id to score)"
            )
        return self


class EvalRunSummarySchema(BaseModel):
    """List/summary form of :class:`caliber.db.models.CaliberEvalRun` (no rows)."""

    model_config = ConfigDict(from_attributes=True)

    run_id: str
    dataset_id: str
    dataset_version: int
    label: str
    predict_target: str
    subject_ref: str | None = None
    model: str | None = None
    scorers: list[str] = Field(default_factory=list)
    pass_threshold: float
    n_examples: int
    passed_count: int
    failed_count: int
    overall_score: float | None = None
    pass_rate: float | None = None
    aggregate: dict[str, float] = Field(default_factory=dict)
    status: str
    error_message: str | None = None
    created_by: str
    created_at: datetime
    completed_at: datetime | None = None


class EvalRunSchema(EvalRunSummarySchema):
    """Detail form — adds the heavy per-example ``results`` array."""

    results: list[dict[str, object]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# MLflow AI Gateway (LLM gateway) — see caliber.routes.gateway.
# ---------------------------------------------------------------------------


class GatewayEndpointSchema(BaseModel):
    """One LLM endpoint exposed by the MLflow AI Gateway."""

    model_config = ConfigDict(extra="ignore")

    name: str
    endpoint_type: str = ""
    provider: str = ""
    model: str = ""
    endpoint_url: str = ""
    limit: dict[str, object] | None = None


class SystemServiceSchema(BaseModel):
    """One backing platform service for the Settings → Services tab."""

    model_config = ConfigDict(extra="forbid")

    key: str
    name: str
    description: str = ""
    category: str = ""
    # Public, browsable URL (``None`` for non-HTTP services like the DB / bus).
    url: str | None = None
    # The target CALIBER actually probed (host:port or URL) — shown as the
    # connection detail so it's clear what was checked.
    target: str = ""
    # ``True``/``False`` = probed up/down; ``None`` = unknown / not configured.
    healthy: bool | None = None
    detail: str = ""
    latency_ms: int | None = None


class GatewayStatusSchema(BaseModel):
    """Discovery + routing status of the MLflow AI Gateway for the Gateway page."""

    model_config = ConfigDict(extra="forbid")

    configured: bool
    reachable: bool
    gateway_uri: str = ""
    # Whether CALIBER's own LLM calls route through the gateway (llm_base_url
    # points at it) — distinct from merely discovering it.
    routing_through_gateway: bool = False
    llm_base_url: str = ""
    endpoints: list[GatewayEndpointSchema] = Field(default_factory=list)
    error: str | None = None


class GatewayGuardrailSchema(BaseModel):
    """One gateway guardrail (MLflow scorer-backed) configured on the tracking server."""

    model_config = ConfigDict(extra="ignore")

    guardrail_id: str
    name: str
    stage: str = ""  # BEFORE | AFTER
    action: str = ""  # VALIDATION | SANITIZATION
    scorer: str | None = None
    action_endpoint_name: str | None = None


class GatewayEndpointGuardrailSchema(BaseModel):
    """A guardrail as attached to a specific endpoint (with its execution order)."""

    model_config = ConfigDict(extra="ignore")

    guardrail_id: str
    name: str = ""
    execution_order: int | None = None
    enabled: bool = True


class GatewayGuardrailCoverageSchema(BaseModel):
    """The guardrails protecting one endpoint, in execution order."""

    model_config = ConfigDict(extra="ignore")

    endpoint: str
    endpoint_id: str
    guardrails: list[GatewayEndpointGuardrailSchema] = Field(default_factory=list)


class GatewayGuardrailsStatusSchema(BaseModel):
    """Guardrails tab payload: the configured guardrails + per-endpoint coverage."""

    model_config = ConfigDict(extra="forbid")

    configured: bool
    reachable: bool
    guardrails: list[GatewayGuardrailSchema] = Field(default_factory=list)
    coverage: list[GatewayGuardrailCoverageSchema] = Field(default_factory=list)
    error: str | None = None


class GatewayGuardrailAttachRequest(BaseModel):
    """Body of ``POST /caliber/gateway/endpoints/{endpoint_id}/guardrails``."""

    model_config = ConfigDict(extra="forbid")

    guardrail_id: str = Field(min_length=1)
    execution_order: int | None = Field(default=None, ge=0)


class GatewayGuardrailConfigUpdateRequest(BaseModel):
    """Body of ``PATCH .../endpoints/{endpoint_id}/guardrails/{guardrail_id}``."""

    model_config = ConfigDict(extra="forbid")

    execution_order: int | None = Field(default=None, ge=0)
    enabled: bool | None = None


GUARDRAIL_STAGES = ("BEFORE", "AFTER")
GUARDRAIL_ACTIONS = ("VALIDATION", "SANITIZATION")


class GatewayScorerFieldSchema(BaseModel):
    """One config input a scorer template exposes (drives the create form)."""

    model_config = ConfigDict(extra="ignore")

    name: str
    label: str
    type: str  # text | textarea | select | multiselect | boolean
    required: bool = False
    help: str | None = None
    placeholder: str | None = None
    options: list[str] = Field(default_factory=list)


class GatewayScorerTemplateSchema(BaseModel):
    """A buildable guardrail kind backed by a native (no-extra-deps) MLflow scorer."""

    model_config = ConfigDict(extra="ignore")

    type: str  # pii | toxicity | guidelines | regex
    label: str
    summary: str
    scorer_class: str
    deterministic: bool  # True = rule-based (no LLM judge needed at the gateway)
    default_stage: str
    default_action: str
    fields: list[GatewayScorerFieldSchema] = Field(default_factory=list)


class GatewayRegisteredScorerSchema(BaseModel):
    """An already-registered MLflow scorer that can back a guardrail directly."""

    model_config = ConfigDict(extra="ignore")

    name: str
    scorer_id: str
    version: int


class GatewayGuardrailCatalogSchema(BaseModel):
    """``GET .../guardrails/catalog`` — what the create form can offer."""

    model_config = ConfigDict(extra="forbid")

    configured: bool
    reachable: bool
    templates: list[GatewayScorerTemplateSchema] = Field(default_factory=list)
    scorers: list[GatewayRegisteredScorerSchema] = Field(default_factory=list)
    error: str | None = None


class GatewayGuardrailCreateRequest(BaseModel):
    """Body of ``POST .../guardrails`` — define a new gateway guardrail.

    Either ``scorer_type`` (build + register a native scorer from ``config``) or
    ``scorer_id``/``scorer_version`` (reuse an already-registered scorer) must be
    given, never both.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    stage: str
    action: str
    action_endpoint_id: str | None = None
    scorer_type: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    scorer_id: str | None = None
    scorer_version: int | None = Field(default=None, ge=1)

    @field_validator("stage")
    @classmethod
    def _check_stage(cls, v: str) -> str:
        if v not in GUARDRAIL_STAGES:
            raise ValueError(f"stage must be one of {GUARDRAIL_STAGES}")
        return v

    @field_validator("action")
    @classmethod
    def _check_action(cls, v: str) -> str:
        if v not in GUARDRAIL_ACTIONS:
            raise ValueError(f"action must be one of {GUARDRAIL_ACTIONS}")
        return v

    @model_validator(mode="after")
    def _check_scorer_source(self) -> GatewayGuardrailCreateRequest:
        has_template = bool(self.scorer_type)
        has_existing = bool(self.scorer_id)
        if has_template == has_existing:
            raise ValueError(
                "provide exactly one of 'scorer_type' (build a new scorer) or "
                "'scorer_id' (reuse a registered scorer)"
            )
        if has_existing and self.scorer_version is None:
            raise ValueError("'scorer_version' is required when 'scorer_id' is given")
        return self


# ---------------------------------------------------------------------------
# MCP servers
# ---------------------------------------------------------------------------


MCP_TRANSPORT_PATTERN = "^(stdio|sse|streamable-http)$"
MCP_AUTH_TYPE_PATTERN = "^(none|token|basic|custom)$"
MCP_STATUS_PATTERN = "^(active|error|disabled)$"


class McpExecutionReadinessSchema(BaseModel):
    """Effective MCP runtime containment and availability assessment."""

    model_config = ConfigDict(extra="forbid")

    ready: bool = False
    transport_ready: bool = False
    status_ready: bool = False
    boundary: str = "none"
    production_isolated: bool = False
    command_allowed: bool | None = None
    executable_available: bool | None = None
    remote_host_allowed: bool | None = None
    controls: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


MCP_SIDE_EFFECT_PATTERN = "^(read|write|external_action)$"


class McpToolPolicySchema(BaseModel):
    """Policy controls applied to one MCP tool."""

    model_config = ConfigDict(extra="forbid")

    allowed: bool = True
    side_effect_level: str = Field(default="read", pattern=MCP_SIDE_EFFECT_PATTERN)
    requires_approval: bool = False
    rate_limit_per_minute: int | None = Field(default=None, ge=1, le=100_000)


class McpServerSchema(BaseModel):
    """Serialized form of :class:`caliber.db.models.CaliberMcpServer`."""

    model_config = ConfigDict(from_attributes=True)

    server_id: str
    name: str
    description: str = ""
    transport: str
    uri: str = ""
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, object] = Field(default_factory=dict)
    headers: dict[str, object] = Field(default_factory=dict)
    auth_type: str = "none"
    auth_config: dict[str, object] = Field(default_factory=dict)
    discovered_tools: list[dict[str, object]] = Field(default_factory=list)
    tool_policies: dict[str, object] = Field(default_factory=dict)
    tool_test_cases: dict[str, object] = Field(default_factory=dict)
    tool_calibrations: dict[str, object] = Field(default_factory=dict)
    icon: str = ""
    status: str
    last_connected_at: datetime | None = None
    connection_error: str | None = None
    execution: McpExecutionReadinessSchema = Field(default_factory=McpExecutionReadinessSchema)
    owner: str = ""
    created_at: datetime
    updated_at: datetime


class McpServerCreateRequest(BaseModel):
    """Body of ``POST /caliber/mcp-servers``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2048)
    transport: str = Field(default="stdio", pattern=MCP_TRANSPORT_PATTERN)
    uri: str = Field(default="", max_length=1024)
    command: str = Field(default="", max_length=1024)
    args: list[str] = Field(default_factory=list)
    env: dict[str, object] = Field(default_factory=dict)
    headers: dict[str, object] = Field(default_factory=dict)
    auth_type: str = Field(default="none", pattern=MCP_AUTH_TYPE_PATTERN)
    auth_config: dict[str, object] = Field(default_factory=dict)
    icon: str = Field(default="", max_length=64)
    owner: str = Field(default="", max_length=256)
    # Optional well-known toolset to seed the server with (e.g. from a catalog
    # template) so its tools are visible before a live discovery runs. Real
    # ``tools/list`` discovery overwrites these on the next test-connection.
    discovered_tools: list[dict[str, object]] = Field(default_factory=list)
    tool_policies: dict[str, McpToolPolicySchema] = Field(default_factory=dict)


class McpServerUpdateRequest(BaseModel):
    """Body of ``PATCH /caliber/mcp-servers/{server_id}``."""

    model_config = ConfigDict(extra="forbid")

    description: str | None = Field(default=None, max_length=2048)
    transport: str | None = Field(default=None, pattern=MCP_TRANSPORT_PATTERN)
    uri: str | None = Field(default=None, max_length=1024)
    command: str | None = Field(default=None, max_length=1024)
    args: list[str] | None = None
    env: dict[str, object] | None = None
    headers: dict[str, object] | None = None
    auth_type: str | None = Field(default=None, pattern=MCP_AUTH_TYPE_PATTERN)
    auth_config: dict[str, object] | None = None
    icon: str | None = Field(default=None, max_length=64)
    owner: str | None = Field(default=None, max_length=256)
    status: str | None = Field(default=None, pattern=MCP_STATUS_PATTERN)


class McpToolPolicyUpdateRequest(BaseModel):
    """Body of ``PATCH /caliber/mcp-servers/{id}/tools/{tool_name}/policy``."""

    model_config = ConfigDict(extra="forbid")

    allowed: bool | None = None
    side_effect_level: str | None = Field(default=None, pattern=MCP_SIDE_EFFECT_PATTERN)
    requires_approval: bool | None = None
    rate_limit_per_minute: int | None = Field(default=None, ge=1, le=100_000)


class McpDiscoveredToolSchema(BaseModel):
    """One tool discovered from an MCP server."""

    model_config = ConfigDict(extra="allow")

    name: str = Field(min_length=1, max_length=256)
    description: str = ""
    input_schema: dict[str, object] | None = None
    output_schema: dict[str, object] | None = None


class McpDiscoverToolsResponse(BaseModel):
    """Body of ``POST /caliber/mcp-servers/{id}/discover-tools``."""

    model_config = ConfigDict(extra="forbid")

    server_id: str
    tools: list[McpDiscoveredToolSchema] = Field(default_factory=list)
    tool_count: int = Field(default=0, ge=0)
    discovered_at: datetime | None = None


class McpDiscoveredToolWithPolicySchema(McpDiscoveredToolSchema):
    """Discovered tool plus effective policy controls."""

    model_config = ConfigDict(extra="allow")

    policy: McpToolPolicySchema
    classified: bool = False


class McpServerToolsResponse(BaseModel):
    """Body of ``GET /caliber/mcp-servers/{id}/tools``."""

    model_config = ConfigDict(extra="forbid")

    server_id: str
    tools: list[McpDiscoveredToolWithPolicySchema] = Field(default_factory=list)


class McpToolPolicyUpdateResponse(BaseModel):
    """Body of ``PATCH /caliber/mcp-servers/{id}/tools/{tool}/policy``."""

    model_config = ConfigDict(extra="forbid")

    server_id: str
    tool_name: str
    policy: McpToolPolicySchema


class AgentSkillsResponse(BaseModel):
    """Body of ``GET /caliber/agents/{agent_id}/skills``.

    Resolved skills + the list of cited names that didn't match a row.
    ``missing`` is empty in the healthy case — present so a UI can
    flag broken references when an operator archived a skill an
    agent still cites by name.
    """

    model_config = ConfigDict(from_attributes=True)

    skills: list[SkillSchema] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


class SkillUpdateRequest(BaseModel):
    """Body of ``PATCH /caliber/skills/{skill_id}``.

    All fields optional — clients send only the keys they want to
    change. ``skill_id`` and ``name`` are intentionally absent:
    identity is fixed at create time (a renamed skill would silently
    break agent references that cite it by name). ``status`` accepts
    ``active`` or ``archived`` — the soft-delete path.

    Updating ``content`` bumps ``version`` automatically so external
    references can detect drift without comparing strings; the audit
    row records the old/new version pair.
    """

    model_config = ConfigDict(extra="forbid")

    description: str | None = Field(default=None, max_length=2048)
    summary: str | None = Field(default=None, max_length=1024)
    content: str | None = Field(default=None, min_length=1)
    owner: str | None = Field(default=None, min_length=1, max_length=256)
    category: str | None = Field(default=None, pattern=SKILL_CATEGORY_PATTERN)
    tags: list[str] | None = None
    skill_metadata: dict[str, object] | None = None
    allowed_tools: str | None = None
    depends_on: list[str] | None = None
    status: str | None = Field(default=None, pattern="^(active|archived)$")


# ---------------------------------------------------------------------------
# Skill test runs + workspace ("pytest for skills")
# ---------------------------------------------------------------------------


class SkillTestCaseResult(BaseModel):
    """One judged case inside a persisted skill-test run.

    ``verdict`` is constrained to the judge vocabulary and ``score`` to a
    normalized 0-1 range so a corrupt client payload can't poison the durable
    history. ``input``/``output`` are free-form; ``output`` may be any JSON value
    (or null). A selection-trigger case typically carries the user message in
    ``input`` and the selection decision in ``output``; a scenario case carries
    the rendered expectation/response prose in ``reasoning``.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=256)
    input: dict[str, Any] = Field(default_factory=dict)
    output: Any | None = None
    error: str | None = None
    verdict: Literal["pass", "fail", "partial"]
    score: float = Field(ge=0, le=1)
    duration_ms: float = 0.0
    reasoning: str = ""


class SkillTestRunCreateRequest(BaseModel):
    """Body of ``POST /caliber/skills/test-runs``.

    The client sends the skill id, an optional ``kind``/``skill_version``
    snapshot, an optional ``host_agent_id`` for context, and the per-case
    ``results``; the server recomputes ``test_set_size``, the pass/fail/partial
    counts, and ``overall_score`` from ``results`` rather than trusting any
    client-supplied aggregates.
    """

    model_config = ConfigDict(extra="forbid")

    skill_id: str = Field(min_length=1, max_length=64)
    kind: str = Field(default="scenario", pattern=SKILL_TEST_RUN_KIND_PATTERN)
    skill_version: int | None = Field(default=None, ge=0, le=2**31 - 1)
    host_agent_id: str | None = Field(default=None, max_length=64)
    results: list[SkillTestCaseResult]
    trace_id: str | None = Field(default=None, max_length=64)
    mlflow_run_id: str | None = Field(default=None, max_length=64)


class SkillTestRunSummary(BaseModel):
    """History-list row for a persisted skill-test run (no per-case array)."""

    model_config = ConfigDict(from_attributes=True)

    test_run_id: str
    skill_id: str
    skill_version: int | None
    kind: str
    test_set_size: int
    passed_count: int
    failed_count: int
    partial_count: int
    overall_score: float | None
    host_agent_id: str | None
    trace_id: str | None
    mlflow_run_id: str | None
    created_by: str
    status: str
    created_at: datetime
    completed_at: datetime | None


class SkillTestRunDetail(SkillTestRunSummary):
    """Full skill-test run including the per-case ``results`` array."""

    results: list[SkillTestCaseResult] = Field(default_factory=list)


class SkillWorkspaceLastRun(BaseModel):
    """Compact summary of the latest skill-test run for a skill."""

    model_config = ConfigDict(from_attributes=True)

    test_run_id: str
    kind: str = "scenario"
    overall_score: float | None = None
    test_set_size: int = 0
    passed_count: int = 0
    failed_count: int = 0
    partial_count: int = 0
    created_at: datetime


class SkillWorkspaceResponse(BaseModel):
    """Body of ``GET /caliber/skills/{skill_id}/workspace``.

    Surfaces the skill's runtime facts plus the computed lifecycle ``status``
    (Bound > Calibrated > Tested > Has scenarios > Draft). ``bound_to`` is read
    off the hidden skill target's ``optimizer_config`` (null when no target row
    exists yet).
    """

    model_config = ConfigDict(from_attributes=True)

    version: int | None = None
    category: str | None = None
    status: str
    lifecycle: str
    last_run: SkillWorkspaceLastRun | None = None
    baseline_run_id: str | None = None
    baseline_run: SkillWorkspaceLastRun | None = None
    bound_to: dict[str, Any] | None = None


class SkillBaselineRequest(BaseModel):
    """Body of ``POST /caliber/skills/{skill_id}/baseline``.

    Pins one persisted skill-test run as the comparison baseline for the skill.
    The run must belong to this skill (its ``skill_id`` must equal the path id).
    """

    model_config = ConfigDict(extra="forbid")

    test_run_id: str = Field(min_length=1, max_length=64)


class SkillBindRequest(BaseModel):
    """Body of ``POST /caliber/skills/{skill_id}/bind``.

    ``kind`` selects the bind target; the matching ids are required for each
    kind (``agent`` needs ``agent_id``; ``workflow_node`` needs ``workflow_id``
    and ``node_id``; ``standalone`` needs nothing).
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["agent", "workflow_node", "standalone"]
    agent_id: str | None = Field(default=None, max_length=64)
    workflow_id: str | None = Field(default=None, max_length=128)
    node_id: str | None = Field(default=None, max_length=128)


class SkillCalibrateRequest(BaseModel):
    """Body of ``POST /caliber/skills/{skill_id}/calibrate``.

    Deliberately minimal — calibrating a skill never requires "select an agent".
    The route auto-provisions the hidden skill target and queues the refinement
    job against it, so the only inputs are an optional optimizer choice and free
    notes.
    """

    model_config = ConfigDict(extra="forbid")

    optimizer_type: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=2048)


class SkillPackageFileSchema(BaseModel):
    """One generated file in an OpenAI-compatible skill package preview."""

    path: str
    kind: str
    content: str
    size_bytes: int = 0


class SkillPackageSchema(BaseModel):
    """OpenAI-compatible skill package preview."""

    root: str
    format: str
    files: list[SkillPackageFileSchema] = Field(default_factory=list)
    resource_counts: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    is_valid: bool = True


class SkillPackageFilePayload(BaseModel):
    """One uploaded file when importing a skill package."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=512)
    content: str


class SkillPackageImportRequest(BaseModel):
    """Body of ``POST /caliber/skills/import-package``."""

    model_config = ConfigDict(extra="forbid")

    owner: str = Field(min_length=1, max_length=256)
    category: str = Field(default="custom", pattern=SKILL_CATEGORY_PATTERN)
    tags: list[str] = Field(default_factory=list)
    skill_metadata: dict[str, object] = Field(default_factory=dict)
    allowed_tools: str | None = Field(default=None, max_length=512)
    depends_on: list[str] = Field(default_factory=list)
    files: list[SkillPackageFilePayload] = Field(min_length=1)


# ---------------------------------------------------------------------------
# Workflow-as-a-service (deploy workflow as a service).
# ---------------------------------------------------------------------------


class WorkflowServicePublishRequest(BaseModel):
    """Body of ``POST /caliber/workflows/{workflow_id}/service``.

    Every field is optional: ``alias`` defaults to ``prod`` and the I/O schemas
    are derived from the deployed version's manifest when omitted.
    """

    model_config = ConfigDict(extra="forbid")

    alias: str | None = Field(default=None, min_length=1, max_length=64)
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    enabled: bool | None = None
    # New services default to Bearer-token auth. Set false only for an
    # intentionally public endpoint.
    auth_required: bool | None = None


class WorkflowServiceSchema(BaseModel):
    """Serialized form of :class:`caliber.db.models.CaliberWorkflowService`."""

    model_config = ConfigDict(extra="forbid")

    service_id: str
    workflow_id: str
    alias: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    enabled: bool
    auth_required: bool = True
    endpoint: str
    created_by: str = ""
    created_at: datetime
    updated_at: datetime
    token_count: int = 0


class ServiceInvokeRequest(BaseModel):
    """Body of ``POST /caliber/services/{workflow_id}/invoke``."""

    model_config = ConfigDict(extra="forbid")

    input: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=128)


class ServiceInvokeResponse(BaseModel):
    """Run-and-poll acknowledgement returned by the invoke endpoint."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: str


class ServiceRunStatusSchema(BaseModel):
    """Poll response for a service-invoked run."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: str
    output: dict[str, Any] | None = None
    error: str | None = None
    trace_id: str | None = None


class ServiceTokenCreateRequest(BaseModel):
    """Body of ``POST /caliber/workflows/{workflow_id}/service/tokens``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    scopes: list[str] = Field(default_factory=lambda: ["invoke"])
    expires_at: datetime | None = None


class ServiceTokenSchema(BaseModel):
    """Masked serialized form of a service token (never exposes the secret)."""

    model_config = ConfigDict(extra="forbid")

    token_id: str
    name: str = ""
    prefix: str = ""
    scopes: list[str] = Field(default_factory=list)
    created_by: str = ""
    created_at: datetime
    expires_at: datetime | None = None
    revoked_at: datetime | None = None


class ServiceTokenCreatedSchema(ServiceTokenSchema):
    """Token creation response — carries the plaintext secret exactly once."""

    token: str
