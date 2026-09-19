"""Provider-free Workspace release and durable evaluation services.

P5-A owns the durable contract and the literal state machines.  Provider
effects, role-specific decisions, environment pointers, and reconciliation are
intentionally handled by later Phase 5 slices.  These functions therefore only
write CALIBER state and are safe to exercise with an offline database.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from caliber.db.models import (
    CaliberProject,
    CaliberWorkspaceEnvironment,
    CaliberWorkspaceRelease,
    CaliberWorkspaceReleaseEvaluation,
    CaliberWorkspaceRevision,
)
from caliber.ids import new_workspace_release_evaluation_id, new_workspace_release_id
from caliber.workspace_runtime_lineage import create_runtime_lineage

RELEASE_DRAFT = "draft"
RELEASE_EVALUATING = "evaluating"
RELEASE_BLOCKED = "blocked"
RELEASE_REJECTED = "rejected"
RELEASE_APPROVED = "approved"
RELEASE_AWAITING_QUALITY_SIGNOFF = "awaiting_quality_signoff"
RELEASE_AWAITING_APPROVAL = "awaiting_approval"

EVALUATION_QUEUED = "queued"
EVALUATION_RUNNING = "running"
EVALUATION_SUCCEEDED = "succeeded"
EVALUATION_FAILED = "failed"

RELEASE_TRANSITIONS: dict[str, frozenset[str]] = {
    RELEASE_DRAFT: frozenset({RELEASE_EVALUATING}),
    RELEASE_EVALUATING: frozenset(
        {RELEASE_BLOCKED, RELEASE_REJECTED, RELEASE_APPROVED, RELEASE_AWAITING_QUALITY_SIGNOFF}
    ),
    RELEASE_BLOCKED: frozenset({RELEASE_EVALUATING}),
    RELEASE_REJECTED: frozenset(),
    RELEASE_APPROVED: frozenset(),
    RELEASE_AWAITING_QUALITY_SIGNOFF: frozenset(
        {RELEASE_REJECTED, RELEASE_APPROVED, RELEASE_AWAITING_APPROVAL}
    ),
    RELEASE_AWAITING_APPROVAL: frozenset({RELEASE_REJECTED, RELEASE_APPROVED}),
}

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_ERROR_CODE_LENGTH = 64
_MAX_ERROR_SUMMARY_LENGTH = 4000
EMPTY_DECISION_SET_SHA256 = hashlib.sha256(b"[]").hexdigest()

#: `caliber_projects.source_mode` (`P1-A`, migration `0093`). The only other
#: value is `"caliber_managed"` (the default) -- `schemas.py`'s
#: `WorkspaceSourceResponse.source_mode` is a closed two-value `Literal` and
#: `routes/workspace.py::_put_source_sync` is the only site that ever writes
#: `"git_managed"`, with no reverse transition. Git-managed authority is
#: therefore a one-way commitment: once a project configures a Git source,
#: local (CALIBER-managed) drafts stop being promotable beyond development.
SOURCE_MODE_GIT_MANAGED = "git_managed"

#: `caliber_workspace_revisions.source_kind` (`P4-C`, migration `0111`). The
#: only other value is `"git"` (the default), produced by the Git-import
#: materializer; `"managed"` is produced only by
#: `routes/workspace.py::snapshot_revision` and pins live CALIBER resource
#: versions with no Git commit behind them.
SOURCE_KIND_MANAGED = "managed"


class WorkspaceReleaseConflictError(RuntimeError):
    """The requested release mutation conflicts with durable state."""


class WorkspaceReleaseTransitionError(WorkspaceReleaseConflictError):
    """A release state transition is not legal for its current state."""


class WorkspaceReleaseLeaseError(WorkspaceReleaseConflictError):
    """An evaluation lease is missing, live, expired, or owned by another worker."""


class WorkspaceReleaseSourceModeError(WorkspaceReleaseConflictError):
    """A CALIBER-managed revision cannot release beyond development once the
    project has committed to Git-managed source authority (docs/workspace-plan.md
    Phase 4 item 11)."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _db_time(value: datetime) -> datetime:
    """Return a UTC-naive timestamp for the repository's DateTime columns."""
    return value.replace(tzinfo=None)


