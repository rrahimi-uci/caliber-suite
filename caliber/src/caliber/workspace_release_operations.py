"""Intent-first Workspace release execution (`P5-C`).

This module owns the database-side parent/child operation protocol.  External
effects are delegated to :mod:`caliber.workspace_release_adapters`; an effect
that is not proven to have completed leaves both the operation and environment
in ``reconcile_required`` and never advances the current-release pointer.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from caliber.audit import record as audit_record
from caliber.db.models import (
    CaliberWorkspaceBreakGlassAuthorization,
    CaliberWorkspaceEnvironment,
    CaliberWorkspaceRelease,
    CaliberWorkspaceReleaseOperation,
    CaliberWorkspaceReleaseOperationItem,
    CaliberWorkspaceRevision,
    CaliberWorkspaceRevisionResource,
)
from caliber.ids import new_workspace_release_operation_item_id
from caliber.workspace_release_adapters import (
    PreparedAction,
    ProviderOutcome,
    WorkspaceProviderTimeoutError,
    WorkspaceReleaseAdapterError,
    WorkspaceResourceAdapter,
    WorkspaceResourceAdapterRegistry,
)
from caliber.workspace_release_operation_service import (
    OPERATION_APPLIED,
    OPERATION_APPLYING,
    OPERATION_CANCELLED,
    OPERATION_FAILED,
    OPERATION_PREPARED,
    OPERATION_RECONCILE_REQUIRED,
    WorkspaceReleaseOperationConflictError,
    create_workspace_release_operation,
    transition_workspace_release_operation,
)
from caliber.workspace_runtime_lineage import require_runtime_lineage


class WorkspaceReleaseExecutionError(WorkspaceReleaseOperationConflictError):
    """The operation cannot safely execute against its current coordinates."""


@dataclass(frozen=True)
class OperationExecutionResult:
    """A durable operation and its current child-item projection."""

    operation: CaliberWorkspaceReleaseOperation
    items: tuple[CaliberWorkspaceReleaseOperationItem, ...]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _require_operation(session: Session, operation_id: str) -> CaliberWorkspaceReleaseOperation:
    operation = session.get(CaliberWorkspaceReleaseOperation, operation_id)
    if operation is None:
        raise WorkspaceReleaseExecutionError("workspace release operation not found")
    return operation


def _require_environment(
    session: Session, operation: CaliberWorkspaceReleaseOperation
) -> CaliberWorkspaceEnvironment:
    environment = session.get(CaliberWorkspaceEnvironment, operation.environment_id)
    if environment is None or environment.project_id != operation.project_id:
        raise WorkspaceReleaseExecutionError("operation environment is outside the project")
    return environment


def _release(session: Session, release_id: str, project_id: str) -> CaliberWorkspaceRelease:
    release = session.get(CaliberWorkspaceRelease, release_id)
    if release is None or release.project_id != project_id:
        raise WorkspaceReleaseExecutionError("workspace release is outside the project")
    return release


def _pins(
    session: Session, release: CaliberWorkspaceRelease
) -> list[CaliberWorkspaceRevisionResource]:
    revision = session.get(CaliberWorkspaceRevision, release.revision_id)
    if revision is None or revision.project_id != release.project_id or revision.status != "ready":
        raise WorkspaceReleaseExecutionError("release revision is not ready")
    return list(
        session.execute(
            select(CaliberWorkspaceRevisionResource)
            .where(CaliberWorkspaceRevisionResource.revision_id == revision.revision_id)
            .order_by(
                CaliberWorkspaceRevisionResource.resource_type,
                CaliberWorkspaceRevisionResource.logical_name,
                CaliberWorkspaceRevisionResource.resource_pin_id,
            )
        ).scalars()
    )


def _current_refs(
    session: Session, environment: CaliberWorkspaceEnvironment
) -> dict[tuple[str, str], str | None]:
    if not environment.current_release_id:
        return {}
    release = session.get(CaliberWorkspaceRelease, environment.current_release_id)
    if release is None or release.project_id != environment.project_id:
        raise WorkspaceReleaseExecutionError("environment current release pointer is invalid")
    return {
        (pin.resource_type, pin.logical_name): pin.provider_ref for pin in _pins(session, release)
    }


def _operation_items(
    session: Session, operation_id: str
) -> list[CaliberWorkspaceReleaseOperationItem]:
    return list(
        session.execute(
            select(CaliberWorkspaceReleaseOperationItem)
            .where(
                CaliberWorkspaceReleaseOperationItem.workspace_release_operation_id == operation_id
            )
            .order_by(
                CaliberWorkspaceReleaseOperationItem.created_at,
                CaliberWorkspaceReleaseOperationItem.operation_item_id,
            )
        ).scalars()
    )


def _prepared_from_item(item: CaliberWorkspaceReleaseOperationItem) -> PreparedAction:
    # `P5-E`: `provider_result` is where `prepare_workspace_release_operation`
    # persisted `prepared.metadata` (merged with `resource_type`) -- rebuilding
    # `PreparedAction` without it silently dropped every adapter's own metadata
    # (project id, environment name, etc.) at apply/observe/rollback time,
    # forcing an adapter to encode everything into the ref strings instead.
    stored = item.provider_result if isinstance(item.provider_result, dict) else {}
    resource_type = stored.get("resource_type", "") if isinstance(stored, dict) else "unknown"
    metadata = {key: value for key, value in stored.items() if key != "resource_type"}
    return PreparedAction(
        resource_pin_id=item.revision_resource_id,
        resource_type=resource_type,
        action=item.action,
        target_ref=item.target_ref,
        before_ref=item.before_ref,
        after_ref=item.after_ref,
        metadata=metadata,
    )


def _set_environment_terminal(
    session: Session,
    environment: CaliberWorkspaceEnvironment,
    operation: CaliberWorkspaceReleaseOperation,
    *,
    current_release_id: str | None,
) -> None:
    result = session.execute(
        update(CaliberWorkspaceEnvironment)
        .where(
            CaliberWorkspaceEnvironment.environment_id == environment.environment_id,
            CaliberWorkspaceEnvironment.pending_operation_id == operation.operation_id,
        )
        .values(
            current_release_id=current_release_id,
            pending_operation_id=None,
            operation_state="idle",
            lock_version=CaliberWorkspaceEnvironment.lock_version + 1,
        )
    )
    if getattr(result, "rowcount", 0) != 1:
        raise WorkspaceReleaseExecutionError("operation no longer owns the environment lock")
    session.flush()


def _set_environment_reconcile_required(
    session: Session,
    environment: CaliberWorkspaceEnvironment,
    operation: CaliberWorkspaceReleaseOperation,
) -> None:
    result = session.execute(
        update(CaliberWorkspaceEnvironment)
        .where(
            CaliberWorkspaceEnvironment.environment_id == environment.environment_id,
            CaliberWorkspaceEnvironment.pending_operation_id == operation.operation_id,
        )
        .values(
            operation_state="reconcile_required",
            lock_version=CaliberWorkspaceEnvironment.lock_version + 1,
        )
    )
    if getattr(result, "rowcount", 0) != 1:
        raise WorkspaceReleaseExecutionError("operation no longer owns the environment lock")
    session.flush()


def _set_item_outcome(
    item: CaliberWorkspaceReleaseOperationItem,
    outcome: ProviderOutcome,
    *,
    completed: bool,
) -> None:
    item.status = outcome.status
    item.provider_operation_ref = outcome.provider_operation_ref
    existing_result = dict(item.provider_result or {})
    existing_result.update(outcome.provider_result)
    item.provider_result = existing_result
    item.error_code = outcome.error_code
    item.error_summary = outcome.error_summary
    if completed:
        item.completed_at = _now()


def _effect_method(
    operation: CaliberWorkspaceReleaseOperation,
    adapter: WorkspaceResourceAdapter,
) -> Callable[[Session, PreparedAction], ProviderOutcome]:
    """Select the provider effect for the operation's immutable intent."""

    if operation.kind == "rollback":
        return adapter.rollback_release
    return adapter.apply_release


