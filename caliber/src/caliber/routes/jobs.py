"""``/caliber/jobs`` endpoints — list and detail.

Read-only for now: jobs are created by the verify endpoint
(`POST /caliber/verification-queue/{id}/verify`). The 6-stage pipeline UI in
[job-detail.html](../ui-mockups/job-detail.html) reads these endpoints.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Final

from sqlalchemy import Select, select, update
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.apply import apply_candidate
from caliber.audit import record as audit_record
from caliber.auth import SCOPE_OPERATOR, require_scopes, require_user
from caliber.db.models import CaliberApprovalRequest, CaliberRefinementJob
from caliber.ids import new_approval_id
from caliber.observability import metrics
from caliber.promoter import Promoter
from caliber.routes._deps import (
    envelope_response,
    envelope_response_dict,
    get_session_factory,
    list_limit,
    parse_json_object,
)
from caliber.schemas import (
    ApprovalRequestChangesRequest,
    JobTargetSchema,
    JobTargetsResponse,
    RefinementJobSchema,
)

logger = logging.getLogger("caliber.routes.jobs")

LIST_PATH = "/ajax-api/2.0/mlflow/caliber/jobs"
DETAIL_PATH = "/ajax-api/2.0/mlflow/caliber/jobs/{job_id}"
TARGETS_PATH = "/ajax-api/2.0/mlflow/caliber/jobs/{job_id}/targets"
APPLY_PATH = "/ajax-api/2.0/mlflow/caliber/jobs/{job_id}/apply"
REQUEST_CHANGES_PATH = "/ajax-api/2.0/mlflow/caliber/jobs/{job_id}/request-changes"

# Vocabulary mirrors caliber_refinement_jobs.status — kept here as an allowlist
# rather than imported from anywhere so any future status addition is forced
# to update this file too (good — list filters should be conscious choices).
# ``candidate_ready`` is the terminal state a job lands at once its candidate
# clears the eval gate; ``applied`` is the terminal state after an operator
# promotes that candidate via the Apply endpoint.
_VALID_STATUSES: Final[frozenset[str]] = frozenset(
    {
        "queued",
        "running",
        "awaiting_approval",
        "candidate_ready",
        "applied",
        "completed",
        "rejected",
        "failed",
        "cancelled",
    }
)
_VALID_STAGES: Final[frozenset[str]] = frozenset(
    {"triage", "evidence", "diagnosis", "candidate", "eval", "approval", "done"}
)


def _validate_filter(name: str, value: str, allowlist: frozenset[str]) -> None:
    if value not in allowlist:
        raise HTTPException(
            status_code=400,
            detail=f"invalid value for {name!r}: {value!r}; expected one of {sorted(allowlist)}",
        )


def _apply_filters(
    stmt: Select[tuple[CaliberRefinementJob]], request: Request
) -> Select[tuple[CaliberRefinementJob]]:
    status = request.query_params.get("status")
    if status is not None:
        _validate_filter("status", status, _VALID_STATUSES)
        stmt = stmt.where(CaliberRefinementJob.status == status)

    stage = request.query_params.get("stage")
    if stage is not None:
        _validate_filter("stage", stage, _VALID_STAGES)
        stmt = stmt.where(CaliberRefinementJob.current_stage == stage)

    agent_id = request.query_params.get("agent_id")
    if agent_id is not None:
        stmt = stmt.where(CaliberRefinementJob.agent_id == agent_id)

    workflow_id = request.query_params.get("workflow_id")
    if workflow_id is not None:
        stmt = stmt.where(CaliberRefinementJob.workflow_id == workflow_id)

    return stmt


_GEPA_PROGRESS_METRIC = "eval_score"


def _get_mlflow_module() -> Any | None:
    """Lazy-import mlflow and return it, or ``None`` if unavailable."""
    try:
        import mlflow  # noqa: PLC0415

        return mlflow
    except ImportError:
        return None


def _fetch_gepa_metric_history(mlflow_mod: Any, job: CaliberRefinementJob) -> list[Any]:
    """Read the ``eval_score`` metric history off ``job``'s MLflow run.

    Isolated from :func:`_gepa_progress` purely to keep that function's
    branch count in check; returns ``[]`` (not ``None``) on any failure so
    the caller's "nothing logged yet" and "lookup failed" paths collapse.
    """
    try:
        client = mlflow_mod.MlflowClient()
        return list(client.get_metric_history(job.mlflow_run_id, _GEPA_PROGRESS_METRIC))
    except Exception:
        logger.debug("failed to read GEPA progress for job=%s", job.job_id, exc_info=True)
        return []


def _gepa_progress(job: CaliberRefinementJob) -> dict[str, object] | None:
    """Best-effort live progress for an in-flight GEPA candidate-generation stage.

    GEPA can spend minutes inside a single ``candidate`` stage call, during
    which the job row itself never changes (``status='running'``,
    ``current_stage='candidate'``) — from the UI's point of view that reads
    as a static "running" with no signal of movement. But
    ``_generate_candidate_gepa`` calls ``mlflow.genai.optimize_prompts(...,
    enable_tracking=True)`` *while the worker's own MLflow run for this job is
    still active*, so MLflow's autologging reuses that run instead of
    starting a nested one — every full-validation pass GEPA completes logs an
    ``eval_score`` metric point (plus per-scorer breakdowns) directly onto
    ``job.mlflow_run_id``, live, as it happens.

    Reading that metric history back — rather than plumbing a new progress
    callback through the optimizer boundary — gives the UI an actual
    "iteration N, latest score S" readout with no new DB columns, no
    cross-thread session sharing, and no coupling between the LLM provider
    Protocol and persistence.

    Returns ``None`` when the job isn't a running GEPA candidate stage, MLflow
    isn't available, or nothing has been logged yet (e.g. GEPA is still on
    its first pass). Never raises — this is purely a UI nicety.
    """
    is_running_gepa_candidate = (
        job.optimizer_type == "GEPA"
        and job.status == "running"
        and job.current_stage == "candidate"
        and bool(job.mlflow_run_id)
    )
    if not is_running_gepa_candidate:
        return None
    mlflow_mod = _get_mlflow_module()
    if mlflow_mod is None:
        return None
    history = _fetch_gepa_metric_history(mlflow_mod, job)
    if not history:
        return None
    ordered = sorted(history, key=lambda metric: metric.step)
    latest = ordered[-1]
    return {
        "iterations": len(ordered),
        "latest_score": latest.value,
        "latest_step": latest.step,
        "updated_at": datetime.fromtimestamp(latest.timestamp / 1000, tz=timezone.utc).isoformat(),
        "history": [{"step": metric.step, "score": metric.value} for metric in ordered],
    }


def _serialize_job(job: CaliberRefinementJob) -> dict[str, object]:
    """``RefinementJobSchema`` plus the computed, non-persisted GEPA progress."""
    payload = RefinementJobSchema.model_validate(job).model_dump(mode="json")
    payload["gepa_progress"] = _gepa_progress(job)
    return payload


async def list_jobs(request: Request) -> JSONResponse:
    """Return refinement jobs, optionally filtered by ``status``, ``stage``, ``agent_id``,
    ``workflow_id``.

    Default ordering is newest-first. Operators usually want to see what's
    happening right now; longer-tail browsing happens via the per-agent
    history view.
    """
    require_user(request)
    factory = get_session_factory(request)
    stmt = select(CaliberRefinementJob).order_by(CaliberRefinementJob.created_at.desc())
    stmt = _apply_filters(stmt, request)
    limit, offset = list_limit(request)
    with factory() as session:
        rows = session.execute(stmt.limit(limit).offset(offset)).scalars().all()
        items = [_serialize_job(row) for row in rows]
    return envelope_response_dict(items)


async def get_job(request: Request) -> JSONResponse:
    """Return a single refinement job by ``job_id``."""
    require_user(request)
    job_id = request.path_params["job_id"]
    factory = get_session_factory(request)
    with factory() as session:
        row = session.get(CaliberRefinementJob, job_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"refinement job {job_id!r} not found")
        payload = _serialize_job(row)
    return envelope_response_dict(payload)


async def get_job_targets(request: Request) -> JSONResponse:
    """Return the impacted agents/artifacts for a bundle job.

    Single-agent jobs (the common case) return one target — the job's
    own agent + artifact_type. That keeps the response shape uniform
    so the UI's bundle-review component doesn't need a special case for
    "this isn't really a bundle."

    Multi-agent jobs return one row per entry in ``bundle_targets``.
    The shape of each entry is whatever the MultiAgentCoord optimizer
    wrote — at minimum ``agent_id`` + ``artifact_type``, optionally a
    ``role`` and an ``artifact_ref`` for the impacted artifact's current
    location. Extra keys pass through via ``ConfigDict(extra='allow')``.
    """
    require_user(request)
    job_id = request.path_params["job_id"]
    factory = get_session_factory(request)
    with factory() as session:
        job = session.get(CaliberRefinementJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"refinement job {job_id!r} not found")

    targets = _resolve_targets(job)
    response = JobTargetsResponse(
        job_id=job.job_id,
        agent_id=job.agent_id,
        artifact_type=job.artifact_type,
        bundle_size=len(targets),
        targets=targets,
    )
    return envelope_response(response)


def _resolve_targets(job: CaliberRefinementJob) -> list[JobTargetSchema]:
    """Normalize the heterogeneous ``bundle_targets`` JSON into typed rows.

    Bundle entries that miss required fields are tolerated — we fall
    back to the job's own ``agent_id`` / ``artifact_type``. Unknown
    extra keys flow through as-is so a richer payload (cost-budget,
    blast-radius score, etc.) survives the API hop.
    """
    raw = job.bundle_targets or []
    if not raw:
        return [JobTargetSchema(agent_id=job.agent_id, artifact_type=job.artifact_type)]

    targets: list[JobTargetSchema] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        targets.append(
            JobTargetSchema(
                # Same safety-coercion pattern as ``caliber.bundle._coerce_str``:
                # ``null`` and non-string values fall back to the job's
                # primary identifier rather than collapsing to literal
                # ``"None"`` (deep-review Finding 6).
                agent_id=_coerce_str(entry.get("agent_id"), job.agent_id),
                artifact_type=_coerce_str(entry.get("artifact_type"), job.artifact_type),
                artifact_ref=_optional_str(entry.get("artifact_ref")),
                role=_optional_str(entry.get("role")),
                **{
                    k: v
                    for k, v in entry.items()
                    if k not in {"agent_id", "artifact_type", "artifact_ref", "role"}
                },
            )
        )
    if not targets:
        return [JobTargetSchema(agent_id=job.agent_id, artifact_type=job.artifact_type)]
    return targets


def _coerce_str(value: object, fallback: str) -> str:
    if isinstance(value, str) and value:
        return value
    return fallback


def _optional_str(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


async def apply_job(request: Request) -> JSONResponse:
    """Promote a ``candidate_ready`` job's candidate (operator action).

    This is the lightweight replacement for the removed approval-governance
    flow: there are no votes, quorum, or comments. The operator simply applies
    the candidate that already cleared the eval gate.

    A born-``approved`` :class:`CaliberApprovalRequest` is minted as the
    provenance/rollback anchor (the rollback-checkpoint FK is NOT NULL), then
    :func:`caliber.apply.apply_candidate` performs the promotion and marks the
    job terminal at ``applied``.
    """
    actor = require_scopes(request, [SCOPE_OPERATOR])
    job_id = request.path_params["job_id"]
    promoter: Promoter = request.app.state.promoter
    factory = get_session_factory(request)
    with factory() as session:
        # Claim with a conditional write rather than relying only on
        # SELECT FOR UPDATE (which SQLite ignores). The transient status lives
        # in this transaction: failure rolls it back to candidate_ready, while
        # a racing request blocks and then observes rowcount=0 before any
        # promoter/provider effect runs.
        claim = session.execute(
            update(CaliberRefinementJob)
            .where(CaliberRefinementJob.job_id == job_id)
            .where(CaliberRefinementJob.status == "candidate_ready")
            .values(status="applying")
        )
        if int(getattr(claim, "rowcount", 0) or 0) != 1:
            current = session.get(CaliberRefinementJob, job_id)
            if current is None:
                raise HTTPException(status_code=404, detail=f"refinement job {job_id!r} not found")
            raise HTTPException(
                status_code=409,
                detail=(
                    f"job {job_id!r} cannot be applied: "
                    f"current status is {current.status!r} (expected 'candidate_ready')"
                ),
            )
        job = session.get(CaliberRefinementJob, job_id)
        assert job is not None
        if not job.candidate:
            raise HTTPException(
                status_code=409,
                detail=f"job {job_id!r} has no candidate to promote",
            )

        now = datetime.now(timezone.utc)
        approval = CaliberApprovalRequest(
            approval_id=new_approval_id(),
            job_id=job.job_id,
            agent_id=job.agent_id,
            status="approved",
            attempt_number=job.attempt_count or 1,
            eval_results=job.eval_results,
            candidate_snapshot=dict(job.candidate),
            diagnosis_snapshot=dict(job.diagnosis) if job.diagnosis else None,
            approved_by=actor,
            approved_at=now,
        )
        session.add(approval)
        session.flush()

        outcome = apply_candidate(
            session,
            job=job,
            approval=approval,
            promoter=promoter,
            actor=actor,
            terminal_status="applied",
        )

        audit_record(
            session,
            actor=actor,
            action="apply_candidate",
            entity_type="refinement_job",
            entity_id=job.job_id,
            details={
                "approval_id": approval.approval_id,
                "artifact_ref": outcome.promotion.get("artifact_ref"),
                "checkpoint_ids": outcome.checkpoint_ids,
            },
        )
        session.commit()
        agent_id = job.agent_id
        artifact_type = job.artifact_type
        artifact_ref = outcome.promotion.get("artifact_ref")
        payload: dict[str, object] = {
            "job_id": job.job_id,
            "status": job.status,
            "promotion": outcome.promotion,
        }

    metrics.record_promotion(agent_id=agent_id, artifact_type=artifact_type)
    metrics.record_job_terminal(agent_id=agent_id, artifact_type=artifact_type, status="applied")

    # Announce the promotion so the queue/job UI can update live (no-op when
    # no event bus is configured, e.g. in some tests).
    bus = getattr(request.app.state, "event_bus", None)
    if bus is not None:
        bus.publish(
            {
                "type": "job.applied",
                "job_id": job_id,
                "agent_id": agent_id,
                "artifact_ref": artifact_ref,
            }
        )
    return envelope_response_dict(payload)


def _request_changes_sync(
    factory: Any, *, job_id: str, actor: str, payload: ApprovalRequestChangesRequest
) -> RefinementJobSchema:
    with factory() as session:
        # Same conditional-UPDATE claim idiom as apply_job: the transient
        # status lives in this transaction, so a racing request observes
        # rowcount=0 before any further effect runs.
        claim = session.execute(
            update(CaliberRefinementJob)
            .where(CaliberRefinementJob.job_id == job_id)
            .where(CaliberRefinementJob.status == "candidate_ready")
            .values(status="running")
        )
        if int(getattr(claim, "rowcount", 0) or 0) != 1:
            current = session.get(CaliberRefinementJob, job_id)
            if current is None:
                raise HTTPException(status_code=404, detail=f"refinement job {job_id!r} not found")
            raise HTTPException(
                status_code=409,
                detail=(
                    f"job {job_id!r} cannot have changes requested: "
                    f"current status is {current.status!r} (expected 'candidate_ready')"
                ),
            )
        job = session.get(CaliberRefinementJob, job_id)
        assert job is not None
        job.review_notes = payload.notes
        job.current_stage = "candidate"

        audit_record(
            session,
            actor=actor,
            action="request_changes",
            entity_type="refinement_job",
            entity_id=job.job_id,
            details={"notes": payload.notes},
        )
        session.commit()
        return RefinementJobSchema.model_validate(job)


async def request_changes(request: Request) -> JSONResponse:
    """Send a ``candidate_ready`` job's candidate back for another pass.

    A human collaboration action, distinct from the automatic self-correction
    loop: it records reviewer feedback in ``job.review_notes`` (which
    ``orchestrator/candidate.py`` already reads and clears on the next
    candidate-generation pass) and returns the job to ``running`` at the
    ``candidate`` stage. Deliberately does **not** touch ``refine_iteration``
    -- this is not a way to bypass ``refinement_max_iterations``.

    Offloads its synchronous SQLAlchemy work to
    :func:`starlette.concurrency.run_in_threadpool`, matching
    ``routes/verification.py`` and ``routes/rework_tasks.py`` — see
    ``tests/test_async_offload_ratchet.py``.
    """
    actor = require_scopes(request, [SCOPE_OPERATOR])
    job_id = request.path_params["job_id"]
    body = await parse_json_object(request)
    payload = ApprovalRequestChangesRequest.model_validate(body)

    factory = get_session_factory(request)
    schema = await run_in_threadpool(
        _request_changes_sync, factory, job_id=job_id, actor=actor, payload=payload
    )
    return envelope_response_dict({"job": schema.model_dump(mode="json")})


def register(app: Starlette) -> None:
    """Add the jobs routes to the given Starlette application."""
    app.routes.append(Route(LIST_PATH, list_jobs, methods=["GET"]))
    app.routes.append(Route(DETAIL_PATH, get_job, methods=["GET"]))
    app.routes.append(Route(TARGETS_PATH, get_job_targets, methods=["GET"]))
    app.routes.append(Route(APPLY_PATH, apply_job, methods=["POST"]))
    app.routes.append(Route(REQUEST_CHANGES_PATH, request_changes, methods=["POST"]))