def _digest(value: str, name: str) -> str:
    if not _DIGEST_RE.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _require_release(session: Session, release_id: str) -> CaliberWorkspaceRelease:
    release = session.get(CaliberWorkspaceRelease, release_id)
    if release is None:
        raise WorkspaceReleaseConflictError("workspace release not found")
    return release


def _require_source_mode_promotion_eligible(
    session: Session,
    *,
    project_id: str,
    revision_id: str,
    environment_id: str,
) -> None:
    """Enforce Git-managed source authority before a new release is created.

    Only a project that has transitioned to ``git_managed`` is restricted,
    and only a CALIBER-managed (``source_kind="managed"``) revision is
    affected: it may still release to the ``development`` environment class
    but not beyond it until the same change is imported from a Git commit.
    Git-sourced revisions, and any revision under the default
    ``caliber_managed`` project mode, are always eligible -- the plan names
    only Git-managed authority as exclusive, never the reverse. Fails closed
    (raises) rather than silently allowing when the project, revision, or
    environment cannot be resolved, since those are exactly the coordinates
    this check depends on.

    Session factories in this codebase are configured with
    ``autoflush=False`` (``db/session.py::sessionmaker_from_engine``), so a
    caller that just added the project/revision/environment rows in the same
    session (a fresh Workspace, or a test fixture) would not see them via
    ``session.get``/``select`` without an explicit flush first.
    """

    session.flush()
    project = session.get(CaliberProject, project_id)
    if project is None:
        raise WorkspaceReleaseConflictError("workspace release project not found")
    if project.source_mode != SOURCE_MODE_GIT_MANAGED:
        return
    revision = session.get(CaliberWorkspaceRevision, revision_id)
    if revision is None or revision.project_id != project_id:
        raise WorkspaceReleaseConflictError("workspace release revision not found")
    if revision.source_kind != SOURCE_KIND_MANAGED:
        return
    environment = session.execute(
        select(CaliberWorkspaceEnvironment).where(
            CaliberWorkspaceEnvironment.environment_id == environment_id,
            CaliberWorkspaceEnvironment.project_id == project_id,
        )
    ).scalar_one_or_none()
    if environment is None:
        raise WorkspaceReleaseConflictError("workspace release environment not found")
    if environment.environment_class == "development":
        return
    raise WorkspaceReleaseSourceModeError(
        "git-managed projects cannot release a CALIBER-managed (non-Git) revision "
        "beyond the development environment; import the change from a Git commit "
        "first"
    )


def create_workspace_release(
    session: Session,
    *,
    project_id: str,
    revision_id: str,
    environment_id: str,
    environment_config_sha256: str,
    runtime_dependencies_sha256: str,
    policy_sha256: str,
    request_idempotency_key: str,
    requested_by: str,
    change_request_id: str | None = None,
    change_request_head_id: str | None = None,
    version_tag_id: str | None = None,
    predecessor_release_id: str | None = None,
    release_id: str | None = None,
) -> CaliberWorkspaceRelease:
    """Create or replay a draft release using an immutable request key."""

    if not request_idempotency_key.strip():
        raise ValueError("request_idempotency_key must not be empty")
    digests = {
        "environment_config_sha256": _digest(
            environment_config_sha256, "environment_config_sha256"
        ),
        "runtime_dependencies_sha256": _digest(
            runtime_dependencies_sha256, "runtime_dependencies_sha256"
        ),
        "policy_sha256": _digest(policy_sha256, "policy_sha256"),
    }
    existing = session.execute(
        select(CaliberWorkspaceRelease).where(
            CaliberWorkspaceRelease.project_id == project_id,
            CaliberWorkspaceRelease.request_idempotency_key == request_idempotency_key,
        )
    ).scalar_one_or_none()
    if existing is not None:
        requested = {
            "revision_id": revision_id,
            "environment_id": environment_id,
            "change_request_id": change_request_id,
            "change_request_head_id": change_request_head_id,
            "version_tag_id": version_tag_id,
            "predecessor_release_id": predecessor_release_id,
            **digests,
        }
        if any(getattr(existing, key) != value for key, value in requested.items()):
            raise WorkspaceReleaseConflictError("workspace release idempotency conflict")
        return existing

    _require_source_mode_promotion_eligible(
        session,
        project_id=project_id,
        revision_id=revision_id,
        environment_id=environment_id,
    )

    release = CaliberWorkspaceRelease(
        release_id=release_id or new_workspace_release_id(),
        project_id=project_id,
        revision_id=revision_id,
        environment_id=environment_id,
        change_request_id=change_request_id,
        change_request_head_id=change_request_head_id,
        version_tag_id=version_tag_id,
        predecessor_release_id=predecessor_release_id,
        **digests,
        request_idempotency_key=request_idempotency_key,
        requested_by=requested_by,
    )
    session.add(release)
    session.flush()
    return release


