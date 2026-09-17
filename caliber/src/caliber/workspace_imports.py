"""Durable, provider-neutral lifecycle operations for Workspace imports.

This module owns the database arbitration around ``CaliberWorkspaceImportJob``.
It deliberately does not materialize source files, call a source provider, or
blindly retry a worker whose outcome is ambiguous. A worker must hold a live
lease while it works; an expired lease is converted to
``reconcile_required`` so an operator or a later reconcile command can observe
the external state before deciding what to do. Known failures may be
deliberately requeued only while the job's total attempt budget remains.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from caliber.db.models import CaliberWorkspaceImportJob

IMPORT_JOB_STATUS_QUEUED = "queued"
IMPORT_JOB_STATUS_RUNNING = "running"
IMPORT_JOB_STATUS_SUCCEEDED = "succeeded"
IMPORT_JOB_STATUS_FAILED = "failed"
IMPORT_JOB_STATUS_RECONCILE_REQUIRED = "reconcile_required"
IMPORT_JOB_TERMINAL_STATUSES = frozenset(
    {
        IMPORT_JOB_STATUS_SUCCEEDED,
        IMPORT_JOB_STATUS_FAILED,
        IMPORT_JOB_STATUS_RECONCILE_REQUIRED,
    }
)

DEFAULT_IMPORT_LEASE_SECONDS = 300.0
MAX_IMPORT_LEASE_SECONDS = 3600.0
DEFAULT_IMPORT_MAX_ATTEMPTS = 3
MAX_IMPORT_ERROR_SUMMARY_CHARS = 2048
MAX_IMPORT_WORKER_ID_CHARS = 128
MAX_IMPORT_ERROR_CODE_CHARS = 64


class WorkspaceImportJobError(RuntimeError):
    """Base error for a rejected import-job lifecycle operation."""

    code = "workspace_import_job_error"


class WorkspaceImportLeaseLostError(WorkspaceImportJobError):
    """Raised when a worker no longer owns a live import lease."""

    code = "import_job_lease_lost"


class WorkspaceImportRetryExhaustedError(WorkspaceImportJobError):
    """Raised when a known import failure has no attempts remaining."""

    code = "import_retry_exhausted"


class WorkspaceImportTransitionError(ValueError, WorkspaceImportJobError):
    """Raised when an import job cannot enter the requested terminal state."""

    code = "invalid_import_job_transition"


def _utc_naive(now: datetime | None) -> datetime:
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(timezone.utc).replace(tzinfo=None)
    return stamp


def _validate_worker_id(worker_id: str) -> str:
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise ValueError("worker_id must be a non-empty string")
    if len(worker_id) > MAX_IMPORT_WORKER_ID_CHARS:
        raise ValueError(f"worker_id must be at most {MAX_IMPORT_WORKER_ID_CHARS} characters")
    return worker_id


def _lease_delta(lease_seconds: float) -> timedelta:
    if not isinstance(lease_seconds, (int, float)) or isinstance(lease_seconds, bool):
        raise ValueError("lease_seconds must be a finite positive number")
    if not math.isfinite(lease_seconds) or lease_seconds <= 0:
        raise ValueError("lease_seconds must be a finite positive number")
    if lease_seconds > MAX_IMPORT_LEASE_SECONDS:
        raise ValueError(f"lease_seconds must be at most {MAX_IMPORT_LEASE_SECONDS:g} seconds")
    return timedelta(seconds=float(lease_seconds))


def _validate_error(
    *, error_code: str | None, error_summary: str | None
) -> tuple[str | None, str | None]:
    if not isinstance(error_code, str) or not error_code.strip():
        raise ValueError("error_code is required for failed or reconcile-required imports")
    if len(error_code) > MAX_IMPORT_ERROR_CODE_CHARS:
        raise ValueError(f"error_code must be at most {MAX_IMPORT_ERROR_CODE_CHARS} characters")
    if error_summary is not None:
        if not isinstance(error_summary, str):
            raise ValueError("error_summary must be a string when provided")
        if len(error_summary) > MAX_IMPORT_ERROR_SUMMARY_CHARS:
            raise ValueError(
                f"error_summary must be at most {MAX_IMPORT_ERROR_SUMMARY_CHARS} characters"
            )
    return error_code, error_summary


def claim_next_import_job(
    session: Session,
    *,
    worker_id: str,
    lease_seconds: float = DEFAULT_IMPORT_LEASE_SECONDS,
    now: datetime | None = None,
) -> CaliberWorkspaceImportJob | None:
    """Atomically claim the oldest queued import job for ``worker_id``.

    The candidate read is only an ordering hint.  The conditional UPDATE is
    the arbitration: if another worker claims the same row first, this call
    returns ``None``.  The operation commits the claim so another worker can
    observe it immediately; callers should use a dedicated worker session.
    """
    worker_id = _validate_worker_id(worker_id)
    lease = _lease_delta(lease_seconds)
    stamp = _utc_naive(now)

    candidate = session.execute(
        select(CaliberWorkspaceImportJob)
        .where(
            CaliberWorkspaceImportJob.status == IMPORT_JOB_STATUS_QUEUED,
            CaliberWorkspaceImportJob.attempt_count < CaliberWorkspaceImportJob.max_attempts,
        )
        .order_by(
            CaliberWorkspaceImportJob.created_at.asc(),
            CaliberWorkspaceImportJob.import_job_id.asc(),
        )
        .limit(1)
    ).scalar_one_or_none()
    if candidate is None:
        return None

    claimed = cast(
        CursorResult[Any],
        session.execute(
            update(CaliberWorkspaceImportJob)
            .where(
                CaliberWorkspaceImportJob.import_job_id == candidate.import_job_id,
                CaliberWorkspaceImportJob.status == IMPORT_JOB_STATUS_QUEUED,
                CaliberWorkspaceImportJob.attempt_count < CaliberWorkspaceImportJob.max_attempts,
            )
            .values(
                status=IMPORT_JOB_STATUS_RUNNING,
                claimed_by=worker_id,
                claimed_at=stamp,
                lease_expires_at=stamp + lease,
                last_heartbeat_at=stamp,
                error_code=None,
                error_summary=None,
                completed_at=None,
                attempt_count=CaliberWorkspaceImportJob.attempt_count + 1,
                updated_by=worker_id,
            )
        ),
    )
    session.commit()
    if claimed.rowcount != 1:
        return None
    return session.get(CaliberWorkspaceImportJob, candidate.import_job_id)


def retry_failed_import_job(
    session: Session,
    import_job_id: str,
    *,
    actor: str,
    now: datetime | None = None,
) -> CaliberWorkspaceImportJob:
    """Deliberately requeue a known failed import while its budget remains.

    Only a terminal ``failed`` outcome is retryable. ``reconcile_required`` is
    intentionally excluded because a lost worker may have produced an
    external revision or object; that state needs observation by the later
    reconcile API before any new attempt is queued. The previous failure
    details remain visible while the job is queued and are cleared by its next
    claim.
    """
    actor = _validate_worker_id(actor)
    stamp = _utc_naive(now)
    requeued = cast(
        CursorResult[Any],
        session.execute(
            update(CaliberWorkspaceImportJob)
            .where(
                CaliberWorkspaceImportJob.import_job_id == import_job_id,
                CaliberWorkspaceImportJob.status == IMPORT_JOB_STATUS_FAILED,
                CaliberWorkspaceImportJob.attempt_count < CaliberWorkspaceImportJob.max_attempts,
            )
            .values(
                status=IMPORT_JOB_STATUS_QUEUED,
                claimed_by=None,
                claimed_at=None,
                lease_expires_at=None,
                last_heartbeat_at=None,
                completed_at=None,
                updated_at=stamp,
                updated_by=actor,
            )
        ),
    )
    session.commit()
    if requeued.rowcount == 1:
        job = session.get(CaliberWorkspaceImportJob, import_job_id)
        if job is None:  # pragma: no cover - the primary-key update proved existence
            raise WorkspaceImportJobError(f"import job {import_job_id!r} disappeared")
        return job

    job = session.get(CaliberWorkspaceImportJob, import_job_id)
    if job is None:
        raise WorkspaceImportJobError(f"import job {import_job_id!r} was not found")
    if job.status == IMPORT_JOB_STATUS_FAILED and job.attempt_count >= job.max_attempts:
        raise WorkspaceImportRetryExhaustedError(
            f"import job {import_job_id!r} exhausted its {job.max_attempts}-attempt budget"
        )
    raise WorkspaceImportTransitionError(
        f"only failed imports with remaining budget may be retried; "
        f"job {import_job_id!r} is {job.status!r}"
    )


def heartbeat_import_job(
    session: Session,
    import_job_id: str,
    *,
    worker_id: str,
    lease_seconds: float = DEFAULT_IMPORT_LEASE_SECONDS,
    now: datetime | None = None,
) -> CaliberWorkspaceImportJob:
    """Renew a live import lease, refusing stale or foreign workers."""
    worker_id = _validate_worker_id(worker_id)
    lease = _lease_delta(lease_seconds)
    stamp = _utc_naive(now)
    renewed = cast(
        CursorResult[Any],
        session.execute(
            update(CaliberWorkspaceImportJob)
            .where(
                CaliberWorkspaceImportJob.import_job_id == import_job_id,
                CaliberWorkspaceImportJob.status == IMPORT_JOB_STATUS_RUNNING,
                CaliberWorkspaceImportJob.claimed_by == worker_id,
                CaliberWorkspaceImportJob.lease_expires_at.is_not(None),
                CaliberWorkspaceImportJob.lease_expires_at > stamp,
            )
            .values(
                lease_expires_at=stamp + lease,
                last_heartbeat_at=stamp,
                updated_by=worker_id,
            )
        ),
    )
    session.commit()
    if renewed.rowcount != 1:
        raise WorkspaceImportLeaseLostError(
            f"worker {worker_id!r} no longer owns a live import lease for {import_job_id!r}"
        )
    job = session.get(CaliberWorkspaceImportJob, import_job_id)
    if job is None:  # pragma: no cover - the primary-key update already proved existence
        raise WorkspaceImportLeaseLostError(f"import job {import_job_id!r} disappeared")
    return job


def finish_import_job(
    session: Session,
    import_job_id: str,
    *,
    worker_id: str,
    status: str,
    revision_id: str | None = None,
    error_code: str | None = None,
    error_summary: str | None = None,
    now: datetime | None = None,
) -> CaliberWorkspaceImportJob:
    """Commit one owner-bound terminal transition for an import job.

    ``succeeded`` requires the immutable revision produced by materialization.
    ``failed`` and ``reconcile_required`` require a stable error code.  A
    worker whose lease expired cannot publish a late result, preventing a
    stale process from overwriting a later reconciliation decision.
    """
    worker_id = _validate_worker_id(worker_id)
    stamp = _utc_naive(now)
    if status not in IMPORT_JOB_TERMINAL_STATUSES:
        raise WorkspaceImportTransitionError(
            f"terminal import status must be one of {sorted(IMPORT_JOB_TERMINAL_STATUSES)}"
        )
    if status == IMPORT_JOB_STATUS_SUCCEEDED:
        if not revision_id:
            raise WorkspaceImportTransitionError("revision_id is required for a succeeded import")
        if error_code is not None or error_summary is not None:
            raise WorkspaceImportTransitionError("succeeded imports cannot carry error details")
    else:
        error_code, error_summary = _validate_error(
            error_code=error_code, error_summary=error_summary
        )

    finished = cast(
        CursorResult[Any],
        session.execute(
            update(CaliberWorkspaceImportJob)
            .where(
                CaliberWorkspaceImportJob.import_job_id == import_job_id,
                CaliberWorkspaceImportJob.status == IMPORT_JOB_STATUS_RUNNING,
                CaliberWorkspaceImportJob.claimed_by == worker_id,
                CaliberWorkspaceImportJob.lease_expires_at.is_not(None),
                CaliberWorkspaceImportJob.lease_expires_at > stamp,
            )
            .values(
                status=status,
                revision_id=revision_id,
                error_code=error_code,
                error_summary=error_summary,
                completed_at=stamp,
                lease_expires_at=None,
                last_heartbeat_at=stamp,
                updated_by=worker_id,
            )
        ),
    )
    session.commit()
    if finished.rowcount != 1:
        raise WorkspaceImportLeaseLostError(
            f"worker {worker_id!r} no longer owns a live import lease for {import_job_id!r}"
        )
    job = session.get(CaliberWorkspaceImportJob, import_job_id)
    if job is None:  # pragma: no cover - the primary-key update already proved existence
        raise WorkspaceImportLeaseLostError(f"import job {import_job_id!r} disappeared")
    return job


def reconcile_expired_import_jobs(
    session: Session,
    *,
    now: datetime | None = None,
    limit: int = 100,
    actor: str = "caliber.import-reconciler",
) -> list[str]:
    """Move expired running imports to ``reconcile_required``.

    Each update repeats the lease predicate, so a heartbeat or terminal
    completion that wins the race leaves the job untouched.  This is an
    explicit recovery boundary, not a retry queue: an import may have written
    an external object or revision before its worker disappeared.
    """
    if limit < 1:
        raise ValueError("limit must be positive")
    actor = _validate_worker_id(actor)
    stamp = _utc_naive(now)
    candidates = session.execute(
        select(CaliberWorkspaceImportJob.import_job_id)
        .where(
            CaliberWorkspaceImportJob.status == IMPORT_JOB_STATUS_RUNNING,
            CaliberWorkspaceImportJob.lease_expires_at.is_not(None),
            CaliberWorkspaceImportJob.lease_expires_at <= stamp,
        )
        .order_by(
            CaliberWorkspaceImportJob.lease_expires_at.asc(),
            CaliberWorkspaceImportJob.import_job_id.asc(),
        )
        .limit(limit)
    ).scalars()

    reconciled: list[str] = []
    for import_job_id in candidates:
        updated = cast(
            CursorResult[Any],
            session.execute(
                update(CaliberWorkspaceImportJob)
                .where(
                    CaliberWorkspaceImportJob.import_job_id == import_job_id,
                    CaliberWorkspaceImportJob.status == IMPORT_JOB_STATUS_RUNNING,
                    CaliberWorkspaceImportJob.lease_expires_at.is_not(None),
                    CaliberWorkspaceImportJob.lease_expires_at <= stamp,
                )
                .values(
                    status=IMPORT_JOB_STATUS_RECONCILE_REQUIRED,
                    error_code="import_lease_expired",
                    error_summary="worker lease expired; observe external effects before retrying",
                    completed_at=stamp,
                    lease_expires_at=None,
                    updated_by=actor,
                )
            ),
        )
        if updated.rowcount == 1:
            reconciled.append(import_job_id)
    session.commit()
    return reconciled


__all__ = [
    "DEFAULT_IMPORT_LEASE_SECONDS",
    "DEFAULT_IMPORT_MAX_ATTEMPTS",
    "IMPORT_JOB_STATUS_FAILED",
    "IMPORT_JOB_STATUS_QUEUED",
    "IMPORT_JOB_STATUS_RECONCILE_REQUIRED",
    "IMPORT_JOB_STATUS_RUNNING",
    "IMPORT_JOB_STATUS_SUCCEEDED",
    "IMPORT_JOB_TERMINAL_STATUSES",
    "MAX_IMPORT_ERROR_SUMMARY_CHARS",
    "MAX_IMPORT_LEASE_SECONDS",
    "WorkspaceImportJobError",
    "WorkspaceImportLeaseLostError",
    "WorkspaceImportRetryExhaustedError",
    "WorkspaceImportTransitionError",
    "claim_next_import_job",
    "finish_import_job",
    "heartbeat_import_job",
    "reconcile_expired_import_jobs",
    "retry_failed_import_job",
]