def prepare_workspace_release_operation(
    session: Session,
    *,
    project_id: str,
    workspace_release_id: str,
    environment_id: str,
    kind: str,
    idempotency_key: str,
    expected_environment_lock_version: int,
    requested_by: str,
    adapters: WorkspaceResourceAdapterRegistry,
    expected_current_release_id: str | None = None,
    target_release_id: str | None = None,
    break_glass_authorization_id: str | None = None,
    operation_id: str | None = None,
) -> OperationExecutionResult:
    """Prepare a parent operation and all child intents without provider I/O."""

    existing = session.execute(
        select(CaliberWorkspaceReleaseOperation).where(
            CaliberWorkspaceReleaseOperation.project_id == project_id,
            CaliberWorkspaceReleaseOperation.kind == kind,
            CaliberWorkspaceReleaseOperation.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    operation = create_workspace_release_operation(
        session,
        project_id=project_id,
        workspace_release_id=workspace_release_id,
        environment_id=environment_id,
        kind=kind,
        idempotency_key=idempotency_key,
        expected_environment_lock_version=expected_environment_lock_version,
        requested_by=requested_by,
        expected_current_release_id=expected_current_release_id,
        target_release_id=target_release_id,
        break_glass_authorization_id=break_glass_authorization_id,
        operation_id=operation_id,
    )
    if existing is not None:
        return OperationExecutionResult(
            operation, tuple(_operation_items(session, operation.operation_id))
        )

    environment = _require_environment(session, operation)
    if environment.lock_version != expected_environment_lock_version:
        raise WorkspaceReleaseExecutionError("environment lock version is stale")
    if environment.status != "active":
        raise WorkspaceReleaseExecutionError("environment is not active")
    if environment.operation_state != "idle" or environment.pending_operation_id is not None:
        raise WorkspaceReleaseExecutionError("environment already has a pending operation")
    if (
        expected_current_release_id is not None
        and environment.current_release_id != expected_current_release_id
    ):
        raise WorkspaceReleaseExecutionError("environment current release is stale")

    release = _release(session, workspace_release_id, project_id)
    source_release = (
        _release(session, target_release_id, project_id)
        if kind == "rollback" and target_release_id
        else release
    )
    current_refs = _current_refs(session, environment)
    pins = _pins(session, source_release)
    if not pins:
        raise WorkspaceReleaseExecutionError("release has no resource pins")
    for pin in pins:
        adapter = adapters.require(pin.resource_type)
        before_ref = current_refs.get((pin.resource_type, pin.logical_name))
        prepared = adapter.prepare_release(pin, environment, before_ref=before_ref)
        session.add(
            CaliberWorkspaceReleaseOperationItem(
                operation_item_id=new_workspace_release_operation_item_id(),
                workspace_release_operation_id=operation.operation_id,
                revision_resource_id=pin.resource_pin_id,
                action=prepared.action,
                target_ref=prepared.target_ref,
                before_ref=prepared.before_ref,
                after_ref=prepared.after_ref,
                provider_result={
                    "resource_type": prepared.resource_type,
                    **dict(prepared.metadata),
                },
            )
        )
    session.flush()
    result = session.execute(
        update(CaliberWorkspaceEnvironment)
        .where(
            CaliberWorkspaceEnvironment.environment_id == environment.environment_id,
            CaliberWorkspaceEnvironment.lock_version == expected_environment_lock_version,
            CaliberWorkspaceEnvironment.pending_operation_id.is_(None),
            CaliberWorkspaceEnvironment.operation_state == "idle",
        )
        .values(
            pending_operation_id=operation.operation_id,
            operation_state="applying",
        )
    )
    if getattr(result, "rowcount", 0) != 1:
        raise WorkspaceReleaseExecutionError("environment lock was lost while preparing operation")
    audit_record(
        session,
        actor=requested_by,
        action="prepare_workspace_release_operation",
        entity_type="workspace_release_operation",
        entity_id=operation.operation_id,
        details={"kind": kind, "environment_id": environment_id, "item_count": len(pins)},
    )
    session.flush()
    return OperationExecutionResult(
        operation, tuple(_operation_items(session, operation.operation_id))
    )


def _finish_failed(
    session: Session,
    operation: CaliberWorkspaceReleaseOperation,
    environment: CaliberWorkspaceEnvironment,
    *,
    error_code: str,
    error_summary: str,
) -> OperationExecutionResult:
    transition_workspace_release_operation(
        session,
        operation,
        OPERATION_FAILED,
        actor="workspace-release-worker",
        expected_lock_version=operation.lock_version,
        error_code=error_code,
        error_summary=error_summary,
    )
    _set_environment_terminal(
        session, environment, operation, current_release_id=environment.current_release_id
    )
    return OperationExecutionResult(
        operation, tuple(_operation_items(session, operation.operation_id))
    )


def _finish_reconcile(
    session: Session,
    operation: CaliberWorkspaceReleaseOperation,
    environment: CaliberWorkspaceEnvironment,
    *,
    error_code: str,
    error_summary: str,
) -> OperationExecutionResult:
    transition_workspace_release_operation(
        session,
        operation,
        OPERATION_RECONCILE_REQUIRED,
        actor="workspace-release-worker",
        expected_lock_version=operation.lock_version,
        error_code=error_code,
        error_summary=error_summary,
    )
    _set_environment_reconcile_required(session, environment, operation)
    return OperationExecutionResult(
        operation, tuple(_operation_items(session, operation.operation_id))
    )


def apply_workspace_release_operation(  # noqa: PLR0911 - explicit child state machine
    session: Session,
    operation_id: str,
    *,
    adapters: WorkspaceResourceAdapterRegistry,
    actor: str = "workspace-release-worker",
) -> OperationExecutionResult:
    """Apply every child in order and settle the pointer only after all succeed."""

    with session.begin_nested():
        operation = _require_operation(session, operation_id)
        environment = _require_environment(session, operation)
        if operation.status != OPERATION_PREPARED:
            raise WorkspaceReleaseExecutionError(
                f"operation {operation_id!r} is {operation.status!r}, not prepared"
            )
        if environment.pending_operation_id != operation.operation_id:
            raise WorkspaceReleaseExecutionError("operation does not own the environment lock")
        if environment.status != "active":
            raise WorkspaceReleaseExecutionError("environment is not active")
        try:
            require_runtime_lineage(
                session,
                operation.runtime_lineage_id,
                consumer_kind="provider_operation",
                consumer_id=operation.operation_id,
                require_current_release=False,
            )
        except RuntimeError as exc:
            raise WorkspaceReleaseExecutionError(str(exc)) from exc
        transition_workspace_release_operation(
            session,
            operation,
            OPERATION_APPLYING,
            actor=actor,
            expected_lock_version=operation.lock_version,
        )
        for item in _operation_items(session, operation.operation_id):
            pin = session.get(CaliberWorkspaceRevisionResource, item.revision_resource_id)
            if pin is None:
                return _finish_failed(
                    session,
                    operation,
                    environment,
                    error_code="revision_resource_missing",
                    error_summary="operation item references a missing revision resource",
                )
            resource_type = (
                (item.provider_result or {}).get("resource_type") if item.provider_result else None
            )
            if not isinstance(resource_type, str):
                return _finish_failed(
                    session,
                    operation,
                    environment,
                    error_code="adapter_metadata_missing",
                    error_summary="operation item has no adapter type",
                )
            adapter = adapters.require(resource_type)
            prepared = _prepared_from_item(item)
            item.status = "applying"
            item.started_at = _now()
            session.flush()
            try:
                outcome = _effect_method(operation, adapter)(session, prepared)
            except WorkspaceProviderTimeoutError as exc:
                item.status = "reconcile_required" if exc.effect_started else "failed"
                item.error_code = "provider_timeout"
                item.error_summary = str(exc)
                item.completed_at = _now()
                if exc.effect_started:
                    return _finish_reconcile(
                        session,
                        operation,
                        environment,
                        error_code="provider_timeout",
                        error_summary=str(exc),
                    )
                return _finish_failed(
                    session,
                    operation,
                    environment,
                    error_code="provider_timeout_before_effect",
                    error_summary=str(exc),
                )
            except WorkspaceReleaseAdapterError as exc:
                item.status = "failed"
                item.error_code = "adapter_error"
                item.error_summary = str(exc)
                item.completed_at = _now()
                return _finish_failed(
                    session,
                    operation,
                    environment,
                    error_code="adapter_error",
                    error_summary=str(exc),
                )
            _set_item_outcome(item, outcome, completed=True)
            session.flush()
            if outcome.status == "reconcile_required":
                return _finish_reconcile(
                    session,
                    operation,
                    environment,
                    error_code=outcome.error_code or "ambiguous_provider_outcome",
                    error_summary=outcome.error_summary or "provider outcome requires observation",
                )
            if outcome.status == "failed":
                return _finish_failed(
                    session,
                    operation,
                    environment,
                    error_code=outcome.error_code or "provider_failure",
                    error_summary=outcome.error_summary or "provider rejected the child effect",
                )
        transition_workspace_release_operation(
            session,
            operation,
            OPERATION_APPLIED,
            actor=actor,
            expected_lock_version=operation.lock_version,
        )
        target = (
            operation.target_release_id
            if operation.kind == "rollback"
            else operation.workspace_release_id
        )
        _set_environment_terminal(session, environment, operation, current_release_id=target)
        audit_record(
            session,
            actor=actor,
            action="apply_workspace_release_operation",
            entity_type="workspace_release_operation",
            entity_id=operation.operation_id,
            details={"status": OPERATION_APPLIED, "current_release_id": target},
        )
        return OperationExecutionResult(
            operation, tuple(_operation_items(session, operation.operation_id))
        )


def observe_workspace_release_operation(  # noqa: PLR0915 - explicit observation state machine
    session: Session,
    operation_id: str,
    *,
    adapters: WorkspaceResourceAdapterRegistry,
    actor: str = "workspace-release-reconciler",
) -> OperationExecutionResult:
    """Observe an ambiguous operation and settle it only from normalized evidence."""

    with session.begin_nested():
        operation = _require_operation(session, operation_id)
        environment = _require_environment(session, operation)
        if operation.status != OPERATION_RECONCILE_REQUIRED:
            raise WorkspaceReleaseExecutionError(
                "only reconcile-required operations can be observed"
            )
        if environment.pending_operation_id != operation.operation_id:
            raise WorkspaceReleaseExecutionError("operation does not own the environment lock")
        unresolved = False
        known_failure = False
        for item in _operation_items(session, operation.operation_id):
            if item.status == "prepared":
                resource_type = (
                    (item.provider_result or {}).get("resource_type")
                    if item.provider_result
                    else None
                )
                if not isinstance(resource_type, str):
                    known_failure = True
                    continue
                adapter = adapters.require(resource_type)
                try:
                    outcome = _effect_method(operation, adapter)(session, _prepared_from_item(item))
                except WorkspaceProviderTimeoutError as exc:
                    item.status = "reconcile_required" if exc.effect_started else "failed"
                    item.error_code = "provider_timeout"
                    item.error_summary = str(exc)
                    item.completed_at = _now()
                    unresolved |= exc.effect_started
                    known_failure |= not exc.effect_started
                    continue
                except WorkspaceReleaseAdapterError as exc:
                    item.status = "failed"
                    item.error_code = "adapter_error"
                    item.error_summary = str(exc)
                    item.completed_at = _now()
                    known_failure = True
                    continue
                _set_item_outcome(item, outcome, completed=True)
                unresolved |= outcome.status == "reconcile_required"
                known_failure |= outcome.status == "failed"
                continue
            if item.status not in {"reconcile_required", "applying"}:
                if item.status == "failed":
                    known_failure = True
                continue
            resource_type = (
                (item.provider_result or {}).get("resource_type") if item.provider_result else None
            )
            if not isinstance(resource_type, str):
                known_failure = True
                continue
            outcome = adapters.require(resource_type).observe_release(
                session, _prepared_from_item(item)
            )
            _set_item_outcome(item, outcome, completed=outcome.status != "reconcile_required")
            unresolved |= outcome.status == "reconcile_required"
            known_failure |= outcome.status == "failed"
        operation.observation_count += 1
        operation.last_observed_at = _now()
        session.flush()
        if known_failure:
            return _finish_failed(
                session,
                operation,
                environment,
                error_code="provider_observed_failure",
                error_summary="observation proved at least one child effect failed",
            )
        if unresolved:
            operation.lock_version += 1
            _set_environment_reconcile_required(session, environment, operation)
            audit_record(
                session,
                actor=actor,
                action="observe_workspace_release_operation",
                entity_type="workspace_release_operation",
                entity_id=operation.operation_id,
                details={
                    "status": OPERATION_RECONCILE_REQUIRED,
                    "observation_count": operation.observation_count,
                },
            )
            return OperationExecutionResult(
                operation, tuple(_operation_items(session, operation.operation_id))
            )
        transition_workspace_release_operation(
            session,
            operation,
            OPERATION_APPLIED,
            actor=actor,
            expected_lock_version=operation.lock_version,
        )
        target = (
            operation.target_release_id
            if operation.kind == "rollback"
            else operation.workspace_release_id
        )
        _set_environment_terminal(session, environment, operation, current_release_id=target)
        audit_record(
            session,
            actor=actor,
            action="reconcile_workspace_release_operation",
            entity_type="workspace_release_operation",
            entity_id=operation.operation_id,
            details={"status": OPERATION_APPLIED, "current_release_id": target},
        )
        return OperationExecutionResult(
            operation, tuple(_operation_items(session, operation.operation_id))
        )


def cancel_expired_workspace_release_operation(
    session: Session,
    operation_id: str,
    *,
    actor: str = "workspace-release-reconciler",
) -> OperationExecutionResult:
    """Cancel an expired prepared break-glass intent before any provider effect."""

    with session.begin_nested():
        operation = _require_operation(session, operation_id)
        environment = _require_environment(session, operation)
        if operation.status != OPERATION_PREPARED or not operation.break_glass_authorization_id:
            raise WorkspaceReleaseExecutionError(
                "only prepared break-glass operations can be cancelled by expiry"
            )
        authorization = session.get(
            CaliberWorkspaceBreakGlassAuthorization, operation.break_glass_authorization_id
        )
        expiry = (
            authorization.expires_at.replace(tzinfo=timezone.utc)
            if authorization is not None and authorization.expires_at.tzinfo is None
            else authorization.expires_at
            if authorization is not None
            else None
        )
        if authorization is None or expiry is None or expiry > _now():
            raise WorkspaceReleaseExecutionError("break-glass authorization has not expired")
        transition_workspace_release_operation(
            session,
            operation,
            OPERATION_CANCELLED,
            actor=actor,
            expected_lock_version=operation.lock_version,
            error_code="break_glass_expired",
            error_summary="authorization expired before the first provider effect",
        )
        _set_environment_terminal(
            session, environment, operation, current_release_id=environment.current_release_id
        )
        return OperationExecutionResult(
            operation, tuple(_operation_items(session, operation.operation_id))
        )


__all__ = [
    "OperationExecutionResult",
    "WorkspaceReleaseExecutionError",
    "apply_workspace_release_operation",
    "cancel_expired_workspace_release_operation",
    "observe_workspace_release_operation",
    "prepare_workspace_release_operation",
]
