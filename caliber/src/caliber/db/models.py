"""ORM model definitions.

These models are the source of truth for the CALIBER extension tables. They
must stay in sync with the Alembic migrations under ``db/migrations/versions/``;
the migration test (``tests/test_migrations.py``) verifies that a fresh
``alembic upgrade head`` produces the same schema as ``Base.metadata.create_all``.

For Phase 1.5 we ship two tables — ``caliber_agent_config`` and
``caliber_verification_queue`` — which are everything the verification-queue
read APIs need. Additional tables land alongside the features that use them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from caliber.db.base import Base


class CaliberAgentConfig(Base):
    """Per-agent configuration for the refinement pipeline.

    One row per agent CALIBER manages. ``experiment_id`` links to the MLflow
    experiment whose traces and assessments feed the verification queue;
    ``optimizer_config`` and ``eval_thresholds`` together determine how the
    pipeline routes and gates a refinement job for this agent.
    """

    __tablename__ = "caliber_agent_config"

    # Multi-user project scoping: optional project_id scopes a row to a tenant (null = global).
    # Existing rows are backfilled to visibility='public' by migration 0026.
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    visibility: Mapped[str] = mapped_column(
        String(16), nullable=False, default="project", server_default="project"
    )

    agent_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(256))
    owner: Mapped[str] = mapped_column(String(256))

    # JSON-shaped columns. SQLAlchemy maps JSON → JSONB on Postgres, TEXT on SQLite.
    artifact_types: Mapped[list[str]] = mapped_column(JSON, default=list)
    eval_thresholds: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    optimizer_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    approval_policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    optimize_for: Mapped[str] = mapped_column(String(16), default="quality")
    collaboration_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    required_approvals: Mapped[int] = mapped_column(Integer, default=1)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )


class CaliberVerificationItem(Base):
    """A single feedback item awaiting human verification.

    Created by the feedback poller when a new MLflow assessment arrives, or
    submitted directly via ``POST /caliber/verification-queue`` for operator
    feedback that isn't tied to a specific trace. ``assessment_id`` is the
    natural dedup key — its unique constraint makes poller retries idempotent.
    """

    __tablename__ = "caliber_verification_queue"
    __table_args__ = (UniqueConstraint("assessment_id", name="uq_verification_assessment_id"),)

    item_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(64), ForeignKey("caliber_agent_config.agent_id"))

    # MLflow linkage — all nullable because operator-submitted feedback has no trace.
    assessment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    experiment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Multi-turn / multi-agent context (used by Stories 2 and 4).
    session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # workflow_id will become a real FK to caliber_workflows in Phase 4; nullable
    # string for now so the column is forward-compatible.
    workflow_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Feedback content (denormalized from the MLflow assessment for queue queries).
    category: Mapped[str] = mapped_column(String(64))
    free_text: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(16))
    artifact_type_hint: Mapped[str | None] = mapped_column(String(32), nullable=True)
    artifact_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    submitted_context: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # Queue state.
    status: Mapped[str] = mapped_column(String(16), default="pending")
    priority: Mapped[int] = mapped_column(Integer, default=0)
    assigned_to: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # Verification decision (populated when status transitions to ``verified``).
    verified_by: Mapped[str | None] = mapped_column(String(256), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    verification_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    refinement_target: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Self-FK for dedup tracking.
    duplicate_of_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("caliber_verification_queue.item_id"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CaliberRefinementJob(Base):
    """A single refinement pipeline run.

    Created when a verification item is verified (``POST /.../verify``) or when
    an operator opens one manually. Each job moves through the 6-stage pipeline
    (triage → evidence → diagnosis → candidate → eval → approval). The current position is in ``current_stage``.

    ``mlflow_run_id`` is nullable because the parent MLflow run is created
    when the job *starts* running (i.e. leaves the ``queued`` state), not when
    the row is inserted.
    """

    __tablename__ = "caliber_refinement_jobs"
    __table_args__ = (Index("ix_refinement_jobs_status_created", "status", "created_at"),)

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(64), ForeignKey("caliber_agent_config.agent_id"))
    workflow_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    primary_item_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("caliber_verification_queue.item_id")
    )
    mlflow_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Pipeline state.
    artifact_type: Mapped[str] = mapped_column(String(32))
    optimizer_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="queued")
    current_stage: Mapped[str] = mapped_column(String(32), default="triage")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    # Automatic candidate→eval→re-candidate iterations spent on this job's
    # failed-gate self-correction loop (see config.refinement_max_iterations).
    refine_iteration: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # When ``artifact_type == "skill"``, the kebab-case name of the target
    # skill. The triage stage populates this from the verification item's
    # ``artifact_ref`` or by matching the feedback category to a skill the
    # agent references in ``optimizer_config.skills``. ``None`` for prompt jobs.
    skill_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Cost tracking — populated as the pipeline runs.
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    # Bundle scope tracking — populated when MultiAgentCoord runs (§6.6.4).
    bundle_targets: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    bundle_expansion_count: Mapped[int] = mapped_column(Integer, default=0)

    # Diagnosis output. Populated by the diagnosis
    # stage; consumed by the candidate-generation stage. Stored here for now;
    # when MLflow run integration lands the same payload will also be logged
    # as artifacts on the parent run.
    diagnosis: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # Candidate output. Populated by the
    # candidate-generation stage; consumed by Eval and Approval. For Phase
    # 2.8 holds the single selected candidate; when GEPA / multi-candidate
    # optimizers land, ``alternatives`` inside this JSON holds the others.
    candidate: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # Eval output. Populated by the Eval stage;
    # consumed by the Approval UI to render the per-dimension comparison.
    # Shape: ``{"candidate": {...}, "baseline": {...}|None, "deltas": {...},
    # "eval_dataset_id": "...", "n_examples": int, "gate": {"passed": bool, ...}}``.
    eval_results: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # Workflow calibration runs attach their bounded search/eval contract here.
    # ``None`` preserves the existing prompt and workflow refinement paths.
    calibration_spec: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # Reviewer change-request notes. Set by the request-changes endpoint
    # when an approver wants a new candidate with specific guidance. Read by
    # the candidate stage on the retry pass, then cleared. Stored as text
    # (not JSON) because it's free-form reviewer prose.
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Liveness signal the worker updates at the start of every stage.
    # The janitor (:mod:`caliber.orchestrator.janitor`) reaps
    # ``status='running'`` rows whose heartbeat is older than the
    # configured stale-threshold — that's how a SIGKILL'd worker mid-
    # stage gets cleaned up rather than leaving the job pinned to
    # ``running`` forever.
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )


class CaliberApprovalRequest(Base):
    """Born-``approved`` provenance anchor for a promoted candidate.

    Human-feedback approval governance was removed: refinement jobs that pass
    the eval gate land directly at ``candidate_ready``. When an operator
    applies the candidate (``POST /jobs/{id}/apply``) this row is minted
    already ``approved`` to anchor the promotion's rollback checkpoint(s) —
    the ``caliber_rollback_checkpoints.approval_id`` FK is NOT NULL — and to
    preserve the eval/candidate/diagnosis snapshots for the audit trail.

    Both ``eval_results`` and ``candidate_snapshot`` are copies of the
    upstream payloads — denormalized so historical promotions stay readable
    even after the source job moves on.
    """

    __tablename__ = "caliber_approval_requests"
    __table_args__ = (Index("ix_approval_requests_status_created", "status", "created_at"),)

    approval_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # ``job_id`` is intentionally *not* unique: a request-changes flow
    # produces multiple approval rows per job (one per attempt), and the
    # whole history is the audit trail.
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("caliber_refinement_jobs.job_id"))
    agent_id: Mapped[str] = mapped_column(String(64), ForeignKey("caliber_agent_config.agent_id"))

    status: Mapped[str] = mapped_column(String(16), default="pending")
    # pending | approved | rejected | request_changes

    # Which attempt this is. Set to ``job.attempt_count`` at creation. Lets
    # the UI render "Approval (attempt 2)" when a request-changes retry
    # produces a second approval row for the same job.
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)

    eval_results: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    candidate_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    diagnosis_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    approved_by: Mapped[str | None] = mapped_column(String(256), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )


class CaliberRollbackCheckpoint(Base):
    """A snapshot of the state immediately before a promotion.

    Created in the same transaction that runs the promoter. Holds the
    artifact-type-aware "what to restore" payload so a future rollback
    endpoint can return the artifact to its pre-promotion state without
    re-deriving it from MLflow tags or audit-log scanning.

    For prompt-type artifacts the snapshot is mostly the
    ``version_before`` integer + the prior alias target — the rollback
    is an ``mlflow.genai.set_prompt_alias`` call that rotates the alias
    back. For non-prompt artifacts the ``snapshot_payload`` JSON column
    carries whatever the per-type handler needs (file blob URI, dataset
    version, etc.).
    """

    __tablename__ = "caliber_rollback_checkpoints"
    __table_args__ = (Index("ix_rollback_agent_created", "agent_id", "created_at"),)

    checkpoint_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    approval_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("caliber_approval_requests.approval_id")
    )
    agent_id: Mapped[str] = mapped_column(String(64), ForeignKey("caliber_agent_config.agent_id"))
    artifact_type: Mapped[str] = mapped_column(String(32))
    artifact_name: Mapped[str] = mapped_column(String(256))

    # Before/after refs the rollback endpoint compares to decide whether
    # rolling back is even necessary. A no-op rollback (the active
    # version is already ``version_before``) returns 200 without writing
    # any state.
    artifact_ref_before: Mapped[str | None] = mapped_column(String(512), nullable=True)
    artifact_ref_after: Mapped[str] = mapped_column(String(512))
    version_before: Mapped[int | None] = mapped_column(Integer, nullable=True)
    version_after: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Type-specific snapshot. For prompts this stays mostly empty (the
    # version-int says everything). For skill files / config blobs the
    # caller stores the JSON-encoded artifact payload here.
    snapshot_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # Whether this checkpoint has been rolled back to. Set on rollback
    # so a second rollback against the same checkpoint is a no-op (which
    # the endpoint catches and surfaces as 409).
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rolled_back_by: Mapped[str | None] = mapped_column(String(256), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CaliberRegressionRun(Base):
    """Persisted replay/eval result used as the approval gate.

    The classic ``job.eval_results`` JSON blob is useful for rendering, but a
    named regression-run row gives reviewers and APIs a durable record of which
    replay actually allowed promotion. The approval endpoint only promotes when
    the latest required run for that approval has ``status='passed'``.
    """

    __tablename__ = "caliber_regression_runs"
    __table_args__ = (
        Index("ix_regression_runs_approval_created", "approval_id", "created_at"),
        Index("ix_regression_runs_job_created", "job_id", "created_at"),
    )

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("caliber_refinement_jobs.job_id"))
    approval_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("caliber_approval_requests.approval_id"), nullable=True
    )
    agent_id: Mapped[str] = mapped_column(String(64), ForeignKey("caliber_agent_config.agent_id"))

    candidate_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    # queued | running | passed | failed | error
    required_for_approval: Mapped[bool] = mapped_column(Boolean, default=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    dataset_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    trace_sample_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    baseline_scores: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    candidate_scores: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    deltas: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    regressions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    gate: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CaliberRuntimeLock(Base):
    """Singleton runtime leases and checkpoints.

    Used by background tasks that need a shared, durable piece of state:

    * The feedback poller stores its ``last_polled_at`` checkpoint here so a
      restart resumes from the right point instead of double-scanning.
    * A future multi-replica deployment uses ``owner`` + ``expires_at`` to
      arbitrate "who's the active poller right now."

    The table is deliberately small — one row per task. Combined with the
    unique constraint on ``caliber_verification_queue.assessment_id``, the
    pair makes the assessment-polling pipeline idempotent across worker
    restarts and replica failovers.
    """

    __tablename__ = "caliber_runtime_locks"

    lock_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner: Mapped[str] = mapped_column(String(128), default="")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )


class CaliberAuditLog(Base):
    """Append-only audit trail.

    Every state change CALIBER makes (verify, dismiss, create job, advance
    stage, approve, reject, rollback, register agent, register workflow) writes
    one row here. The table is the source of truth when an auditor asks "who
    did what to which artifact when, and why?"

    Append-only by convention: there's no ``UPDATE`` or ``DELETE`` path in the
    application code. Retention policy is handled at the DB layer.
    """

    __tablename__ = "caliber_audit_log"
    __table_args__ = (
        Index("ix_audit_log_entity", "entity_type", "entity_id"),
        Index("ix_audit_log_actor_timestamp", "actor", "timestamp"),
    )

    log_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    actor: Mapped[str] = mapped_column(String(256))
    action: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str] = mapped_column(String(128))
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class CaliberEvalDataset(Base):
    """Versioned evaluation dataset.

    An eval dataset is a fixed set of input/expected examples that the
    refinement pipeline scores candidate prompts against. ``name`` is
    the agent-cited handle (unique). ``version`` increments every time
    examples are added/changed via the dedicated PATCH path — so
    historical job runs can be pinned to the exact dataset version
    they evaluated against.

    The examples themselves live in
    :class:`CaliberEvalDatasetExample` so adding a row doesn't rewrite
    the dataset, and so a job's record of "I evaluated against
    dataset X version Y" stays compact.
    """

    __tablename__ = "caliber_eval_datasets"

    # Multi-user project scoping: optional project_id scopes a row to a tenant (null = global).
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    visibility: Mapped[str] = mapped_column(
        String(16), nullable=False, default="project", server_default="project"
    )
    __table_args__ = (UniqueConstraint("name", name="uq_eval_dataset_name"),)

    dataset_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    owner: Mapped[str] = mapped_column(String(256))
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="active")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # MLflow GenAI dataset sync linkage (MLflow 3.14 ``mlflow.genai.datasets``).
    # CALIBER stays the source of truth; these record the last push to MLflow's
    # native dataset registry so the revamped dataset UI + source-trace lineage
    # light up. All nullable — a never-synced dataset has them all null.
    mlflow_dataset_id: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    mlflow_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    # The CALIBER ``version`` captured at sync time — lets the UI flag
    # "synced, but the set changed since" when mlflow_synced_version < version.
    mlflow_synced_version: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    mlflow_record_count: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    mlflow_digest: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)


class CaliberEvalDatasetExample(Base):
    """One input/expected pair belonging to an eval dataset.

    ``dataset_version`` snapshots which dataset version the example
    first appeared at — so when an operator pins a job's eval to
    "version 3" we can list exactly the examples that were active at
    that time. Examples are append-only (no edit-in-place) to keep the
    audit trail clean; ``superseded_at`` marks an example as
    "retired" without deleting it, and ``superseded_version`` records
    the dataset version *at which* it was retired so a pinned run can
    reconstruct the exact active set "as of version N": include rows
    with ``dataset_version <= N`` and exclude rows whose
    ``superseded_version`` is non-null and ``<= N``.
    """

    __tablename__ = "caliber_eval_dataset_examples"
    __table_args__ = (Index("ix_eval_example_dataset", "dataset_id"),)

    example_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("caliber_eval_datasets.dataset_id")
    )
    dataset_version: Mapped[int] = mapped_column(Integer)
    input: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    expected: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    superseded_version: Mapped[int | None] = mapped_column(Integer, nullable=True)


class CaliberJudge(Base):
    """A reusable custom LLM judge (MLflow 3.14 ``mlflow.genai.make_judge``).

    Operators author a judge once — a name, natural-language ``instructions``
    referencing the ``{{ inputs }}`` / ``{{ outputs }}`` / ``{{ expectations }}``
    evaluation variables, and a model — and then select it as a scorer in eval
    runs. At evaluate-time the runner reconstructs it via ``make_judge`` and
    passes it to ``mlflow.genai.evaluate`` alongside the built-in scorers, so the
    judge's verdict + rationale land on the run as a normal MLflow feedback.

    CALIBER is the source of truth (the definition lives here, not in MLflow);
    the judge is rebuilt deterministically from these fields on every run.
    """

    __tablename__ = "caliber_judges"

    # Multi-user project scoping (null = global), mirroring the other assets.
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    visibility: Mapped[str] = mapped_column(
        String(16), nullable=False, default="project", server_default="project"
    )
    __table_args__ = (UniqueConstraint("name", name="uq_judge_name"),)

    judge_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    # NL instructions; must reference at least one template var (validated in the schema).
    instructions: Mapped[str] = mapped_column(Text, default="")
    # MLflow model identifier, e.g. ``openai:/gpt-4o-mini``. Null → the runner
    # falls back to CALIBER's configured eval model.
    model: Mapped[str | None] = mapped_column(String(128), nullable=True, default=None)
    # Optional feedback value type hint: ``bool`` | ``int`` | ``float`` | ``str``.
    feedback_value_type: Mapped[str | None] = mapped_column(String(16), nullable=True, default=None)
    owner: Mapped[str] = mapped_column(String(256))
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class CaliberLlmModelPricing(Base):
    """Operator-configured per-model LLM token pricing (USD per 1K tokens).

    CALIBER computes the ``cost_usd`` it records on trace spans + refinement jobs
    from a per-model price table. A built-in default table ships in
    :mod:`caliber.observability.mlflow_tracing` (``DEFAULT_MODEL_PRICING``); rows
    here **override / extend** it so operators can correct rates or add models
    without a code change. Cost is recomputed at record-time, so a rate edit
    applies to *new* traffic (historical span costs are immutable).

    Source of truth is CALIBER (not the gateway — MLflow's gateway exposes USD
    budget *policies*, not editable per-token rates). Surfaced + edited in the
    LLM Gateway page's Pricing tab.
    """

    __tablename__ = "caliber_llm_model_pricing"

    # Multi-user project scoping (null = global), mirroring the other assets.
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    visibility: Mapped[str] = mapped_column(
        String(16), nullable=False, default="project", server_default="project"
    )
    # One pricing row per (provider, model) — the natural key operators reason about.
    __table_args__ = (
        UniqueConstraint("provider", "model_id", name="uq_llm_pricing_provider_model"),
    )

    pricing_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64))
    model_id: Mapped[str] = mapped_column(String(128))
    # USD per 1K tokens.
    prompt_price: Mapped[float] = mapped_column(Float, default=0.0)
    completion_price: Mapped[float] = mapped_column(Float, default=0.0)
    # Optional discounted rate for cached prompt tokens; null → falls back to prompt_price.
    cached_prompt_price: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    owner: Mapped[str] = mapped_column(String(256))
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class CaliberReviewQueue(Base):
    """A structured human-review queue over observed traces.

    The CALIBER-native analogue of MLflow's Review Queues (which are
    Databricks-only): a named collection of trace items + a label schema of
    review ``questions`` (pass/fail, categorical, numeric, free-text) + assigned
    reviewers. Reviewer answers are written straight back onto the trace as
    MLflow assessments (feedback questions) or expectations (ground-truth
    questions) via the OSS ``mlflow.log_feedback`` / ``mlflow.log_expectation``
    primitives, so the output immediately feeds eval, judge-alignment, and
    dataset building.

    ``questions`` is a list of ``{key, title, type, options?, required, target}``
    where ``type`` ∈ pass_fail|categorical|numeric|text and ``target`` ∈
    feedback|expectation.
    """

    __tablename__ = "caliber_review_queues"

    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    visibility: Mapped[str] = mapped_column(
        String(16), nullable=False, default="project", server_default="project"
    )
    __table_args__ = (UniqueConstraint("name", name="uq_review_queue_name"),)

    queue_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    questions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    reviewers: Mapped[list[str]] = mapped_column(JSON, default=list)
    owner: Mapped[str] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class CaliberReviewItem(Base):
    """One trace queued for review within a :class:`CaliberReviewQueue`.

    ``answers`` holds the reviewer's responses keyed by question key; once
    submitted the answers are written back to the trace and their resulting
    MLflow assessment ids are recorded in ``assessment_ids`` for provenance.
    """

    __tablename__ = "caliber_review_items"
    __table_args__ = (
        Index("ix_review_item_queue", "queue_id"),
        UniqueConstraint("queue_id", "trace_id", name="uq_review_item_queue_trace"),
    )

    item_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    queue_id: Mapped[str] = mapped_column(String(64), ForeignKey("caliber_review_queues.queue_id"))
    trace_id: Mapped[str] = mapped_column(String(256))
    experiment_id: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    assigned_to: Mapped[str | None] = mapped_column(String(256), nullable=True, default=None)
    answers: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    assessment_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    completed_by: Mapped[str | None] = mapped_column(String(256), nullable=True, default=None)


class CaliberAriaPlan(Base):
    """A goal-plan — Aria's durable, first-class decomposition of a user goal.

    Phase 1 of the agentic-orchestration plan
    (``docs/12-assistant/aria-agentic-orchestration.md``). A plan turns one goal
    into an ordered/DAG set of :class:`CaliberAriaPlanStep` rows, each invoking a
    registered capability. The plan is the assistant's persisted "todo list": it
    survives across turns and (in later phases) across long async jobs, and it is
    observable + auditable end to end.

    ``autonomy`` is the per-plan dial (``ask_each`` / ``approve_plan`` /
    ``auto_guarded``) that the executor (Phase 2) consults per step tier.
    """

    __tablename__ = "caliber_aria_plans"

    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    visibility: Mapped[str] = mapped_column(
        String(16), nullable=False, default="user", server_default="user"
    )

    plan_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    goal: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="draft")
    autonomy: Mapped[str] = mapped_column(String(16), default="approve_plan")
    constraints: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    done_when: Mapped[list[str]] = mapped_column(JSON, default=list)
    context_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    owner: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class CaliberAriaPlanStep(Base):
    """One step of a :class:`CaliberAriaPlan` — a single capability invocation.

    ``depends_on`` carries the DAG edges (a list of sibling ``step_id`` s).
    Lineage columns link a step to the concrete artifacts it produced (a draft, an
    async job, an approval request, a checkpoint) so "how Aria built this" is fully
    traceable.
    """

    __tablename__ = "caliber_aria_plan_steps"
    __table_args__ = (Index("ix_aria_plan_step_plan", "plan_id"),)

    step_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    plan_id: Mapped[str] = mapped_column(String(64), ForeignKey("caliber_aria_plans.plan_id"))
    seq: Mapped[int] = mapped_column(Integer)
    capability_key: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(256), default="")
    inputs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    depends_on: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Optional quality gate, e.g. ``{"metric": "score", "min": 0.9}``. When set,
    # a completed step whose evidence/result falls below the gate escalates a
    # confirm interaction instead of silently passing (self-correction).
    gate: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=None)
    error: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    # Lineage links to produced artifacts (all nullable).
    draft_id: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    approval_id: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    checkpoint_id: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class CaliberAriaInteraction(Base):
    """A mid-run interaction — Aria pausing a plan step to ask the human.

    Phase 2 of the agentic-orchestration plan. When the autonomy dial says a step
    needs a human (any gated step always; mutate/safe under ``ask_each``), the
    executor records one of these and pauses the plan; answering it resumes
    execution. ``kind`` is ``permission`` | ``choice`` | ``input`` | ``confirm``;
    ``options`` carries the choices for a ``choice`` ask. ``required_scope`` lets a
    gated approval demand an approver authority (the distinct-identity check is
    Phase 3).
    """

    __tablename__ = "caliber_aria_interactions"
    __table_args__ = (Index("ix_aria_interaction_plan", "plan_id"),)

    interaction_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    plan_id: Mapped[str] = mapped_column(String(64), ForeignKey("caliber_aria_plans.plan_id"))
    step_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16), default="permission")
    prompt: Mapped[str] = mapped_column(Text, default="")
    options: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    required_scope: Mapped[str | None] = mapped_column(String(32), nullable=True, default=None)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    response: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    responded_by: Mapped[str | None] = mapped_column(String(256), nullable=True, default=None)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CaliberEvalRun(Base):
    """One ad-hoc evaluation run scoring a dataset's examples (the scorecard).

    Distinct from the refinement-pipeline gate (which compares a candidate
    prompt to a baseline and records only an aggregate on the approval): this
    is the standalone "run the dataset through a predict target + scorers and
    show me the per-example results" surface that mirrors MLflow's evaluation
    UI. Stores the full per-row payload inline in ``results`` (heavy, omitted
    from list summaries) plus server-recomputed scalar/JSON summaries for cheap
    history listing — the same shape as :class:`CaliberPromptTestRun`.

    ``dataset_version`` pins the exact example set scored so a run stays
    reproducible after the dataset grows. ``predict_target`` records how
    predictions were produced (``llm`` = the configured model; ``reference`` =
    a labelled diagnostic that echoes the expected answer).
    """

    __tablename__ = "caliber_eval_runs"
    __table_args__ = (
        Index("ix_eval_runs_dataset_created", "dataset_id", "created_at"),
        Index("ix_eval_runs_created", "created_at"),
    )

    # Multi-user project scoping (mirrors CaliberEvalDataset).
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    visibility: Mapped[str] = mapped_column(
        String(16), nullable=False, default="project", server_default="project"
    )

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("caliber_eval_datasets.dataset_id")
    )
    dataset_version: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(256), default="")
    predict_target: Mapped[str] = mapped_column(String(32), default="llm")
    # The artifact under test for non-``llm`` targets: a prompt ref
    # (``<name>@<version>``) or a skill id. Null for the generic ``llm`` target.
    subject_ref: Mapped[str | None] = mapped_column(String(256), nullable=True, default=None)
    model: Mapped[str | None] = mapped_column(String(256), nullable=True)
    scorers: Mapped[list[str]] = mapped_column(JSON, default=list)
    pass_threshold: Mapped[float] = mapped_column(Float, default=0.5)
    n_examples: Mapped[int] = mapped_column(Integer, default=0)
    passed_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    overall_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    pass_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Per-scorer aggregate means: {"exact_match": 0.8, ...}.
    aggregate: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Full per-example array (example_id/input/expected/prediction/scores/
    # score/passed/error). Heavy — omitted from history summaries.
    results: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(24), default="completed")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CaliberSkill(Base):
    """Reusable prompt fragment that one or more agents compose into their prompts.

    A skill is a freestanding artifact — instructions for tool use,
    safety guardrails, formatting conventions, reasoning rubrics — that
    a refinement loop might update independently of any single agent's
    main prompt. Multiple agents can reference the same skill by
    ``name`` so an improvement landed once benefits the whole fleet.

    ``name`` is the human-facing handle (``"reasoning-v2"``,
    ``"tool-use"``) and is unique — operators reference skills by name
    in agent ``optimizer_config`` and in skill-only refinement jobs.
    Names must be kebab-case (lowercase letters, digits, hyphens).
    ``skill_id`` is the surrogate primary key used in URLs.

    The skill structure follows the progressive-disclosure pattern:

    * **summary** (level 1) — short text always loaded into the
      agent's context so it knows *when* to activate the skill.
    * **content** (level 2) — full instructions loaded only when
      the skill is relevant to the current task.

    ``category`` classifies the skill's primary use-case:

    * ``document_creation`` — consistent documents, assets, code
    * ``workflow_automation`` — multi-step processes
    * ``mcp_enhancement`` — workflow guidance layered on MCP tools
    * ``custom`` — anything that doesn't fit the first three

    ``metadata`` is an open JSON bag for extra key-value pairs
    (author, mcp-server, documentation URL, etc.).

    ``allowed_tools`` restricts which tools the skill may invoke
    (e.g. ``"Bash(python:*) WebFetch"``).  ``None`` means no
    restriction.

    ``depends_on`` lists skill names this skill composes with —
    enabling composability checks and topological ordering.

    ``status`` follows the soft-delete convention used elsewhere
    (``active`` / ``archived``) so a skill that's no longer wanted can
    be removed from the active set without losing audit history. Hard
    deletes are reserved for the rare "accidentally created" case and
    aren't exposed via the API.
    """

    __tablename__ = "caliber_skills"

    # Multi-user project scoping: optional project_id scopes a row to a tenant (null = global).
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    visibility: Mapped[str] = mapped_column(
        String(16), nullable=False, default="project", server_default="project"
    )
    __table_args__ = (UniqueConstraint("name", name="uq_skill_name"),)

    skill_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    content: Mapped[str] = mapped_column(Text)
    owner: Mapped[str] = mapped_column(String(256))
    category: Mapped[str] = mapped_column(String(32), default="custom")
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    skill_metadata: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    allowed_tools: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    depends_on: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="active")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )


class CaliberWorkflow(Base):
    """Workflow Studio workflow root."""

    __tablename__ = "caliber_workflows"

    # Multi-user project scoping: optional project_id scopes a row to a tenant (null = global).
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    visibility: Mapped[str] = mapped_column(
        String(16), nullable=False, default="project", server_default="project"
    )

    workflow_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    description: Mapped[str] = mapped_column(Text, default="")
    owner: Mapped[str] = mapped_column(String(256), default="")
    status: Mapped[str] = mapped_column(String(16), default="active")
    default_experiment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )


class CaliberWorkflowVersion(Base):
    """Immutable or draft manifest version for a workflow."""

    __tablename__ = "caliber_workflow_versions"
    __table_args__ = (
        UniqueConstraint("workflow_id", "version_number", name="uq_workflow_version_number"),
        Index("ix_workflow_versions_workflow", "workflow_id", "version_number"),
    )

    version_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("caliber_workflows.workflow_id")
    )
    version_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="draft")
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON)
    manifest_hash: Mapped[str] = mapped_column(String(64), default="")
    compiler_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    compiled_artifact_uri: Mapped[str | None] = mapped_column(String(512), nullable=True)
    validation_report: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    compiled_bundle: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_by: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    published_by: Mapped[str | None] = mapped_column(String(256), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CaliberWorkflowDeployment(Base):
    """Mutable deployment alias pointing at a workflow version."""

    __tablename__ = "caliber_workflow_deployments"
    __table_args__ = (
        UniqueConstraint("workflow_id", "alias", name="uq_workflow_deployment_alias"),
    )

    deployment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("caliber_workflows.workflow_id")
    )
    alias: Mapped[str] = mapped_column(String(64))
    version_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("caliber_workflow_versions.version_id")
    )
    environment: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active")
    deployed_by: Mapped[str | None] = mapped_column(String(256), nullable=True)
    deployed_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    rollback_checkpoint: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)


class CaliberToolRegistry(Base):
    """Registered Python callable exposed to Workflow Studio."""

    __tablename__ = "caliber_tool_registry"

    # Multi-user project scoping: optional project_id scopes a row to a tenant (null = global).
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    visibility: Mapped[str] = mapped_column(
        String(16), nullable=False, default="project", server_default="project"
    )
    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_tool_name_version"),
        Index("ix_tool_registry_name", "name"),
    )

    tool_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    version: Mapped[str] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(Text, default="")
    module_path: Mapped[str] = mapped_column(String(512))
    callable_name: Mapped[str] = mapped_column(String(128))
    input_schema: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    output_schema: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    side_effect_level: Mapped[str] = mapped_column(String(16), default="read")
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_in_preview: Mapped[bool] = mapped_column(Boolean, default=False)
    secret_refs: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Saved calibration test cases (flat list of {name, input, assertion}) and
    # the latest scored calibration result for this tool. See routes/tools.py.
    test_cases: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, server_default="[]"
    )
    last_calibration: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # Pinned comparison baseline: a CaliberToolTestRun.test_run_id the Runs tab
    # diffs the current run against. Null until an operator sets one.
    baseline_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    owner: Mapped[str] = mapped_column(String(256), default="")
    status: Mapped[str] = mapped_column(String(16), default="active")
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    successor_tool_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )


class CaliberWorkflowRun(Base):
    """Workflow execution index row linking CALIBER runs to MLflow traces."""

    __tablename__ = "caliber_workflow_runs"
    __table_args__ = (
        Index("ix_workflow_runs_workflow", "workflow_id", "started_at"),
        Index("ix_workflow_runs_trace", "trace_id"),
        Index("ix_workflow_runs_queue_claim", "status", "priority", "queued_at"),
        Index("ix_workflow_runs_lease", "status", "lease_expires_at"),
        Index(
            "ix_workflow_runs_idempotency",
            "workflow_id",
            "source",
            "idempotency_key",
            unique=True,
            sqlite_where=text("idempotency_key IS NOT NULL"),
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    workflow_run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(String(128))
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default="local")
    workflow_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deployment_alias: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mlflow_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="completed")
    source: Mapped[str] = mapped_column(String(32), default="manual")
    priority: Mapped[int] = mapped_column(Integer, default=0)
    queued_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)
    parent_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancel_requested_by: Mapped[str | None] = mapped_column(String(256), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_node_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    input_file_ref: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # Untruncated run input for the async/queue path (summary keeps only a
    # 1000-char preview). Read by the worker for execution; not in WorkflowRunSchema.
    input_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Immutable per-run workflow manifest copy. Draft editor runs persist the
    # unsaved snapshot here; saved-version runs persist the resolved version
    # manifest so replay/debugging survives later version churn or deletion.
    manifest_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class CaliberWorkflowRunEvent(Base):
    """Durable, append-only timeline events for one workflow run."""

    __tablename__ = "caliber_workflow_run_events"
    __table_args__ = (
        UniqueConstraint("workflow_run_id", "sequence", name="uq_workflow_run_event_sequence"),
        Index("ix_workflow_run_events_run_sequence", "workflow_run_id", "sequence"),
        Index("ix_workflow_run_events_run_created", "workflow_run_id", "created_at"),
        Index("ix_workflow_run_events_type_created", "event_type", "created_at"),
    )

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workflow_run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("caliber_workflow_runs.workflow_run_id")
    )
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(64))
    node_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CaliberWorkflowRunCheckpoint(Base):
    """Persisted checkpoint snapshots for resumable workflow runs."""

    __tablename__ = "caliber_workflow_run_checkpoints"
    __table_args__ = (
        UniqueConstraint("workflow_run_id", "sequence", name="uq_workflow_run_checkpoint_sequence"),
        Index("ix_workflow_run_checkpoints_run_sequence", "workflow_run_id", "sequence"),
        Index("ix_workflow_run_checkpoints_run_created", "workflow_run_id", "created_at"),
    )

    checkpoint_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workflow_run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("caliber_workflow_runs.workflow_run_id")
    )
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sequence: Mapped[int] = mapped_column(Integer)
    node_id: Mapped[str] = mapped_column(String(128))
    state_blob: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CaliberRuntimeApprovalRequest(Base):
    """In-run human approval request created by runtime approval gates."""

    __tablename__ = "caliber_runtime_approval_requests"
    __table_args__ = (
        Index("ix_runtime_approvals_run_status", "workflow_run_id", "status"),
        Index("ix_runtime_approvals_project_status", "project_id", "status"),
    )

    runtime_approval_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workflow_run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("caliber_workflow_runs.workflow_run_id")
    )
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    node_id: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    requested_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(256), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    policy_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class CaliberProject(Base):
    """A workspace/project that groups uploaded files (and future resources).

    Files reference a project via ``CaliberWorkflowFile.project_id``; the storage
    namespace is ``tenant/{tenant_id}/project/{project_id}/...`` (storage doc §2.1).
    """

    __tablename__ = "caliber_projects"
    __table_args__ = (UniqueConstraint("name", name="uq_project_name"),)

    project_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default="local")
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    owner: Mapped[str] = mapped_column(String(256), default="")
    status: Mapped[str] = mapped_column(String(16), default="active")
    storage_backend: Mapped[str] = mapped_column(String(16), default="local")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class CaliberWorkflowFile(Base):
    """File metadata for run/playground/dataset-scoped files (storage doc §4.6).

    The DB is the source of truth for file metadata (not bucket listings). One
    row per physical object; maps 1:1 to ``CaliberFileRecord``. ``relative_path``
    is prefix-excluded (storage doc §0.1 rule 3); the full object key is
    ``{kind-segment}/{relative_path}`` under the run namespace.
    """

    __tablename__ = "caliber_workflow_files"
    __table_args__ = (
        Index("ix_workflow_files_run", "workflow_run_id", "kind", "relative_path"),
        Index("ix_workflow_files_dataset", "dataset_id", "example_id"),
        Index("ix_workflow_files_playground", "playground_run_id"),
    )

    file_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default="local")
    project_id: Mapped[str] = mapped_column(String(64), default="default")
    workflow_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    workflow_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    workflow_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    playground_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    dataset_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    example_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parent_file_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(512))
    relative_path: Mapped[str] = mapped_column(String(1024))
    file_ref: Mapped[str] = mapped_column(String(1024), unique=True)
    storage_backend: Mapped[str] = mapped_column(String(16))
    storage_uri: Mapped[str] = mapped_column(String(2048))
    bucket: Mapped[str | None] = mapped_column(String(256), nullable=True)
    object_key: Mapped[str] = mapped_column(String(2048))
    object_version_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    etag: Mapped[str | None] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending_upload")
    version: Mapped[int] = mapped_column(Integer, default=1)
    producer_node_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    producer_tool_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_by: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # ``metadata`` is reserved on the declarative Base, so the attribute is
    # ``file_metadata`` while the column keeps the documented name ``metadata``.
    file_metadata: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSON, nullable=True)


class CaliberEvalDatasetFile(Base):
    """Join/role table linking dataset examples to physical files (storage doc §7.3).

    The file *bytes* live in a ``caliber_workflow_files`` row (with ``dataset_id``/
    ``example_id`` set); this table records the role the file plays in an example
    plus the artifact-comparison ``match_spec``. ``kind`` uses the dataset
    vocabulary: ``input · expected · reference · rubric · fixture``.
    """

    __tablename__ = "caliber_eval_dataset_files"
    __table_args__ = (Index("ix_eval_dataset_files_dataset", "dataset_id", "example_id"),)

    dataset_file_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(String(64))
    example_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    role: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(512))
    file_id: Mapped[str] = mapped_column(String(64), ForeignKey("caliber_workflow_files.file_id"))
    match_spec: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    file_metadata: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CaliberWorkflowFileEvent(Base):
    """Operational telemetry for file operations (storage doc §4.6, §8.6).

    Non-security telemetry. Security-relevant events also flow through
    ``audit.record`` so they inherit redaction (storage doc §8.6).
    """

    __tablename__ = "caliber_workflow_file_events"
    __table_args__ = (
        Index("ix_workflow_file_events_file", "file_id"),
        Index("ix_workflow_file_events_run", "workflow_run_id"),
    )

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    file_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    workflow_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor: Mapped[str] = mapped_column(String(256))
    action: Mapped[str] = mapped_column(String(64))
    relative_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    status: Mapped[str] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_metadata: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CaliberLiveEvent(Base):
    """Cross-process live event fanout for SSE-capable UI surfaces.

    Each row stores one JSON-safe event payload emitted through the shared
    runtime event bus. Replica-local subscribers still receive events
    immediately in memory; peer replicas tail this table to forward the same
    events to their own SSE clients without requiring NATS.
    """

    __tablename__ = "caliber_live_events"
    __table_args__ = (
        Index("ix_live_events_created", "created_at"),
        Index("ix_live_events_origin_id", "origin", "event_id"),
        Index("ix_live_events_type_created", "event_type", "created_at"),
    )

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    origin: Mapped[str] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CaliberKnowledgeBase(Base):
    """Versioned document corpus prepared for chunking, embeddings, and RAG."""

    __tablename__ = "caliber_knowledge_bases"
    __table_args__ = (
        Index("ix_knowledge_bases_owner_status", "owner", "status"),
        Index("ix_knowledge_bases_project_status", "project_id", "status"),
    )

    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    visibility: Mapped[str] = mapped_column(
        String(16), nullable=False, default="project", server_default="project"
    )

    knowledge_base_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    description: Mapped[str] = mapped_column(Text, default="")
    owner: Mapped[str] = mapped_column(String(256), default="")
    status: Mapped[str] = mapped_column(String(16), default="active")
    source_bucket: Mapped[str] = mapped_column(String(256))
    source_manifest: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    source_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    active_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The calibration run pinned as the comparison baseline for this KB. Points
    # at a :class:`CaliberKnowledgeBaseTestRun`; set via the baseline route.
    baseline_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_run_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_run_completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )


class CaliberKnowledgeBaseVersion(Base):
    """One immutable chunking/embedding build of a knowledge base."""

    __tablename__ = "caliber_knowledge_base_versions"
    __table_args__ = (
        UniqueConstraint(
            "knowledge_base_id",
            "version_number",
            name="uq_knowledge_base_version_number",
        ),
        Index(
            "ix_knowledge_base_versions_lookup",
            "knowledge_base_id",
            "version_number",
        ),
        Index("ix_knowledge_base_versions_status_created", "status", "created_at"),
    )

    knowledge_base_version_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    knowledge_base_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("caliber_knowledge_bases.knowledge_base_id")
    )
    version_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="processing")
    chunking_strategy: Mapped[str] = mapped_column(String(64))
    chunking_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    graph_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    embedding_provider: Mapped[str] = mapped_column(String(32), default="huggingface")
    embedding_model: Mapped[str] = mapped_column(String(256))
    embedding_dimension: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_manifest: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    source_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    output_bucket: Mapped[str] = mapped_column(String(256))
    output_prefix: Mapped[str] = mapped_column(String(1024))
    chunks_uri: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    entities_uri: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    relationships_uri: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    graph_uri: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    manifest_uri: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    logs_uri: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    stats_uri: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CaliberKnowledgeBaseSource(Base):
    """A concrete source document (or expansion result) inside a build version."""

    __tablename__ = "caliber_knowledge_base_sources"
    __table_args__ = (
        Index(
            "ix_knowledge_base_sources_version",
            "knowledge_base_version_id",
            "document_id",
        ),
        Index(
            "ix_knowledge_base_sources_bucket_key",
            "bucket",
            "object_key",
        ),
    )

    knowledge_base_source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    knowledge_base_version_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("caliber_knowledge_base_versions.knowledge_base_version_id"),
    )
    document_id: Mapped[str] = mapped_column(String(64))
    selection_kind: Mapped[str] = mapped_column(String(16))
    bucket: Mapped[str] = mapped_column(String(256))
    object_key: Mapped[str] = mapped_column(String(2048))
    object_name: Mapped[str] = mapped_column(String(512))
    object_store_path: Mapped[str] = mapped_column(String(2048))
    content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    etag: Mapped[str | None] = mapped_column(String(256), nullable=True)
    last_modified: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    extracted_chars: Mapped[int] = mapped_column(Integer, default=0)
    extracted_format: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ocr_used: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(24), default="processed")
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_metadata: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CaliberKnowledgeBaseChunk(Base):
    """One chunk plus its embedding and lineage metadata."""

    __tablename__ = "caliber_knowledge_base_chunks"
    __table_args__ = (
        Index(
            "ix_knowledge_base_chunks_version_ordinal",
            "knowledge_base_version_id",
            "ordinal",
        ),
        Index(
            "ix_knowledge_base_chunks_version_document",
            "knowledge_base_version_id",
            "document_id",
            "chunk_index",
        ),
    )

    knowledge_base_chunk_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    knowledge_base_version_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("caliber_knowledge_base_versions.knowledge_base_version_id"),
    )
    document_id: Mapped[str] = mapped_column(String(64))
    source_bucket: Mapped[str] = mapped_column(String(256))
    source_key: Mapped[str] = mapped_column(String(2048))
    source_name: Mapped[str] = mapped_column(String(512))
    chunk_index: Mapped[int] = mapped_column(Integer)
    ordinal: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    start_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding: Mapped[list[float]] = mapped_column(JSON, default=list)
    chunk_metadata: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CaliberKnowledgeBaseEntity(Base):
    """One extracted entity aggregated from a knowledge-base version."""

    __tablename__ = "caliber_knowledge_base_entities"
    __table_args__ = (
        UniqueConstraint(
            "knowledge_base_version_id",
            "entity_key",
            name="uq_knowledge_base_entity_key",
        ),
        Index(
            "ix_knowledge_base_entities_version_mentions",
            "knowledge_base_version_id",
            "mention_count",
        ),
    )

    knowledge_base_entity_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    knowledge_base_version_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("caliber_knowledge_base_versions.knowledge_base_version_id"),
    )
    entity_key: Mapped[str] = mapped_column(String(256))
    label: Mapped[str] = mapped_column(String(512))
    entity_type: Mapped[str] = mapped_column(String(64), default="term")
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    mention_count: Mapped[int] = mapped_column(Integer, default=0)
    source_documents: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_keys: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_chunks: Mapped[list[str]] = mapped_column(JSON, default=list)
    entity_metadata: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CaliberKnowledgeBaseRelationship(Base):
    """A graph edge inferred between two version-scoped entities."""

    __tablename__ = "caliber_knowledge_base_relationships"
    __table_args__ = (
        UniqueConstraint(
            "knowledge_base_version_id",
            "source_entity_id",
            "target_entity_id",
            "relationship_type",
            name="uq_knowledge_base_relationship_edge",
        ),
        Index(
            "ix_knowledge_base_relationships_version_weight",
            "knowledge_base_version_id",
            "weight",
        ),
    )

    knowledge_base_relationship_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    knowledge_base_version_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("caliber_knowledge_base_versions.knowledge_base_version_id"),
    )
    source_entity_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("caliber_knowledge_base_entities.knowledge_base_entity_id"),
    )
    target_entity_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("caliber_knowledge_base_entities.knowledge_base_entity_id"),
    )
    relationship_type: Mapped[str] = mapped_column(String(64), default="co_occurs")
    weight: Mapped[float] = mapped_column(Float, default=0.0)
    evidence_chunk_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_documents: Mapped[list[str]] = mapped_column(JSON, default=list)
    relationship_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JSON, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CaliberKnowledgeBaseRun(Base):
    """Operational record of one knowledge-base build execution."""

    __tablename__ = "caliber_knowledge_base_runs"
    __table_args__ = (
        Index("ix_knowledge_base_runs_base_created", "knowledge_base_id", "created_at"),
        Index("ix_knowledge_base_runs_status_created", "status", "created_at"),
        Index("ix_knowledge_base_runs_status_queued", "status", "queued_at"),
    )

    knowledge_base_run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    knowledge_base_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("caliber_knowledge_bases.knowledge_base_id")
    )
    knowledge_base_version_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("caliber_knowledge_base_versions.knowledge_base_version_id"),
    )
    status: Mapped[str] = mapped_column(String(24), default="queued")
    source_manifest: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    log_line_count: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    queued_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CaliberKnowledgeBaseRunEvent(Base):
    """Append-only timeline events for one knowledge-base build."""

    __tablename__ = "caliber_knowledge_base_run_events"
    __table_args__ = (
        UniqueConstraint(
            "knowledge_base_run_id",
            "sequence",
            name="uq_knowledge_base_run_event_sequence",
        ),
        Index(
            "ix_knowledge_base_run_events_run_sequence",
            "knowledge_base_run_id",
            "sequence",
        ),
    )

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    knowledge_base_run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("caliber_knowledge_base_runs.knowledge_base_run_id")
    )
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CaliberWorkflowPatch(Base):
    """Generated workflow candidate patch produced by refinement."""

    __tablename__ = "caliber_workflow_patches"
    __table_args__ = (Index("ix_workflow_patches_job", "job_id"),)

    patch_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    workflow_id: Mapped[str] = mapped_column(String(128))
    base_version_id: Mapped[str] = mapped_column(String(64))
    candidate_manifest: Mapped[dict[str, Any]] = mapped_column(JSON)
    semantic_ops: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    patch_summary: Mapped[str] = mapped_column(Text, default="")
    graph_diff: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    risk_summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CaliberWorkflowPromotion(Base):
    """Reviewable promotion request for a gated workflow deployment alias."""

    __tablename__ = "caliber_workflow_promotions"
    __table_args__ = (Index("ix_workflow_promotions_lookup", "workflow_id", "alias", "status"),)

    promotion_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("caliber_workflows.workflow_id")
    )
    alias: Mapped[str] = mapped_column(String(64))
    version_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("caliber_workflow_versions.version_id")
    )
    status: Mapped[str] = mapped_column(String(16), default="pending")
    gate_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    requested_by: Mapped[str] = mapped_column(String(256), default="")
    requested_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    decided_by: Mapped[str | None] = mapped_column(String(256), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class CaliberWorkflowSessionMemory(Base):
    """Persistent per-workflow, per-agent conversation state."""

    __tablename__ = "caliber_workflow_session_memory"
    __table_args__ = (Index("ix_wf_session_memory_lookup", "workflow_id", "session_id"),)

    workflow_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("caliber_workflows.workflow_id"), primary_key=True
    )
    node_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    message_history: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    turn_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )


class CaliberWorkflowBenchmarkReport(Base):
    """Saved workflow-product bakeoff worksheet and scorecard."""

    __tablename__ = "caliber_workflow_benchmark_reports"
    __table_args__ = (
        Index("ix_wf_benchmark_reports_owner_status", "owner", "status"),
        Index("ix_wf_benchmark_reports_project_status", "project_id", "status"),
    )

    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    visibility: Mapped[str] = mapped_column(
        String(16), nullable=False, default="project", server_default="project"
    )
    report_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    owner: Mapped[str] = mapped_column(String(256), default="")
    status: Mapped[str] = mapped_column(String(16), default="draft")
    worksheet: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )


class CaliberPromptTestRun(Base):
    """A single ad-hoc prompt-test run from the Prompts tab.

    One row per completed run of the browser-side test runner: the user
    generates/curates test cases, runs them through an assistant + LLM judge,
    and the assembled per-case verdicts are persisted here so the run survives
    a page refresh. ``results`` holds the full per-case array (the heavy
    payload); the scalar count/score columns are server-recomputed summaries
    for cheap history listing without deserializing ``results``.

    Replay is a frontend re-run of the stored ``results`` cases followed by a
    fresh insert — there is no server-side replay action.
    """

    __tablename__ = "caliber_prompt_test_runs"
    __table_args__ = (
        Index("ix_prompt_test_runs_agent_created", "agent_id", "created_at"),
        Index("ix_prompt_test_runs_created", "created_at"),
    )

    test_run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(64), index=True)
    prompt_name: Mapped[str] = mapped_column(String(256), default="")
    prompt_alias: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # Populated when the test cases came from a saved eval dataset.
    eval_dataset_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    test_set_size: Mapped[int] = mapped_column(Integer, default=0)
    passed_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    partial_count: Mapped[int] = mapped_column(Integer, default=0)
    overall_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Full per-case array: testCaseId/input/expectedBehavior/actualResponse/
    # verdict/score/reasoning. Heavy — omitted from history summaries.
    results: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mlflow_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by: Mapped[str] = mapped_column(String(256), default="")
    status: Mapped[str] = mapped_column(String(24), default="completed")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CaliberToolTestRun(Base):
    """A single durable tool-test run from the Tools tab.

    Mirrors :class:`CaliberPromptTestRun` but keyed on the tool registry. One
    row per completed run; ``kind`` records which surface produced it so the
    Sandbox single-invoke (``sandbox``), the saved-fixtures suite (``suite``),
    and the LLM-judged hardening pass (``hardening``) all persist as one run
    type. ``results`` holds the full per-case array (the heavy payload); the
    scalar count/score columns are server-recomputed summaries for cheap history
    listing without deserializing ``results``.
    """

    __tablename__ = "caliber_tool_test_runs"
    __table_args__ = (
        Index("ix_tool_test_runs_tool_created", "tool_id", "created_at"),
        Index("ix_tool_test_runs_created", "created_at"),
    )

    test_run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tool_id: Mapped[str] = mapped_column(String(64), index=True)
    # Snapshot of the tool's registry version at run time (e.g. "1.0").
    tool_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Which surface produced the run: "sandbox" | "suite" | "hardening".
    kind: Mapped[str] = mapped_column(String(16), default="suite")
    test_set_size: Mapped[int] = mapped_column(Integer, default=0)
    passed_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    partial_count: Mapped[int] = mapped_column(Integer, default=0)
    overall_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Full per-case array: name/input/verdict/score/output/error/reasoning.
    # Heavy — omitted from history summaries.
    results: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mlflow_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by: Mapped[str] = mapped_column(String(256), default="")
    status: Mapped[str] = mapped_column(String(24), default="completed")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CaliberSkillTestRun(Base):
    """A single durable skill-test run from the Skills tab.

    Mirrors :class:`CaliberToolTestRun` but keyed on the skill registry. One row
    per completed run; ``kind`` records which surface produced it so the
    selection trigger test (``selection``), the variable-render preview
    (``render``), and the multi-case scenario suite (``scenario``) all persist as
    one run type. ``results`` holds the full per-case array (the heavy payload);
    the scalar count/score columns are server-recomputed summaries for cheap
    history listing without deserializing ``results``.

    ``host_agent_id`` records the runtime identity the run executed under — the
    hidden skill target (``skill::{name}``) by default, or a chosen real host
    agent for context. ``baseline_run_id`` is pinned on the hidden skill
    target's ``optimizer_config`` (no column here), mirroring the prompt target.
    """

    __tablename__ = "caliber_skill_test_runs"
    __table_args__ = (
        Index("ix_skill_test_runs_skill_created", "skill_id", "created_at"),
        Index("ix_skill_test_runs_created", "created_at"),
    )

    test_run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    skill_id: Mapped[str] = mapped_column(String(64), index=True)
    # Snapshot of the skill's version at run time.
    skill_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Which surface produced the run: "selection" | "render" | "scenario".
    kind: Mapped[str] = mapped_column(String(16), default="scenario")
    test_set_size: Mapped[int] = mapped_column(Integer, default=0)
    passed_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    partial_count: Mapped[int] = mapped_column(Integer, default=0)
    overall_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Full per-case array: name/input/verdict/score/output/reasoning.
    # Heavy — omitted from history summaries.
    results: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # The hidden skill target or a chosen host agent the run executed under.
    host_agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mlflow_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by: Mapped[str] = mapped_column(String(256), default="")
    status: Mapped[str] = mapped_column(String(24), default="completed")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CaliberKnowledgeBaseTestRun(Base):
    """A single durable knowledge-base *calibration* run.

    Scores one knowledge-base version against a test-question set on retrieval +
    answer quality — Recall@k, nDCG@k, Faithfulness, Answer-correctness — so a KB
    version's quality survives a page refresh and can be compared baseline-vs-
    candidate. The retrieval-metric math (Recall@k, nDCG@k) is deterministic; the
    two answer metrics are LLM-judged.

    Mirrors :class:`CaliberPromptTestRun` / :class:`CaliberToolTestRun` /
    :class:`CaliberSkillTestRun` but keyed on the knowledge base. ``metrics`` is
    the aggregate JSON (``{recall_at_k, ndcg_at_k, faithfulness,
    answer_correctness, ...}`` — each averaged over the questions where it is
    defined). ``results`` holds the full per-question array (the heavy payload),
    omitted from history summaries. ``eval_dataset_version`` pins the dataset
    version the run scored against (like the prompt-run pin) for reproducibility.
    """

    __tablename__ = "caliber_knowledge_base_test_runs"
    __table_args__ = (
        Index("ix_kb_test_runs_kb_created", "knowledge_base_id", "created_at"),
        Index("ix_kb_test_runs_created", "created_at"),
    )

    test_run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    knowledge_base_id: Mapped[str] = mapped_column(String(64), index=True)
    knowledge_base_version_id: Mapped[str] = mapped_column(String(64))
    # Populated when the questions came from a saved eval dataset; the version
    # pins the exact example set scored (reconstructable "as of version N").
    eval_dataset_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    eval_dataset_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retrieval_mode: Mapped[str] = mapped_column(String(16), default="dense")
    top_k: Mapped[int] = mapped_column(Integer, default=6)
    test_set_size: Mapped[int] = mapped_column(Integer, default=0)
    # Aggregate metric bag: recall_at_k/ndcg_at_k/faithfulness/answer_correctness
    # plus pass/partial/fail counts. None entries mean "undefined for this set".
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Full per-question array: question/verdict/per-metric scores/retrieved
    # sources/answer. Heavy — omitted from history summaries.
    results: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_by: Mapped[str] = mapped_column(String(256), default="")
    status: Mapped[str] = mapped_column(String(24), default="completed")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CaliberAssistantSession(Base):
    """Persistent authoring conversation for Caliber Assistant."""

    __tablename__ = "caliber_assistant_sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(256), default="")
    owner: Mapped[str] = mapped_column(String(256), default="")
    status: Mapped[str] = mapped_column(String(32), default="active")
    goal: Mapped[str] = mapped_column(Text, default="")
    active_draft_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )


class CaliberAssistantMessage(Base):
    """Single message inside an assistant authoring session."""

    __tablename__ = "caliber_assistant_messages"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence_number", name="uq_asst_msg_seq"),
        Index("ix_asst_msg_session", "session_id"),
    )

    message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("caliber_assistant_sessions.session_id")
    )
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text, default="")
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSON, nullable=True)
    sequence_number: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CaliberAssistantDraft(Base):
    """Draft artifact being authored by the assistant."""

    __tablename__ = "caliber_assistant_drafts"
    __table_args__ = (Index("ix_asst_draft_session", "session_id"),)

    draft_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("caliber_assistant_sessions.session_id")
    )
    artifact_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="draft")
    title: Mapped[str] = mapped_column(String(256), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    spec: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    artifact: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    validation_report: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    test_report: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    target_registry_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[str] = mapped_column(String(256), default="")
    updated_by: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )


class CaliberAssistantRun(Base):
    """Assistant operation/run state."""

    __tablename__ = "caliber_assistant_runs"
    __table_args__ = (Index("ix_asst_run_session", "session_id"),)

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("caliber_assistant_sessions.session_id")
    )
    draft_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="running")
    engine: Mapped[str] = mapped_column(String(32), default="fake")
    model: Mapped[str] = mapped_column(String(128), default="")
    input_summary: Mapped[str] = mapped_column(Text, default="")
    output_summary: Mapped[str] = mapped_column(Text, default="")
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    mlflow_run_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CaliberAssistantPublishEvent(Base):
    """Audit row for publishing an assistant draft to a registry artifact."""

    __tablename__ = "caliber_assistant_publish_events"
    __table_args__ = (Index("ix_asst_pub_draft", "draft_id"),)

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    draft_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("caliber_assistant_drafts.draft_id")
    )
    artifact_type: Mapped[str] = mapped_column(String(32))
    target_registry_id: Mapped[str] = mapped_column(String(64))
    target_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved_by: Mapped[str] = mapped_column(String(256))
    published_by: Mapped[str] = mapped_column(String(256))
    publish_report: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CaliberAssistantAttachment(Base):
    """Context the user attaches to an assistant session ("+ add files").

    ``content_text`` is a capped plain-text snapshot resolved at attach time so the
    engine sees uniform context regardless of ``kind``; ``metadata_`` holds the
    kind-specific pointer (bucket/key, resource version, size, content-type).
    """

    __tablename__ = "caliber_assistant_attachments"
    __table_args__ = (Index("ix_asst_attachment_session", "session_id"),)

    attachment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("caliber_assistant_sessions.session_id")
    )
    kind: Mapped[str] = mapped_column(String(32))
    ref_type: Mapped[str] = mapped_column(String(32), default="")
    ref_id: Mapped[str] = mapped_column(String(512), default="")
    name: Mapped[str] = mapped_column(String(512), default="")
    content_text: Mapped[str] = mapped_column(Text, default="")
    bytes_size: Mapped[int] = mapped_column(Integer, default=0)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CaliberAssistantQueuedMessage(Base):
    """A not-yet-sent user turn for an assistant session ("add to queue" / steer).

    ``kind`` is ``queued`` for a plain follow-up or ``steer`` for a priority
    course-correction that jumps the queue; ``position`` orders pending rows and
    ``status`` moves from ``pending`` to dispatched/cancelled as the queue drains.
    """

    __tablename__ = "caliber_assistant_queued_messages"
    __table_args__ = (Index("ix_asst_queue_session", "session_id", "status", "position"),)

    queue_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("caliber_assistant_sessions.session_id")
    )
    content: Mapped[str] = mapped_column(Text, default="")
    mode: Mapped[str] = mapped_column(String(16), default="build")
    kind: Mapped[str] = mapped_column(String(16), default="queued")
    position: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    created_by: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CaliberMcpServer(Base):
    """Registered MCP server configuration."""

    __tablename__ = "caliber_mcp_servers"
    __table_args__ = (
        UniqueConstraint("name", name="uq_mcp_server_name"),
        Index("ix_mcp_servers_status", "status"),
    )

    server_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    transport: Mapped[str] = mapped_column(String(32), default="stdio")
    uri: Mapped[str] = mapped_column(String(1024), default="")
    command: Mapped[str] = mapped_column(String(1024), default="")
    args: Mapped[list[str]] = mapped_column(JSON, default=list)
    env: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    headers: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    auth_type: Mapped[str] = mapped_column(String(32), default="none")
    auth_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    discovered_tools: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    tool_policies: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Saved per-tool calibration test cases (keyed by tool name → list of cases)
    # and the latest scored calibration result per tool. See routes/mcp_servers.py.
    tool_test_cases: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}")
    tool_calibrations: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, server_default="{}"
    )
    icon: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="active")
    last_connected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    connection_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )


class CaliberWorkflowService(Base):
    """A workflow published as an invocable HTTP service.

    One row per workflow (the ``workflow_id`` unique constraint enforces it).
    Publishing requires a live deployment for ``alias``; the derived
    ``input_schema``/``output_schema`` (JSON Schema) document the typed
    invocation contract callers validate against. Service tokens
    (:class:`CaliberServiceToken`) authorize external invocations.
    """

    __tablename__ = "caliber_workflow_services"
    __table_args__ = (UniqueConstraint("workflow_id", name="uq_workflow_service_workflow_id"),)

    service_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("caliber_workflows.workflow_id")
    )
    alias: Mapped[str] = mapped_column(String(64), default="prod")
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output_schema: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # v1 ships services OPEN (no token needed to invoke). Flip this per-service to
    # require a Bearer service token; the token machinery is built but dormant
    # until then.
    auth_required: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )


class CaliberServiceToken(Base):
    """A Bearer service token authorizing external invocations of a workflow service.

    Only the SHA-256 hex digest (``token_hash``) is persisted — the plaintext is
    shown once at creation and never recoverable. ``prefix`` is a display-only
    fragment of the plaintext for UI identification. ``scopes`` gates what the
    token may do (e.g. ``["invoke"]``).
    """

    __tablename__ = "caliber_service_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_service_token_hash"),
        Index("ix_service_tokens_workflow", "workflow_id"),
    )

    token_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("caliber_workflows.workflow_id")
    )
    name: Mapped[str] = mapped_column(String(128), default="")
    token_hash: Mapped[str] = mapped_column(String(64))
    prefix: Mapped[str] = mapped_column(String(32), default="")
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_by: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CaliberGateVerdict(Base):
    """The latest advisory eval-gate verdict for one artifact version.

    The gate is *advisory* in v1 (no rotation-boundary enforcement), so this
    row is the version-addressable record the Version panel reads to show
    PASS/FAIL/none before a promotion, and that the audited promote stamps from
    the operator-supplied verdict. ``state`` is authoritative (computed from BOTH
    the aggregate floor and the per-dimension regression rule); the numeric
    columns are display detail. One row per ``(artifact_type, version_key)`` —
    upserted, so it always reflects the most recent evaluation of that version.
    """

    __tablename__ = "caliber_gate_verdicts"
    __table_args__ = (
        UniqueConstraint("artifact_type", "version_key", name="uq_gate_verdict_artifact_version"),
    )

    gate_verdict_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    artifact_type: Mapped[str] = mapped_column(String(32))
    version_key: Mapped[str] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(16))
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    baseline_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    min_aggregate_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    worst_regression: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_regression_delta: Mapped[float | None] = mapped_column(Float, nullable=True)
    eval_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