def transition_workspace_release(
    session: Session,
    release: CaliberWorkspaceRelease,
    target_status: str,
    *,
    actor: str,
    expected_lock_version: int,
    error_code: str | None = None,
    error_summary: str | None = None,
    evaluation_evidence_sha256: str | None = None,
    decision_set_sha256: str | None = None,
) -> CaliberWorkspaceRelease:
    """CAS one literal release transition and return the refreshed row."""

    current = _require_release(session, release.release_id)
    if target_status not in RELEASE_TRANSITIONS.get(current.status, frozenset()):
        raise WorkspaceReleaseTransitionError(
            f"illegal Workspace release transition {current.status!r} -> {target_status!r}"
        )
    if current.lock_version != expected_lock_version:
        raise WorkspaceReleaseConflictError("workspace release lock version is stale")
    if error_code is not None and len(error_code) > _MAX_ERROR_CODE_LENGTH:
        raise ValueError(f"error_code exceeds {_MAX_ERROR_CODE_LENGTH} characters")
    if error_summary is not None and len(error_summary) > _MAX_ERROR_SUMMARY_LENGTH:
        raise ValueError(f"error_summary exceeds {_MAX_ERROR_SUMMARY_LENGTH} characters")
    values: dict[str, object] = {
        "status": target_status,
        "lock_version": expected_lock_version + 1,
        "evaluated_by": actor if target_status in {RELEASE_APPROVED, RELEASE_REJECTED} else None,
        "evaluated_at": _now() if target_status in {RELEASE_APPROVED, RELEASE_REJECTED} else None,
        "error_code": error_code,
        "error_summary": error_summary,
    }
    if evaluation_evidence_sha256 is not None:
        values["evaluation_evidence_sha256"] = _digest(
            evaluation_evidence_sha256, "evaluation_evidence_sha256"
        )
    if decision_set_sha256 is not None:
        values["decision_set_sha256"] = _digest(decision_set_sha256, "decision_set_sha256")
    result = session.execute(
        update(CaliberWorkspaceRelease)
        .where(
            CaliberWorkspaceRelease.release_id == current.release_id,
            CaliberWorkspaceRelease.lock_version == expected_lock_version,
        )
        .values(**values)
    )
    if getattr(result, "rowcount", 0) != 1:
        raise WorkspaceReleaseConflictError("workspace release transition lost its CAS race")
    session.flush()
    session.refresh(current)
    return current


def start_workspace_release_evaluation(
    session: Session,
    release: CaliberWorkspaceRelease,
    *,
    actor: str,
    expected_lock_version: int,
) -> CaliberWorkspaceRelease:
    """Move a draft release into durable machine evaluation."""

    return transition_workspace_release(
        session,
        release,
        RELEASE_EVALUATING,
        actor=actor,
        expected_lock_version=expected_lock_version,
    )


