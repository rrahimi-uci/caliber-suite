"""Provider-free Workspace release-operation state machine (`P5-A`)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from caliber.db.models import (
    CaliberWorkspaceBreakGlassAuthorization,
    CaliberWorkspaceEnvironment,
    CaliberWorkspaceRelease,
    CaliberWorkspaceReleaseOperation,
    CaliberWorkspaceRevision,
)
from caliber.ids import new_workspace_release_operation_id
from caliber.workspace_runtime_lineage import create_runtime_lineage

OPERATION_PREPARED = "prepared"
OPERATION_APPLYING = "applying"
OPERATION_APPLIED = "applied"
OPERATION_FAILED = "failed"
OPERATION_RECONCILE_REQUIRED = "reconcile_required"
OPERATION_CANCELLED = "cancelled"

OPERATION_TRANSITIONS: dict[str, frozenset[str]] = {
    OPERATION_PREPARED: frozenset({OPERATION_APPLYING, OPERATION_CANCELLED}),
    OPERATION_APPLYING: frozenset(
        {OPERATION_APPLIED, OPERATION_FAILED, OPERATION_RECONCILE_REQUIRED}
    ),
    OPERATION_RECONCILE_REQUIRED: frozenset({OPERATION_APPLIED, OPERATION_FAILED}),
    OPERATION_APPLIED: frozenset(),
    OPERATION_FAILED: frozenset(),
    OPERATION_CANCELLED: frozenset(),
}
_MAX_ERROR_CODE_LENGTH = 64
_MAX_ERROR_SUMMARY_LENGTH = 4000


class WorkspaceReleaseOperationConflictError(RuntimeError):
    """The requested Workspace operation conflicts with durable state."""


class WorkspaceReleaseOperationTransitionError(WorkspaceReleaseOperationConflictError):
    """An operation state transition is not legal."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def create_workspace_release_operation(  # noqa: PLR0912, PLR0915
    session: Session,
    *,
    project_id: str,
    workspace_release_id: str,
    environment_id: str,
    kind: str,
    idempotency_key: str,
    expected_environment_lock_version: int,
    requested_by: str,
    expected_current_release_id: str | None = None,
    target_release_id: str | None = None,
    break_glass_authorization_id: str | None = None,
    operation_id: str | None = None,
) -> CaliberWorkspaceReleaseOperation:
    """Create or replay an intent without invoking a provider."""

    if kind not in {"apply", "rollback"}:
        raise ValueError("kind must be apply or rollback")
    if not idempotency_key.strip():
        raise ValueError("idempotency_key must not be empty")
    if expected_environment_lock_version < 1:
        raise ValueError("expected_environment_lock_version must be positive")
    if kind == "apply" and target_release_id is not None:
        raise WorkspaceReleaseOperationConflictError("apply cannot name a rollback target")
    if kind == "rollback" and not target_release_id:
        raise WorkspaceReleaseOperationConflictError("rollback requires a target release")
    if kind == "rollback" and expected_current_release_id != workspace_release_id:
        raise WorkspaceReleaseOperationConflictError(
            "rollback expected_current_release_id must be the replaced release"
        )
    if kind == "rollback" and target_release_id == workspace_release_id:
        raise WorkspaceReleaseOperationConflictError("rollback target must be a prior release")
    release = session.get(CaliberWorkspaceRelease, workspace_release_id)
    if release is None or release.project_id != project_id:
        raise WorkspaceReleaseOperationConflictError("workspace release is outside the project")
    if release.environment_id != environment_id:
        raise WorkspaceReleaseOperationConflictError("release environment does not match operation")
    environment = session.get(CaliberWorkspaceEnvironment, environment_id)
    if environment is None or environment.project_id != project_id:
        raise WorkspaceReleaseOperationConflictError("operation environment is outside the project")
    if break_glass_authorization_id is None and kind == "apply" and release.status != "approved":
        raise WorkspaceReleaseOperationConflictError("only an approved release can be applied")
    if break_glass_authorization_id is not None:
        if kind != "apply":
            raise WorkspaceReleaseOperationConflictError(
                "break-glass authorization can only create an apply operation"
            )
        if release.status not in {"awaiting_quality_signoff", "awaiting_approval"}:
            raise WorkspaceReleaseOperationConflictError(
                "break-glass apply requires a release awaiting a human decision"
            )
        authorization = session.get(
            CaliberWorkspaceBreakGlassAuthorization, break_glass_authorization_id
        )
        if authorization is None:
            raise WorkspaceReleaseOperationConflictError("break-glass authorization not found")
        if (
            authorization.project_id != project_id
            or authorization.workspace_release_id != workspace_release_id
            or authorization.environment_id != environment_id
            or authorization.credential_kind != "session"
            or not authorization.credential_id
            or environment.name != "prod"
            or environment.environment_class != "production"
        ):
            raise WorkspaceReleaseOperationConflictError(
                "break-glass authorization coordinates are invalid"
            )
        expiry = _utc(authorization.expires_at)
        if expiry is None or expiry <= _now():
            raise WorkspaceReleaseOperationConflictError("break-glass authorization has expired")
        revision = session.get(CaliberWorkspaceRevision, release.revision_id)
        if revision is None or authorization.revision_sha256 != revision.revision_sha256:
            raise WorkspaceReleaseOperationConflictError(
                "break-glass authorization revision binding is invalid"
            )
        if any(
            getattr(authorization, key) != getattr(release, release_key)
            for key, release_key in (
                ("environment_config_sha256", "environment_config_sha256"),
                ("runtime_dependencies_sha256", "runtime_dependencies_sha256"),
                ("policy_sha256", "policy_sha256"),
                ("gate_evidence_sha256", "evaluation_evidence_sha256"),
            )
        ):
            raise WorkspaceReleaseOperationConflictError(
                "break-glass authorization digest binding is invalid"
            )
    existing = session.execute(
        select(CaliberWorkspaceReleaseOperation).where(
            CaliberWorkspaceReleaseOperation.project_id == project_id,
            CaliberWorkspaceReleaseOperation.kind == kind,
            CaliberWorkspaceReleaseOperation.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    requested = {
        "workspace_release_id": workspace_release_id,
        "environment_id": environment_id,
        "target_release_id": target_release_id,
        "expected_current_release_id": expected_current_release_id,
        "expected_environment_lock_version": expected_environment_lock_version,
        "break_glass_authorization_id": break_glass_authorization_id,
    }
    if existing is not None:
        if any(getattr(existing, key) != value for key, value in requested.items()):
            raise WorkspaceReleaseOperationConflictError("workspace operation idempotency conflict")
        return existing
    active = session.execute(
        select(CaliberWorkspaceReleaseOperation.operation_id).where(
            CaliberWorkspaceReleaseOperation.environment_id == environment_id,
            CaliberWorkspaceReleaseOperation.status.in_(
                (OPERATION_PREPARED, OPERATION_APPLYING, OPERATION_RECONCILE_REQUIRED)
            ),
        )
    ).scalar_one_or_none()
    if active is not None:
        raise WorkspaceReleaseOperationConflictError("environment already has an active operation")
    if kind == "rollback":
        target = session.get(CaliberWorkspaceRelease, target_release_id)
        if (
            target is None
            or target.project_id != project_id
            or target.environment_id != environment_id
        ):
            raise WorkspaceReleaseOperationConflictError(
                "rollback target has different coordinates"
            )
        if not session.execute(
            select(CaliberWorkspaceReleaseOperation.operation_id).where(
                CaliberWorkspaceReleaseOperation.project_id == project_id,
                CaliberWorkspaceReleaseOperation.environment_id == environment_id,
                CaliberWorkspaceReleaseOperation.workspace_release_id == target_release_id,
                CaliberWorkspaceReleaseOperation.kind == "apply",
                CaliberWorkspaceReleaseOperation.status == OPERATION_APPLIED,
            )
        ).first():
            raise WorkspaceReleaseOperationConflictError("rollback target was never applied")
    operation = CaliberWorkspaceReleaseOperation(
        operation_id=operation_id or new_workspace_release_operation_id(),
        project_id=project_id,
        workspace_release_id=workspace_release_id,
        environment_id=environment_id,
        kind=kind,
        target_release_id=target_release_id,
        idempotency_key=idempotency_key,
        expected_current_release_id=expected_current_release_id,
        expected_environment_lock_version=expected_environment_lock_version,
        requested_by=requested_by,
        break_glass_authorization_id=break_glass_authorization_id,
    )
    session.add(operation)
    session.flush()
    lineage = create_runtime_lineage(
        session,
        project_id=project_id,
        workspace_release_id=workspace_release_id,
        revision_id=release.revision_id,
        environment_id=environment_id,
        consumer_kind="provider_operation",
        consumer_id=operation.operation_id,
        config_sha256=release.environment_config_sha256,
        strict_execution=False,
        created_by=requested_by,
        allow_break_glass=break_glass_authorization_id is not None,
    )
    operation.runtime_lineage_id = lineage.lineage_id
    session.flush()
    return operation


def transition_workspace_release_operation(
    session: Session,
    operation: CaliberWorkspaceReleaseOperation,
    target_status: str,
    *,
    actor: str,
    expected_lock_version: int,
    error_code: str | None = None,
    error_summary: str | None = None,
) -> CaliberWorkspaceReleaseOperation:
    """CAS one literal operation transition and return the refreshed row."""

    current = session.get(CaliberWorkspaceReleaseOperation, operation.operation_id)
    if current is None:
        raise WorkspaceReleaseOperationConflictError("workspace release operation not found")
    if target_status not in OPERATION_TRANSITIONS.get(current.status, frozenset()):
        raise WorkspaceReleaseOperationTransitionError(
            f"illegal Workspace operation transition {current.status!r} -> {target_status!r}"
        )
    if current.lock_version != expected_lock_version:
        raise WorkspaceReleaseOperationConflictError("workspace operation lock version is stale")
    if error_code is not None and len(error_code) > _MAX_ERROR_CODE_LENGTH:
        raise ValueError(f"error_code exceeds {_MAX_ERROR_CODE_LENGTH} characters")
    if error_summary is not None and len(error_summary) > _MAX_ERROR_SUMMARY_LENGTH:
        raise ValueError(f"error_summary exceeds {_MAX_ERROR_SUMMARY_LENGTH} characters")
    values: dict[str, object] = {
        "status": target_status,
        "lock_version": expected_lock_version + 1,
        "error_code": error_code,
        "error_summary": error_summary,
    }
    if target_status == OPERATION_APPLYING:
        values.update({"applied_by": actor, "applied_at": _now()})
    if target_status in {
        OPERATION_APPLIED,
        OPERATION_FAILED,
        OPERATION_CANCELLED,
    }:
        values.update({"completed_by": actor, "completed_at": _now()})
    result = session.execute(
        update(CaliberWorkspaceReleaseOperation)
        .where(
            CaliberWorkspaceReleaseOperation.operation_id == current.operation_id,
            CaliberWorkspaceReleaseOperation.lock_version == expected_lock_version,
        )
        .values(**values)
    )
    if getattr(result, "rowcount", 0) != 1:
        raise WorkspaceReleaseOperationConflictError(
            "workspace operation transition lost its CAS race"
        )
    session.flush()
    session.refresh(current)
    return current


__all__ = [
    "OPERATION_APPLIED",
    "OPERATION_APPLYING",
    "OPERATION_CANCELLED",
    "OPERATION_FAILED",
    "OPERATION_PREPARED",
    "OPERATION_RECONCILE_REQUIRED",
    "OPERATION_TRANSITIONS",
    "WorkspaceReleaseOperationConflictError",
    "WorkspaceReleaseOperationTransitionError",
    "create_workspace_release_operation",
    "transition_workspace_release_operation",
]
