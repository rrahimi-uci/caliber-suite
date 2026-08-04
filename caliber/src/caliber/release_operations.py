"""Crash-observable release intents for external prompt-alias mutations.

Provider aliases and CALIBER's relational state cannot share one transaction.
This module therefore uses an intent-first state machine:

``prepared -> applying -> applied``

Any exception after the provider call begins is recorded as
``reconcile_required``.  Reconciliation compares the live alias with the exact
before/after versions in the durable operation and settles the row without
guessing a predecessor.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from caliber.audit import record as audit_record
from caliber.db.models import (
    CaliberApprovalRequest,
    CaliberRefinementJob,
    CaliberReleaseOperation,
)
from caliber.ids import new_release_operation_id

AliasMutator = Callable[..., dict[str, Any]]
AliasResolver = Callable[[str, str], dict[str, Any] | None]


class ReleaseOperationConflictError(RuntimeError):
    """A reused operation ID names a different requested mutation."""


class ReleaseMutationNotStartedError(RuntimeError):
    """A preflight failure proving that no provider mutation was attempted."""

    def __init__(self, cause: Exception) -> None:
        super().__init__(str(cause))
        self.cause = cause


def prepare_prompt_alias_release(
    session: Session,
    *,
    name: str,
    alias: str,
    version_before: int | None,
    version_after: int,
    actor: str,
    operation_type: str = "promote",
    operation_id: str | None = None,
    effective_scopes: Iterable[str] = (),
    evidence: dict[str, Any] | None = None,
    approval_id: str | None = None,
) -> CaliberReleaseOperation:
    """Persist and commit an idempotent prompt-alias release intent.

    The explicit commit is the safety boundary: callers must not invoke the
    provider until this function returns.  Reusing ``operation_id`` is safe only
    for the exact same requested mutation.
    """
    operation_id = operation_id or new_release_operation_id()
    existing = session.get(CaliberReleaseOperation, operation_id)
    if existing is not None:
        requested = (
            operation_type,
            name,
            alias,
            version_after,
        )
        persisted = (
            existing.operation_type,
            existing.resource_name,
            existing.target_name,
            existing.version_after,
        )
        if requested != persisted:
            raise ReleaseOperationConflictError(
                f"release operation {operation_id!r} already describes a different mutation"
            )
        return existing

    row = CaliberReleaseOperation(
        operation_id=operation_id,
        operation_type=operation_type,
        resource_type="prompt",
        resource_name=name,
        target_name=alias,
        active_lock=f"prompt:{name}:{alias}",
        version_before=version_before,
        version_after=version_after,
        actor=actor,
        effective_scopes=sorted(set(effective_scopes)),
        evidence=dict(evidence or {}),
        approval_id=approval_id,
        status="prepared",
    )
    session.add(row)
    try:
        audit_record(
            session,
            actor=actor,
            action="prepare_prompt_release",
            entity_type="prompt",
            entity_id=name,
            details={
                "operation_id": operation_id,
                "operation_type": operation_type,
                "alias": alias,
                "from_version": version_before,
                "to_version": version_after,
                "evidence": dict(evidence or {}),
                "approval_id": approval_id,
            },
        )
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        # A concurrent retry can lose the INSERT race on `operation_id` even
        # though it describes the exact same idempotent request. Once the winner
        # commits, return its row instead of turning a safe retry into a 409.
        concurrent = session.get(CaliberReleaseOperation, operation_id)
        if concurrent is not None:
            requested = (operation_type, name, alias, version_after)
            persisted = (
                concurrent.operation_type,
                concurrent.resource_name,
                concurrent.target_name,
                concurrent.version_after,
            )
            if requested == persisted:
                return concurrent
        raise ReleaseOperationConflictError(
            f"another incomplete release already owns prompt {name!r} alias {alias!r}"
        ) from exc
    session.refresh(row)
    return row


def execute_prompt_alias_release(
    session: Session,
    operation: CaliberReleaseOperation,
    *,
    mutate_alias: AliasMutator,
) -> dict[str, Any]:
    """Apply a prepared mutation and durably settle its result.

    Alias assignment is idempotent for the same ``(name, alias, version)``.
    Retrying an ``applying`` or ``reconcile_required`` operation is therefore
    safe and uses the same operation ID and target.
    """
    operation = session.get(CaliberReleaseOperation, operation.operation_id) or operation
    if operation.status == "applied":
        return dict(operation.provider_result or {})

    operation.status = "applying"
    operation.last_error = None
    session.commit()

    try:
        result = mutate_alias(
            name=operation.resource_name,
            alias=operation.target_name,
            version=operation.version_after,
        )
    except ReleaseMutationNotStartedError as exc:
        session.rollback()
        current = session.get(CaliberReleaseOperation, operation.operation_id)
        if current is not None:
            current.status = "failed"
            current.active_lock = None
            current.last_error = f"{type(exc.cause).__name__}: {exc.cause}"[:4000]
            audit_record(
                session,
                actor=current.actor,
                action="prompt_release_failed",
                entity_type="prompt",
                entity_id=current.resource_name,
                details={
                    "operation_id": current.operation_id,
                    "alias": current.target_name,
                    "from_version": current.version_before,
                    "to_version": current.version_after,
                    "provider_call_started": False,
                    "error": current.last_error,
                },
            )
            session.commit()
        raise exc.cause from exc
    except Exception as exc:
        session.rollback()
        current = session.get(CaliberReleaseOperation, operation.operation_id)
        if current is not None:
            current.status = "reconcile_required"
            current.last_error = f"{type(exc).__name__}: {exc}"[:4000]
            audit_record(
                session,
                actor=current.actor,
                action="prompt_release_reconcile_required",
                entity_type="prompt",
                entity_id=current.resource_name,
                details={
                    "operation_id": current.operation_id,
                    "alias": current.target_name,
                    "from_version": current.version_before,
                    "to_version": current.version_after,
                    "error": current.last_error,
                },
            )
            session.commit()
        raise

    current = session.get(CaliberReleaseOperation, operation.operation_id)
    if current is None:  # pragma: no cover - externally deleted invariant row
        raise RuntimeError(f"release operation {operation.operation_id!r} disappeared")
    current.status = "applied"
    current.active_lock = None
    current.provider_result = dict(result)
    current.applied_at = datetime.now(timezone.utc)
    action = "promote_prompt" if current.operation_type == "promote" else "rollback_prompt"
    audit_record(
        session,
        actor=current.actor,
        action=action,
        entity_type="prompt",
        entity_id=current.resource_name,
        details={
            "operation_id": current.operation_id,
            "alias": current.target_name,
            "from_version": current.version_before,
            "to_version": current.version_after,
            **dict(current.evidence or {}),
        },
    )
    session.commit()
    return dict(result)


def reconcile_prompt_alias_releases(
    session: Session,
    *,
    resolve_alias: AliasResolver,
    limit: int = 100,
) -> list[CaliberReleaseOperation]:
    """Settle incomplete prompt operations from observed provider state."""
    rows = list(
        session.execute(
            select(CaliberReleaseOperation)
            .where(CaliberReleaseOperation.resource_type == "prompt")
            .where(CaliberReleaseOperation.status.in_(("applying", "reconcile_required")))
            .order_by(CaliberReleaseOperation.created_at.asc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    now = datetime.now(timezone.utc)
    for row in rows:
        info = resolve_alias(row.resource_name, row.target_name)
        observed = info.get("version") if isinstance(info, dict) else None
        job_id = (row.evidence or {}).get("job_id")
        if not (isinstance(job_id, str) and job_id) and row.approval_id:
            approval = session.get(CaliberApprovalRequest, row.approval_id)
            job_id = approval.job_id if approval is not None else None
        job = (
            session.get(CaliberRefinementJob, job_id)
            if isinstance(job_id, str) and job_id
            else None
        )
        if observed == row.version_after:
            row.status = "applied"
            row.active_lock = None
            row.applied_at = row.applied_at or now
            row.last_error = None
            action = "promote_prompt" if row.operation_type == "promote" else "rollback_prompt"
            audit_record(
                session,
                actor="system:release-reconciler",
                action=action,
                entity_type="prompt",
                entity_id=row.resource_name,
                details={
                    "operation_id": row.operation_id,
                    "alias": row.target_name,
                    "from_version": row.version_before,
                    "to_version": row.version_after,
                    "reconciled": True,
                    **dict(row.evidence or {}),
                },
            )
            if job is not None and job.status == "applying":
                job.status = "applied"
                job.current_stage = "done"
                audit_record(
                    session,
                    actor="system:release-reconciler",
                    action="reconcile_apply_candidate",
                    entity_type="refinement_job",
                    entity_id=job.job_id,
                    details={"operation_id": row.operation_id, "outcome": "applied"},
                )
        elif row.version_before is not None and observed == row.version_before:
            row.status = "failed"
            row.active_lock = None
            row.last_error = "provider still resolves the recorded pre-release version"
            if job is not None and job.status == "applying":
                job.status = "candidate_ready"
                job.current_stage = "done"
                audit_record(
                    session,
                    actor="system:release-reconciler",
                    action="reconcile_apply_candidate",
                    entity_type="refinement_job",
                    entity_id=job.job_id,
                    details={"operation_id": row.operation_id, "outcome": "not_applied"},
                )
        else:
            row.status = "reconcile_required"
            row.last_error = (
                f"provider resolves unexpected version {observed!r}; expected "
                f"{row.version_before!r} or {row.version_after!r}"
            )
    session.commit()
    return rows


def serialize_release_operation(row: CaliberReleaseOperation) -> dict[str, Any]:
    return {
        "operation_id": row.operation_id,
        "operation_type": row.operation_type,
        "resource_type": row.resource_type,
        "resource_name": row.resource_name,
        "target_name": row.target_name,
        "active_lock": row.active_lock,
        "version_before": row.version_before,
        "version_after": row.version_after,
        "actor": row.actor,
        "effective_scopes": list(row.effective_scopes or []),
        "evidence": dict(row.evidence or {}),
        "approval_id": row.approval_id,
        "status": row.status,
        "provider_result": row.provider_result,
        "last_error": row.last_error,
        "applied_at": row.applied_at.isoformat() if row.applied_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