def block_workspace_release(
    session: Session,
    release: CaliberWorkspaceRelease,
    *,
    actor: str,
    expected_lock_version: int,
    error_code: str,
    error_summary: str,
) -> CaliberWorkspaceRelease:
    """Record a retryable operational blocker without changing coordinates."""

    return transition_workspace_release(
        session,
        release,
        RELEASE_BLOCKED,
        actor=actor,
        expected_lock_version=expected_lock_version,
        error_code=error_code,
        error_summary=error_summary,
    )


def resume_blocked_workspace_release(
    session: Session,
    release: CaliberWorkspaceRelease,
    *,
    actor: str,
    expected_lock_version: int,
    revision_id: str,
    environment_config_sha256: str,
    runtime_dependencies_sha256: str,
    policy_sha256: str,
) -> CaliberWorkspaceRelease:
    """Resume only a blocked release whose immutable evaluation coordinates match."""

    current = _require_release(session, release.release_id)
    expected = {
        "revision_id": revision_id,
        "environment_config_sha256": environment_config_sha256,
        "runtime_dependencies_sha256": runtime_dependencies_sha256,
        "policy_sha256": policy_sha256,
    }
    if any(getattr(current, key) != value for key, value in expected.items()):
        raise WorkspaceReleaseConflictError("blocked release coordinates changed")
    return transition_workspace_release(
        session,
        current,
        RELEASE_EVALUATING,
        actor=actor,
        expected_lock_version=expected_lock_version,
        error_code=None,
        error_summary=None,
    )


def _release_environment(
    session: Session, release: CaliberWorkspaceRelease
) -> CaliberWorkspaceEnvironment:
    environment = session.execute(
        select(CaliberWorkspaceEnvironment).where(
            CaliberWorkspaceEnvironment.environment_id == release.environment_id,
            CaliberWorkspaceEnvironment.project_id == release.project_id,
        )
    ).scalar_one_or_none()
    if environment is None:
        raise WorkspaceReleaseConflictError("workspace release environment not found")
    return environment


def settle_machine_evaluation(
    session: Session,
    release: CaliberWorkspaceRelease,
    *,
    actor: str,
    expected_lock_version: int,
    result: Literal["pass", "no_go", "blocked"],
    evidence_sha256: str | None = None,
    error_code: str | None = None,
    error_summary: str | None = None,
    predecessor_valid: bool = True,
) -> CaliberWorkspaceRelease:
    """Settle a provider-free machine result under the environment policy.

    Development and staging machine passes become approved.  Staging still
    requires the caller to prove its predecessor contract; QA and production
    stop at quality sign-off for P5-B.  ``no_go`` is terminal and an
    operational blocker is retryable.
    """

    current = _require_release(session, release.release_id)
    environment = _release_environment(session, current)
    if result == "blocked":
        return block_workspace_release(
            session,
            current,
            actor=actor,
            expected_lock_version=expected_lock_version,
            error_code=error_code or "evaluation_blocked",
            error_summary=error_summary or "machine evaluation could not run",
        )
    if evidence_sha256 is None:
        raise ValueError("machine evaluation settlement requires evidence_sha256")
    if result == "no_go":
        return transition_workspace_release(
            session,
            current,
            RELEASE_REJECTED,
            actor=actor,
            expected_lock_version=expected_lock_version,
            error_code=error_code or "machine_gate_no_go",
            error_summary=error_summary or "machine gate rejected the release",
            evaluation_evidence_sha256=evidence_sha256,
        )
    if environment.environment_class == "staging" and not predecessor_valid:
        return block_workspace_release(
            session,
            current,
            actor=actor,
            expected_lock_version=expected_lock_version,
            error_code="predecessor_missing",
            error_summary="staging requires a valid predecessor release",
        )
    target = (
        RELEASE_APPROVED
        if environment.environment_class in {"development", "staging"}
        else RELEASE_AWAITING_QUALITY_SIGNOFF
    )
    return transition_workspace_release(
        session,
        current,
        target,
        actor=actor,
        expected_lock_version=expected_lock_version,
        evaluation_evidence_sha256=evidence_sha256,
        decision_set_sha256=EMPTY_DECISION_SET_SHA256 if target == RELEASE_APPROVED else None,
    )


