"""``/caliber/jobs`` endpoints — list and detail.

Read-only for now: jobs are created by the verify endpoint
(`POST /caliber/verification-queue/{id}/verify`). The 6-stage pipeline UI in
[job-detail.html](../ui-mockups/job-detail.html) reads these endpoints.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Final, NoReturn

from sqlalchemy import Select, delete, func, literal, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.apply import (
    APPLY_BUSY,
    APPLY_PERMISSION_REVOKED,
    APPLY_RECHECK_DETAIL,
    APPLY_SEPARATION_DETAIL,
    apply_candidate,
    apply_candidate_refusal,
    apply_separation_refusal,
)
from caliber.audit import record as audit_record
from caliber.auth import (
    SCOPE_OPERATOR,
    CaliberIdentity,
    require_scopes,
    require_user,
    resolve_identity,
)
from caliber.db.models import (
    CaliberApprovalRequest,
    CaliberRefinementJob,
    CaliberReleaseOperation,
)
from caliber.ids import new_approval_id
from caliber.observability import metrics
from caliber.profile import apply_distinct_approver_required
from caliber.promoter import Promoter
from caliber.routes._deps import (
    envelope_response,
    envelope_response_dict,
    get_session_factory,
    list_limit,
    parse_json_object,
)
from caliber.routes._errors import CaliberHTTPException
from caliber.routes._job_access import (
    ACTION_APPLY,
    ACTION_REVIEW,
    is_lock_contention,
    load_visible_job,
    lock_job_and_authority,
    scope_jobs_to_visible_agents,
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
        "applying",
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


def _serialize_job(
    job: CaliberRefinementJob, apply_summary: dict[str, object] | None = None
) -> dict[str, object]:
    """``RefinementJobSchema`` plus the computed, non-persisted GEPA progress and, for an
    applied job, the approval, applier and registered version."""
    payload = RefinementJobSchema.model_validate(job).model_dump(mode="json")
    payload["gepa_progress"] = _gepa_progress(job)
    if apply_summary:
        payload.update(apply_summary)
    return payload


def _apply_summaries(
    session: Session, jobs: Sequence[CaliberRefinementJob]
) -> dict[str, dict[str, object]]:
    """The approval, applier and registered version of each applied job, in two queries.

    ``POST /jobs/{id}/apply`` mints an already-approved ``CaliberApprovalRequest`` as the
    release anchor and records the release operation under its id. The newest approved
    approval for a job is the one that went live; its operation's ``version_after`` is the
    version it registered. A job that is not applied has neither.
    """
    job_ids = [job.job_id for job in jobs if job.status in {"applied", "applying"}]
    if not job_ids:
        return {}
    approvals = (
        session.execute(
            select(CaliberApprovalRequest)
            .where(CaliberApprovalRequest.job_id.in_(job_ids))
            .where(CaliberApprovalRequest.status == "approved")
            .order_by(
                # ``approved_at`` is set in Python with microseconds; ``created_at`` is a
                # database timestamp with one-second resolution on SQLite, so two approvals
                # minted in the same second tied and the random id broke the tie.
                CaliberApprovalRequest.approved_at.desc(),
                CaliberApprovalRequest.created_at.desc(),
                CaliberApprovalRequest.approval_id.desc(),
            )
        )
        .scalars()
        .all()
    )
    newest: dict[str, CaliberApprovalRequest] = {}
    for approval in approvals:
        newest.setdefault(approval.job_id, approval)
    operations = (
        session.execute(
            select(CaliberReleaseOperation)
            .where(
                CaliberReleaseOperation.approval_id.in_([a.approval_id for a in newest.values()])
            )
            .where(CaliberReleaseOperation.status == "applied")
            .where(CaliberReleaseOperation.operation_type == "promote")
            .order_by(
                CaliberReleaseOperation.applied_at.desc(),
                CaliberReleaseOperation.created_at.desc(),
                CaliberReleaseOperation.operation_id.desc(),
            )
        )
        .scalars()
        .all()
        if newest
        else []
    )
    version_by_approval: dict[str, int] = {}
    for operation in operations:
        if operation.approval_id is not None:
            version_by_approval.setdefault(operation.approval_id, operation.version_after)
    return {
        job_id: {
            "approval_id": approval.approval_id,
            "applied_by": approval.approved_by,
            "applied_version": version_by_approval.get(approval.approval_id),
        }
        for job_id, approval in newest.items()
    }


async def list_jobs(request: Request) -> JSONResponse:
    """Return refinement jobs, optionally filtered by ``status``, ``stage``, ``agent_id``,
    ``workflow_id``.

    Default ordering is newest-first. Operators usually want to see what's
    happening right now; longer-tail browsing happens via the per-agent
    history view.
    """
    require_user(request)
    identity = resolve_identity(request)
    factory = get_session_factory(request)
    stmt = select(CaliberRefinementJob).order_by(CaliberRefinementJob.created_at.desc())
    stmt = scope_jobs_to_visible_agents(stmt, identity)
    stmt = _apply_filters(stmt, request)
    limit, offset = list_limit(request)
    with factory() as session:
        rows = session.execute(stmt.limit(limit).offset(offset)).scalars().all()
        summaries = _apply_summaries(session, rows)
        items = [_serialize_job(row, summaries.get(row.job_id)) for row in rows]
    return envelope_response_dict(items)


async def get_job(request: Request) -> JSONResponse:
    """Return a single refinement job by ``job_id``.

    404 when the job's agent is not visible to the caller (a non-member of the
    owning project); see :mod:`caliber.routes._job_access`.
    """
    require_user(request)
    identity = resolve_identity(request)
    job_id = request.path_params["job_id"]
    factory = get_session_factory(request)
    with factory() as session:
        row = load_visible_job(session, identity, job_id)
        payload = _serialize_job(row, _apply_summaries(session, [row]).get(row.job_id))
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
    identity = resolve_identity(request)
    job_id = request.path_params["job_id"]
    factory = get_session_factory(request)
    with factory() as session:
        job = load_visible_job(session, identity, job_id)

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


_APPLY_ERROR_AUDIT_CHARS = 500


def _revert_unstarted_apply(
    session: Any, *, job_id: str, approval_id: str, actor: str, error: Exception
) -> None:
    """Undo the committed Apply claim when the promotion failed before any release.

    Apply commits its claim (``candidate_ready`` to ``applying``) and the
    approval anchor before the promoter runs, so a failed attempt can no longer
    just roll back. Two cases:

    * A release operation already references this approval. The provider outcome
      may be indeterminate, so the job stays ``applying`` and the release
      reconciler settles it from the durable operation (to ``applied`` or back to
      ``candidate_ready``). Nothing is reverted here.
    * No release operation exists. Nothing external was committed to, so the job
      returns to ``candidate_ready`` (retryable), the unused approval is
      withdrawn, and an ``apply_failed`` audit row records the attempt.

    The revert is best-effort: if it cannot run (for example the database is
    unavailable) the original error still propagates and the job is left
    ``applying`` with no operation, which the janitor reverts after its stale
    window. The conditional UPDATE only touches a job still ``applying``.
    """
    try:
        has_operation = (
            session.execute(
                select(CaliberReleaseOperation.operation_id)
                .where(CaliberReleaseOperation.approval_id == approval_id)
                .limit(1)
            ).first()
            is not None
        )
        if has_operation:
            return
        reverted = session.execute(
            update(CaliberRefinementJob)
            .where(CaliberRefinementJob.job_id == job_id)
            .where(CaliberRefinementJob.status == "applying")
            .values(status="candidate_ready")
        )
        session.execute(
            delete(CaliberApprovalRequest).where(CaliberApprovalRequest.approval_id == approval_id)
        )
        if int(getattr(reverted, "rowcount", 0) or 0) == 1:
            audit_record(
                session,
                actor=actor,
                action="apply_failed",
                entity_type="refinement_job",
                entity_id=job_id,
                details={
                    "approval_id": approval_id,
                    "outcome": "reverted_to_candidate_ready",
                    "error": str(error)[:_APPLY_ERROR_AUDIT_CHARS],
                },
            )
        session.commit()
    except Exception:
        session.rollback()
        logger.exception(
            "could not revert failed apply of job=%s; the janitor will release it", job_id
        )


#: The characters Python's ``str.strip`` removes that the database trims too: ASCII whitespace.
_SQL_TRIM_CHARS = " \t\n\r\x0b\x0c"


def _sql_identity_key(value: Any, dialect: str) -> Any:
    """``value`` trimmed and lower-cased in SQL, the database counterpart of ``apply_separation_refusal``'s key.

    PostgreSQL spells the two-argument trim ``btrim``; SQLite's ``trim`` takes the character set directly.
    """
    trim = func.btrim if dialect == "postgresql" else func.trim
    return func.lower(trim(value, _SQL_TRIM_CHARS))


def _with_distinct_requester(statement: Any, actor: str, *, dialect: str) -> Any:
    """``statement`` (the Apply claim UPDATE) restricted to a job whose requester is not ``actor`` (B06.3, #747).

    The same separation as :func:`~caliber.apply.apply_separation_refusal`, as a predicate of the claim itself, so a
    requester that changed after the route's up-front check still cannot claim: no row, no claim, no effect. Both sides
    are trimmed of ASCII whitespace and lower-cased; a NULL or blank requester matches nothing, so it stays refused. The
    SQL lower-casing is ASCII-only on SQLite; the Python check is the full case-fold.
    """
    requester_key = _sql_identity_key(CaliberRefinementJob.triggered_by, dialect)
    return (
        statement.where(requester_key.is_not(None))
        .where(requester_key != "")
        .where(requester_key != _sql_identity_key(literal(actor), dialect))
    )


@contextmanager
def _lock_contention_is_busy(session: Any) -> Iterator[None]:
    """Turn lock contention anywhere in the Apply claim into the retryable, payload-free ``503`` ``apply_busy`` (#1066).

    Wraps the whole claim transaction, from the row locks to the commit of the claim and its approval anchor: a PostgreSQL
    lock wait that outlasts the lock timeout (``55P03``), a reverse-order deadlock in which the server picked this
    transaction as the victim (``40P01``), and on SQLite the busy timeout (``database is locked``) that surfaces at the claim
    ``UPDATE`` or the commit, because SQLite has no row locks to wait on earlier. None of them has decided anything: the
    aborted transaction is rolled back (releasing what it held), the job keeps its status, no approval anchor, promotion or
    audit row exists (a refusal is evidence of a decision, and none was made), and the client may retry. Any other database
    error propagates unchanged. The promoter and its compensation run after the claim, outside this block.
    """
    try:
        yield
    except OperationalError as exc:
        if not is_lock_contention(exc):
            raise
        session.rollback()
        raise CaliberHTTPException(
            status_code=503,
            detail=APPLY_RECHECK_DETAIL[APPLY_BUSY],
            reason_code=APPLY_BUSY,
        ) from None


def _refuse_apply(
    session: Any, *, job_id: str, actor: str, status_code: int, reason_code: str, detail: str
) -> NoReturn:
    """Refuse an Apply with a typed, payload-free error and record the refusal (B06.4, #753; #1050).

    Everything the attempt held is rolled back first, so the refusal changes no job, approval or promoter state and
    releases the row locks; the audit row is then written in a transaction of its own. It carries the job id, the caller
    and the reason code only, never candidate text, a digest or the requester. A refusal that cannot be recorded is still
    a refusal (logged, not turned into a 500), because the audit row is evidence of the attempt, not part of the decision.
    """
    session.rollback()
    try:
        audit_record(
            session,
            actor=actor,
            action="apply_refused",
            entity_type="refinement_job",
            entity_id=job_id,
            details={"reason_code": reason_code},
            severity="high",
        )
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("could not audit refused apply of job=%s (%s)", job_id, reason_code)
    raise CaliberHTTPException(status_code=status_code, detail=detail, reason_code=reason_code)


def _refuse_unless_distinct(
    session: Any, *, job_id: str, requester: str | None, actor: str
) -> None:
    """Refuse (403, audited) when ``actor`` may not apply a job ``requester`` asked for (B06.3, #747)."""
    code = apply_separation_refusal(requester=requester, approver=actor)
    if code is not None:
        _refuse_apply(
            session,
            job_id=job_id,
            actor=actor,
            status_code=403,
            reason_code=code,
            detail=APPLY_SEPARATION_DETAIL[code],
        )


def _recheck_claim(
    session: Any, *, identity: CaliberIdentity, actor: str, job_id: str, require_distinct: bool
) -> CaliberRefinementJob:
    """Lock the job and what authorises the caller, then recheck every Apply precondition on what is locked (B06.4, #753).

    Run inside the claim transaction, so the rows read here cannot change before the claim commits (see
    :func:`~caliber.routes._job_access.lock_job_and_authority`). Raises, in this order and before anything is written:

    0. ``503`` :data:`~caliber.apply.APPLY_BUSY` when a lock is not granted in time or the transaction is chosen as a
       deadlock victim (see :func:`_lock_contention_is_busy`, which :func:`apply_job` wraps around this and the claim);
    1. ``404`` when the job no longer exists;
    2. ``403`` :data:`~caliber.apply.APPLY_PERMISSION_REVOKED` when the caller no longer holds ``release.apply`` on the
       job's project or can no longer see the job (the request-start check is not trusted);
    3. the separation refusals (``403``) of :func:`caliber.apply.apply_separation_refusal`, when the rule is in force;
    4. ``409`` when the job is no longer ``candidate_ready``;
    5. ``409`` :data:`~caliber.apply.APPLY_CANDIDATE_UNVERIFIED` / :data:`~caliber.apply.APPLY_CANDIDATE_CHANGED` when the
       candidate to be promoted is not the one whose digest the eval gate recorded.

    Returns the locked job, which the caller then claims with a conditional UPDATE.
    """
    locked = lock_job_and_authority(session, job_id, user_id=identity.user_id)
    if locked is None:
        raise HTTPException(status_code=404, detail=f"refinement job {job_id!r} not found")
    try:
        load_visible_job(session, identity, job_id, action=ACTION_APPLY)
    except HTTPException:
        _refuse_apply(
            session,
            job_id=job_id,
            actor=actor,
            status_code=403,
            reason_code=APPLY_PERMISSION_REVOKED,
            detail=APPLY_RECHECK_DETAIL[APPLY_PERMISSION_REVOKED],
        )
    if require_distinct:
        _refuse_unless_distinct(session, job_id=job_id, requester=locked.triggered_by, actor=actor)
    if locked.status != "candidate_ready":
        raise HTTPException(
            status_code=409,
            detail=(
                f"job {job_id!r} cannot be applied: "
                f"current status is {locked.status!r} (expected 'candidate_ready')"
            ),
        )
    digest_code = apply_candidate_refusal(
        recorded_digest=locked.candidate_digest, candidate=locked.candidate
    )
    if digest_code is not None:
        _refuse_apply(
            session,
            job_id=job_id,
            actor=actor,
            status_code=409,
            reason_code=digest_code,
            detail=APPLY_RECHECK_DETAIL[digest_code],
        )
    return locked


def _verified_claim_snapshot(
    session: Any,
    *,
    identity: CaliberIdentity,
    actor: str,
    job_id: str,
    evaluated_digest: str | None,
) -> dict[str, Any]:
    """After the claim UPDATE, recheck authority and return the candidate to snapshot, verified against its digest (B06.4).

    Nothing is committed yet, so a refusal here rolls the claim back with it.

    * Authority once more, now that the UPDATE holds the write lock. PostgreSQL already holds the membership rows
      locked from the recheck; SQLite has no row locks, so this is what makes the permission check atomic with the claim
      there: a grant revoked between the recheck and the UPDATE is seen here. ``expire_all`` so the check reads the
      rows, not what was loaded before.
    * What the approval anchor snapshots, and the promoter then reads, against the digest the gate recorded: the
      conditional UPDATE cannot compare JSON content on every dialect, so on one without row locks this is what stops a
      candidate edited after the recheck.
    """
    session.expire_all()
    try:
        load_visible_job(session, identity, job_id, action=ACTION_APPLY)
    except HTTPException:
        _refuse_apply(
            session,
            job_id=job_id,
            actor=actor,
            status_code=403,
            reason_code=APPLY_PERMISSION_REVOKED,
            detail=APPLY_RECHECK_DETAIL[APPLY_PERMISSION_REVOKED],
        )
    job = session.get(CaliberRefinementJob, job_id, populate_existing=True)
    assert job is not None
    snapshot = dict(job.candidate or {})
    digest_code = apply_candidate_refusal(recorded_digest=evaluated_digest, candidate=snapshot)
    if digest_code is not None:
        _refuse_apply(
            session,
            job_id=job_id,
            actor=actor,
            status_code=409,
            reason_code=digest_code,
            detail=APPLY_RECHECK_DETAIL[digest_code],
        )
    return snapshot


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
    identity = resolve_identity(request)
    job_id = request.path_params["job_id"]
    promoter: Promoter = request.app.state.promoter
    factory = get_session_factory(request)
    with factory() as session:
        # Project authorization comes first: a non-member gets 404 and a
        # member without the ``release.apply`` role gets 403 before any claim,
        # approval row, promoter call or audit write can happen.
        visible_job = load_visible_job(session, identity, job_id, action=ACTION_APPLY)
        # Two-person rule (B06.3, #747): when it is in force (the flag, or always in the reference
        # profile) the authenticated caller must not be the account that requested the job, and a job
        # with no recorded requester is refused. A missing config is treated as the reference profile
        # (fail closed), not a 500. This is the cheap early refusal; the claim below repeats it, with
        # every other precondition, on the row-locked job.
        require_distinct = apply_distinct_approver_required(
            getattr(request.app.state, "config", None)
        )
        if require_distinct:
            _refuse_unless_distinct(
                session, job_id=job_id, requester=visible_job.triggered_by, actor=actor
            )
        # The claim (B06.4, #753). Everything the decision rests on is rechecked here, inside the
        # transaction that commits the claim, on rows locked for it: the caller's current project
        # permission, the distinct requester, the job status and the exact candidate digest. Nothing
        # that was true when the request began is trusted, and a refusal rolls back before any write.
        # The write itself is still a conditional UPDATE, because SELECT ... FOR UPDATE is a no-op on
        # SQLite and the predicate is what makes the claim atomic there. The claim and the approval
        # anchor are committed *before* the promoter runs (see below), so a racing request observes
        # rowcount=0 (or waits for this commit and sees ``applying``) before any provider effect, and
        # the write lock is never held across the provider call.
        with _lock_contention_is_busy(session):
            locked = _recheck_claim(
                session,
                identity=identity,
                actor=actor,
                job_id=job_id,
                require_distinct=require_distinct,
            )
            evaluated_digest = locked.candidate_digest
            claim_statement = (
                update(CaliberRefinementJob)
                .where(CaliberRefinementJob.job_id == job_id)
                .where(CaliberRefinementJob.status == "candidate_ready")
                .where(CaliberRefinementJob.candidate_digest == evaluated_digest)
            )
            if require_distinct:
                claim_statement = _with_distinct_requester(
                    claim_statement, actor, dialect=session.get_bind().dialect.name
                )
            claim = session.execute(claim_statement.values(status="applying"))
            if int(getattr(claim, "rowcount", 0) or 0) != 1:
                # The locked read passed but the conditional write matched nothing: the row changed in the
                # gap SQLite leaves open. Recheck for the precise refusal; if it somehow passes, still refuse.
                _recheck_claim(
                    session,
                    identity=identity,
                    actor=actor,
                    job_id=job_id,
                    require_distinct=require_distinct,
                )
                raise HTTPException(
                    status_code=409,
                    detail=f"job {job_id!r} cannot be applied: it changed while being claimed",
                )
            candidate_snapshot = _verified_claim_snapshot(
                session,
                identity=identity,
                actor=actor,
                job_id=job_id,
                evaluated_digest=evaluated_digest,
            )
            job = session.get(CaliberRefinementJob, job_id)
            assert job is not None

            now = datetime.now(timezone.utc)
            approval = CaliberApprovalRequest(
                approval_id=new_approval_id(),
                job_id=job.job_id,
                agent_id=job.agent_id,
                status="approved",
                attempt_number=job.attempt_count or 1,
                eval_results=job.eval_results,
                candidate_snapshot=candidate_snapshot,
                diagnosis_snapshot=dict(job.diagnosis) if job.diagnosis else None,
                approved_by=actor,
                approved_at=now,
            )
            session.add(approval)
            # Intent first: commit the claim and the approval anchor before any
            # provider call. Previously they stayed in one open write transaction
            # across ``apply_candidate``, and MLflow registers the new prompt
            # version through its own engine. On a SQLite file shared by CALIBER and
            # MLflow that engine waited on this request's own write lock until its
            # busy timeout expired, so Apply failed every time (alpha gap P0-2).
            # The cost is that a failure must now compensate explicitly instead of
            # relying on rollback; see ``_revert_unstarted_apply``.
            session.commit()
            approval_id = approval.approval_id

        # A ``fake`` promoter (explicitly opted into) records a promotion without
        # touching the registry. Say so in the audit rows and the response so a
        # simulated release can never be mistaken for a real one.
        simulated = bool(getattr(promoter, "is_simulated", False))
        try:
            outcome = apply_candidate(
                session,
                job=job,
                approval=approval,
                promoter=promoter,
                actor=actor,
                terminal_status="applied",
                extra_audit={"simulated": True} if simulated else None,
            )

            audit_record(
                session,
                actor=actor,
                action="apply_candidate",
                entity_type="refinement_job",
                entity_id=job.job_id,
                details={
                    "approval_id": approval_id,
                    "artifact_ref": outcome.promotion.get("artifact_ref"),
                    "checkpoint_ids": outcome.checkpoint_ids,
                    "simulated": simulated,
                    "candidate_digest": evaluated_digest,
                },
            )
            session.commit()
        except Exception as exc:
            session.rollback()
            _revert_unstarted_apply(
                session, job_id=job_id, approval_id=approval_id, actor=actor, error=exc
            )
            raise
        agent_id = job.agent_id
        artifact_type = job.artifact_type
        artifact_ref = outcome.promotion.get("artifact_ref")
        payload: dict[str, object] = {
            "job_id": job.job_id,
            "status": job.status,
            "promotion": outcome.promotion,
            "simulated": simulated,
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
    factory: Any,
    *,
    job_id: str,
    actor: str,
    identity: CaliberIdentity,
    payload: ApprovalRequestChangesRequest,
) -> RefinementJobSchema:
    with factory() as session:
        load_visible_job(session, identity, job_id, action=ACTION_REVIEW)
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
    identity = resolve_identity(request)
    job_id = request.path_params["job_id"]
    body = await parse_json_object(request)
    payload = ApprovalRequestChangesRequest.model_validate(body)

    factory = get_session_factory(request)
    schema = await run_in_threadpool(
        _request_changes_sync,
        factory,
        job_id=job_id,
        actor=actor,
        identity=identity,
        payload=payload,
    )
    return envelope_response_dict({"job": schema.model_dump(mode="json")})


def register(app: Starlette) -> None:
    """Add the jobs routes to the given Starlette application."""
    app.routes.append(Route(LIST_PATH, list_jobs, methods=["GET"]))
    app.routes.append(Route(DETAIL_PATH, get_job, methods=["GET"]))
    app.routes.append(Route(TARGETS_PATH, get_job_targets, methods=["GET"]))
    app.routes.append(Route(APPLY_PATH, apply_job, methods=["POST"]))
    app.routes.append(Route(REQUEST_CHANGES_PATH, request_changes, methods=["POST"]))