def create_workspace_release_evaluation(
    session: Session,
    *,
    project_id: str,
    workspace_release_id: str,
    idempotency_key: str,
    evaluation_plan_sha256: str,
    input_sha256: str,
    requested_by: str,
    evaluation_id: str | None = None,
) -> CaliberWorkspaceReleaseEvaluation:
    """Create or replay one queued evaluation attempt before worker dispatch."""

    if not idempotency_key.strip():
        raise ValueError("idempotency_key must not be empty")
    _digest(evaluation_plan_sha256, "evaluation_plan_sha256")
    _digest(input_sha256, "input_sha256")
    release = _require_release(session, workspace_release_id)
    if release.status != RELEASE_EVALUATING:
        raise WorkspaceReleaseConflictError("release is not evaluating")
    existing = session.execute(
        select(CaliberWorkspaceReleaseEvaluation).where(
            CaliberWorkspaceReleaseEvaluation.project_id == project_id,
            CaliberWorkspaceReleaseEvaluation.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if (
            existing.workspace_release_id != workspace_release_id
            or existing.evaluation_plan_sha256 != evaluation_plan_sha256
            or existing.input_sha256 != input_sha256
        ):
            raise WorkspaceReleaseConflictError("workspace evaluation idempotency conflict")
        return existing
    if release.project_id != project_id:
        raise WorkspaceReleaseConflictError("release is outside the requested project")
    active = session.execute(
        select(CaliberWorkspaceReleaseEvaluation).where(
            CaliberWorkspaceReleaseEvaluation.workspace_release_id == workspace_release_id,
            CaliberWorkspaceReleaseEvaluation.status.in_((EVALUATION_QUEUED, EVALUATION_RUNNING)),
        )
    ).scalar_one_or_none()
    if active is not None:
        raise WorkspaceReleaseConflictError("release already has an active evaluation attempt")
    evaluation = CaliberWorkspaceReleaseEvaluation(
        evaluation_id=evaluation_id or new_workspace_release_evaluation_id(),
        project_id=project_id,
        workspace_release_id=workspace_release_id,
        idempotency_key=idempotency_key,
        evaluation_plan_sha256=evaluation_plan_sha256,
        input_sha256=input_sha256,
        requested_by=requested_by,
    )
    session.add(evaluation)
    session.flush()
    lineage = create_runtime_lineage(
        session,
        project_id=project_id,
        workspace_release_id=workspace_release_id,
        revision_id=release.revision_id,
        environment_id=release.environment_id,
        consumer_kind="evidence",
        consumer_id=evaluation.evaluation_id,
        config_sha256=release.environment_config_sha256,
        strict_execution=False,
        created_by=requested_by,
    )
    evaluation.runtime_lineage_id = lineage.lineage_id
    session.flush()
    return evaluation


def claim_workspace_release_evaluation(
    session: Session,
    evaluation: CaliberWorkspaceReleaseEvaluation,
    *,
    worker_id: str,
    lease_seconds: int = 300,
    now: datetime | None = None,
) -> CaliberWorkspaceReleaseEvaluation:
    """Claim a queued attempt, or reclaim a running attempt after lease expiry."""

    if not worker_id.strip() or lease_seconds < 1:
        raise ValueError("worker_id must be non-empty and lease_seconds must be positive")
    current = session.get(CaliberWorkspaceReleaseEvaluation, evaluation.evaluation_id)
    if current is None:
        raise WorkspaceReleaseLeaseError("workspace evaluation not found")
    moment = _utc(now) or _now()
    stamp = _db_time(moment)
    if current.status in {EVALUATION_SUCCEEDED, EVALUATION_FAILED}:
        raise WorkspaceReleaseLeaseError("terminal evaluation cannot be claimed")
    if current.status == EVALUATION_RUNNING:
        lease = _utc(current.lease_expires_at)
        if current.claimed_by == worker_id and lease is not None and lease > moment:
            renewed = session.execute(
                update(CaliberWorkspaceReleaseEvaluation)
                .where(
                    CaliberWorkspaceReleaseEvaluation.evaluation_id == current.evaluation_id,
                    CaliberWorkspaceReleaseEvaluation.status == EVALUATION_RUNNING,
                    CaliberWorkspaceReleaseEvaluation.claimed_by == worker_id,
                    CaliberWorkspaceReleaseEvaluation.lease_expires_at == current.lease_expires_at,
                    CaliberWorkspaceReleaseEvaluation.lease_expires_at > stamp,
                )
                .values(
                    heartbeat_at=stamp,
                    lease_expires_at=stamp + timedelta(seconds=lease_seconds),
                )
            )
            if getattr(renewed, "rowcount", 0) != 1:
                raise WorkspaceReleaseLeaseError("evaluation claim was lost during renewal")
            session.flush()
            session.refresh(current)
            return current
        if lease is not None and lease > moment:
            raise WorkspaceReleaseLeaseError("evaluation lease is owned by another live worker")
        claim = (
            update(CaliberWorkspaceReleaseEvaluation)
            .where(
                CaliberWorkspaceReleaseEvaluation.evaluation_id == current.evaluation_id,
                CaliberWorkspaceReleaseEvaluation.status == EVALUATION_RUNNING,
                or_(
                    CaliberWorkspaceReleaseEvaluation.lease_expires_at.is_(None),
                    CaliberWorkspaceReleaseEvaluation.lease_expires_at <= stamp,
                ),
            )
            .values(
                status=EVALUATION_RUNNING,
                attempt_number=CaliberWorkspaceReleaseEvaluation.attempt_number + 1,
                claimed_by=worker_id,
                started_by=worker_id,
                started_at=current.started_at or stamp,
                heartbeat_at=stamp,
                lease_expires_at=stamp + timedelta(seconds=lease_seconds),
            )
        )
    else:
        claim = (
            update(CaliberWorkspaceReleaseEvaluation)
            .where(
                CaliberWorkspaceReleaseEvaluation.evaluation_id == current.evaluation_id,
                CaliberWorkspaceReleaseEvaluation.status == EVALUATION_QUEUED,
            )
            .values(
                status=EVALUATION_RUNNING,
                claimed_by=worker_id,
                started_by=worker_id,
                started_at=stamp,
                heartbeat_at=stamp,
                lease_expires_at=stamp + timedelta(seconds=lease_seconds),
            )
        )
    claimed = session.execute(claim)
    if getattr(claimed, "rowcount", 0) != 1:
        raise WorkspaceReleaseLeaseError("evaluation claim lost its CAS race")
    session.flush()
    session.refresh(current)
    return current


def heartbeat_workspace_release_evaluation(
    session: Session,
    evaluation: CaliberWorkspaceReleaseEvaluation,
    *,
    worker_id: str,
    lease_seconds: int = 300,
    now: datetime | None = None,
) -> CaliberWorkspaceReleaseEvaluation:
    """Extend a live lease only for its current owner."""

    if not worker_id.strip() or lease_seconds < 1:
        raise ValueError("worker_id must be non-empty and lease_seconds must be positive")
    current = session.get(CaliberWorkspaceReleaseEvaluation, evaluation.evaluation_id)
    if current is None or current.status != EVALUATION_RUNNING:
        raise WorkspaceReleaseLeaseError("evaluation is not running")
    moment = _utc(now) or _now()
    stamp = _db_time(moment)
    if current.claimed_by != worker_id or (_utc(current.lease_expires_at) or moment) <= moment:
        raise WorkspaceReleaseLeaseError("evaluation lease is not live for this worker")
    renewed = session.execute(
        update(CaliberWorkspaceReleaseEvaluation)
        .where(
            CaliberWorkspaceReleaseEvaluation.evaluation_id == current.evaluation_id,
            CaliberWorkspaceReleaseEvaluation.status == EVALUATION_RUNNING,
            CaliberWorkspaceReleaseEvaluation.claimed_by == worker_id,
            CaliberWorkspaceReleaseEvaluation.lease_expires_at == current.lease_expires_at,
            CaliberWorkspaceReleaseEvaluation.lease_expires_at > stamp,
        )
        .values(
            heartbeat_at=stamp,
            lease_expires_at=stamp + timedelta(seconds=lease_seconds),
        )
    )
    if getattr(renewed, "rowcount", 0) != 1:
        raise WorkspaceReleaseLeaseError("evaluation heartbeat lost its CAS race")
    session.flush()
    session.refresh(current)
    return current


def complete_workspace_release_evaluation(
    session: Session,
    evaluation: CaliberWorkspaceReleaseEvaluation,
    *,
    worker_id: str,
    result: Literal["pass", "no_go", "blocked"],
    expected_release_lock_version: int,
    evidence_sha256: str | None = None,
    gate_verdict_id: str | None = None,
    evaluation_run_ids: list[str] | None = None,
    error_code: str | None = None,
    error_summary: str | None = None,
    predecessor_valid: bool = True,
    now: datetime | None = None,
) -> CaliberWorkspaceReleaseEvaluation:
    """Complete an attempt and settle its release in the same transaction."""

    current = session.get(CaliberWorkspaceReleaseEvaluation, evaluation.evaluation_id)
    if current is None or current.status != EVALUATION_RUNNING:
        raise WorkspaceReleaseLeaseError("evaluation is not running")
    if current.claimed_by != worker_id:
        raise WorkspaceReleaseLeaseError("evaluation lease is owned by another worker")
    moment = _utc(now) or _now()
    if (_utc(current.lease_expires_at) or moment) <= moment:
        raise WorkspaceReleaseLeaseError("evaluation lease has expired")
    release = _require_release(session, current.workspace_release_id)
    if result == "blocked":
        release = settle_machine_evaluation(
            session,
            release,
            actor=worker_id,
            expected_lock_version=expected_release_lock_version,
            result="blocked",
            error_code=error_code,
            error_summary=error_summary,
            predecessor_valid=predecessor_valid,
        )
        current.status = EVALUATION_FAILED
    else:
        release = settle_machine_evaluation(
            session,
            release,
            actor=worker_id,
            expected_lock_version=expected_release_lock_version,
            result=result,
            evidence_sha256=evidence_sha256,
            error_code=error_code,
            error_summary=error_summary,
            predecessor_valid=predecessor_valid,
        )
        current.status = EVALUATION_SUCCEEDED
    current.completed_by = worker_id
    current.completed_at = moment
    current.lease_expires_at = None
    current.heartbeat_at = None
    current.gate_verdict_id = gate_verdict_id
    current.linked_evaluation_run_ids = list(evaluation_run_ids or [])
    current.error_code = error_code if result == "blocked" else None
    current.error_summary = error_summary if result == "blocked" else None
    session.flush()
    return current


__all__ = [
    "EMPTY_DECISION_SET_SHA256",
    "EVALUATION_FAILED",
    "EVALUATION_QUEUED",
    "EVALUATION_RUNNING",
    "EVALUATION_SUCCEEDED",
    "RELEASE_APPROVED",
    "RELEASE_AWAITING_APPROVAL",
    "RELEASE_AWAITING_QUALITY_SIGNOFF",
    "RELEASE_BLOCKED",
    "RELEASE_DRAFT",
    "RELEASE_EVALUATING",
    "RELEASE_REJECTED",
    "RELEASE_TRANSITIONS",
    "SOURCE_KIND_MANAGED",
    "SOURCE_MODE_GIT_MANAGED",
    "WorkspaceReleaseConflictError",
    "WorkspaceReleaseLeaseError",
    "WorkspaceReleaseSourceModeError",
    "WorkspaceReleaseTransitionError",
    "block_workspace_release",
    "claim_workspace_release_evaluation",
    "complete_workspace_release_evaluation",
    "create_workspace_release",
    "create_workspace_release_evaluation",
    "heartbeat_workspace_release_evaluation",
    "resume_blocked_workspace_release",
    "settle_machine_evaluation",
    "start_workspace_release_evaluation",
    "transition_workspace_release",
]
