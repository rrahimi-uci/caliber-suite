"""Immutable Workspace runtime lineage and strict execution checks.

P5-D keeps runtime provenance in one durable record instead of asking each
provider or run table to reinterpret mutable aliases.  A consumer may be a
run, release evidence row, or Workspace provider operation.  Legacy consumers
can remain unlinked; callers entering the strict Workspace path must link and
validate their lineage before doing work.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.db.models import (
    CaliberWorkspaceEnvironment,
    CaliberWorkspaceRelease,
    CaliberWorkspaceReleaseEvidence,
    CaliberWorkspaceReleaseOperation,
    CaliberWorkspaceRevision,
    CaliberWorkspaceRuntimeLineage,
)
from caliber.ids import new_workspace_release_evidence_id, new_workspace_runtime_lineage_id

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_CONSUMER_KINDS = frozenset({"run", "evidence", "provider_operation"})


class RuntimeLineageError(RuntimeError):
    """The runtime cannot prove its immutable Workspace coordinates."""


class MissingRuntimeLineageError(RuntimeLineageError):
    """Strict execution was requested without a lineage link."""


@dataclass(frozen=True)
class RuntimeLineageSnapshot:
    """Joined release/revision/environment view for incident reconstruction."""

    lineage: CaliberWorkspaceRuntimeLineage
    release: CaliberWorkspaceRelease
    revision: CaliberWorkspaceRevision
    environment: CaliberWorkspaceEnvironment

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-safe, queryable provenance projection."""

        return {
            "lineage_id": self.lineage.lineage_id,
            "project_id": self.lineage.project_id,
            "consumer_kind": self.lineage.consumer_kind,
            "consumer_id": self.lineage.consumer_id,
            "workspace_release_id": self.lineage.workspace_release_id,
            "revision_id": self.lineage.revision_id,
            "environment_id": self.lineage.environment_id,
            "model_id": self.lineage.model_id,
            "config_sha256": self.lineage.config_sha256,
            "runtime_dependencies_sha256": self.lineage.runtime_dependencies_sha256,
            "policy_sha256": self.lineage.policy_sha256,
            "eligibility_status": self.lineage.eligibility_status,
            "eligibility_reason": self.lineage.eligibility_reason,
            "strict_execution": self.lineage.strict_execution,
            "release_status": self.release.status,
            "environment_status": self.environment.status,
            "environment_operation_state": self.environment.operation_state,
            "current_release_id": self.environment.current_release_id,
            "revision_status": self.revision.status,
            "revision_sha256": self.revision.revision_sha256,
        }


def _digest(value: str, name: str) -> str:
    if not _DIGEST_RE.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _require_coordinates(
    session: Session,
    *,
    project_id: str,
    workspace_release_id: str,
    revision_id: str,
    environment_id: str,
) -> tuple[CaliberWorkspaceRelease, CaliberWorkspaceRevision, CaliberWorkspaceEnvironment]:
    release = session.get(CaliberWorkspaceRelease, workspace_release_id)
    revision = session.get(CaliberWorkspaceRevision, revision_id)
    environment = session.get(CaliberWorkspaceEnvironment, environment_id)
    if release is None or release.project_id != project_id:
        raise RuntimeLineageError("runtime release is outside the project")
    if revision is None or revision.project_id != project_id:
        raise RuntimeLineageError("runtime revision is outside the project")
    if environment is None or environment.project_id != project_id:
        raise RuntimeLineageError("runtime environment is outside the project")
    if release.revision_id != revision_id or release.environment_id != environment_id:
        raise RuntimeLineageError("runtime lineage coordinates do not match the release")
    return release, revision, environment


def _eligibility(
    release: CaliberWorkspaceRelease,
    environment: CaliberWorkspaceEnvironment,
    *,
    consumer_kind: str,
    consumer_id: str,
    allow_break_glass: bool = False,
) -> tuple[str, str]:
    reasons: list[str] = []
    if release.status != "approved" and not (
        allow_break_glass
        and consumer_kind == "provider_operation"
        and release.status in {"awaiting_quality_signoff", "awaiting_approval"}
    ):
        reasons.append(f"release_status={release.status}")
    if environment.status != "active":
        reasons.append(f"environment_status={environment.status}")
    if consumer_kind == "run":
        if environment.current_release_id != release.release_id:
            reasons.append("environment_current_release_mismatch")
        if environment.operation_state != "idle":
            reasons.append(f"environment_operation_state={environment.operation_state}")
    elif consumer_kind == "provider_operation":
        # The operation service writes the pending pointer after inserting its
        # operation and lineage.  Strict validation later requires the pointer
        # to name this operation, fencing stale workers without making prepare
        # depend on mutation order.
        if environment.pending_operation_id not in {None, consumer_id}:
            reasons.append("environment_owned_by_another_operation")
    return (
        ("eligible", "release, revision, and environment coordinates are bound")
        if not reasons
        else (
            "stale",
            "; ".join(reasons),
        )
    )


def create_runtime_lineage(
    session: Session,
    *,
    project_id: str,
    workspace_release_id: str,
    revision_id: str,
    environment_id: str,
    consumer_kind: str,
    consumer_id: str,
    model_id: str | None = None,
    config_sha256: str | None = None,
    strict_execution: bool = True,
    created_by: str = "",
    allow_break_glass: bool = False,
) -> CaliberWorkspaceRuntimeLineage:
    """Create or replay an immutable lineage record for one consumer.

    ``config_sha256`` defaults to the release's captured environment digest.
    A caller may not substitute a different config, runtime dependency, or
    policy digest: doing so would sever the release decision's evidence chain.
    """

    if consumer_kind not in _CONSUMER_KINDS:
        raise ValueError(f"unsupported runtime lineage consumer kind {consumer_kind!r}")
    if not consumer_id.strip():
        raise ValueError("consumer_id must not be empty")
    release, _revision, environment = _require_coordinates(
        session,
        project_id=project_id,
        workspace_release_id=workspace_release_id,
        revision_id=revision_id,
        environment_id=environment_id,
    )
    config_digest = _digest(
        config_sha256 or release.environment_config_sha256,
        "config_sha256",
    )
    runtime_digest = _digest(
        release.runtime_dependencies_sha256,
        "runtime_dependencies_sha256",
    )
    policy_digest = _digest(release.policy_sha256, "policy_sha256")
    if config_digest != release.environment_config_sha256:
        raise RuntimeLineageError("runtime config digest does not match the release")
    eligibility_status, eligibility_reason = _eligibility(
        release,
        environment,
        consumer_kind=consumer_kind,
        consumer_id=consumer_id,
        allow_break_glass=allow_break_glass,
    )
    if strict_execution and eligibility_status != "eligible":
        raise RuntimeLineageError(
            f"runtime lineage is not eligible for strict execution: {eligibility_reason}"
        )
    existing = session.execute(
        select(CaliberWorkspaceRuntimeLineage).where(
            CaliberWorkspaceRuntimeLineage.consumer_kind == consumer_kind,
            CaliberWorkspaceRuntimeLineage.consumer_id == consumer_id,
        )
    ).scalar_one_or_none()
    values = {
        "project_id": project_id,
        "workspace_release_id": workspace_release_id,
        "revision_id": revision_id,
        "environment_id": environment_id,
        "model_id": model_id,
        "config_sha256": config_digest,
        "runtime_dependencies_sha256": runtime_digest,
        "policy_sha256": policy_digest,
        "eligibility_status": eligibility_status,
        "eligibility_reason": eligibility_reason,
        "strict_execution": strict_execution,
    }
    if existing is not None:
        if any(getattr(existing, key) != value for key, value in values.items()):
            raise RuntimeLineageError("runtime lineage consumer is already bound differently")
        return existing
    lineage = CaliberWorkspaceRuntimeLineage(
        lineage_id=new_workspace_runtime_lineage_id(),
        consumer_kind=consumer_kind,
        consumer_id=consumer_id,
        created_by=created_by,
        **values,
    )
    session.add(lineage)
    session.flush()
    return lineage


def reconstruct_runtime_lineage(
    session: Session,
    lineage_id: str,
    *,
    project_id: str | None = None,
) -> RuntimeLineageSnapshot:
    """Reconstruct execution provenance with one joined database query."""

    statement = (
        select(
            CaliberWorkspaceRuntimeLineage,
            CaliberWorkspaceRelease,
            CaliberWorkspaceRevision,
            CaliberWorkspaceEnvironment,
        )
        .join(
            CaliberWorkspaceRelease,
            CaliberWorkspaceRelease.release_id
            == CaliberWorkspaceRuntimeLineage.workspace_release_id,
        )
        .join(
            CaliberWorkspaceRevision,
            CaliberWorkspaceRevision.revision_id == CaliberWorkspaceRuntimeLineage.revision_id,
        )
        .join(
            CaliberWorkspaceEnvironment,
            CaliberWorkspaceEnvironment.environment_id
            == CaliberWorkspaceRuntimeLineage.environment_id,
        )
        .where(CaliberWorkspaceRuntimeLineage.lineage_id == lineage_id)
    )
    if project_id is not None:
        statement = statement.where(CaliberWorkspaceRuntimeLineage.project_id == project_id)
    row = session.execute(statement).one_or_none()
    if row is None:
        raise RuntimeLineageError("runtime lineage not found")
    lineage, release, revision, environment = row
    return RuntimeLineageSnapshot(lineage, release, revision, environment)


def record_workspace_release_evidence(
    session: Session,
    *,
    workspace_release_id: str,
    kind: str,
    evidence_ref: str,
    evidence_sha256: str,
    recorded_by: str,
    required: bool = True,
    evidence_id: str | None = None,
) -> CaliberWorkspaceReleaseEvidence:
    """Persist release evidence together with its immutable runtime lineage."""

    release = session.get(CaliberWorkspaceRelease, workspace_release_id)
    if release is None:
        raise RuntimeLineageError("workspace release not found")
    digest = _digest(evidence_sha256, "evidence_sha256")
    existing = session.execute(
        select(CaliberWorkspaceReleaseEvidence).where(
            CaliberWorkspaceReleaseEvidence.workspace_release_id == workspace_release_id,
            CaliberWorkspaceReleaseEvidence.kind == kind,
            CaliberWorkspaceReleaseEvidence.evidence_ref == evidence_ref,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.evidence_sha256 != digest:
            raise RuntimeLineageError("release evidence reference is already bound differently")
        if existing.runtime_lineage_id is None:
            lineage = create_runtime_lineage(
                session,
                project_id=release.project_id,
                workspace_release_id=release.release_id,
                revision_id=release.revision_id,
                environment_id=release.environment_id,
                consumer_kind="evidence",
                consumer_id=existing.evidence_id,
                config_sha256=release.environment_config_sha256,
                strict_execution=False,
                created_by=recorded_by,
            )
            existing.runtime_lineage_id = lineage.lineage_id
            session.flush()
        return existing
    evidence = CaliberWorkspaceReleaseEvidence(
        evidence_id=evidence_id or new_workspace_release_evidence_id(),
        workspace_release_id=workspace_release_id,
        kind=kind,
        evidence_ref=evidence_ref,
        evidence_sha256=digest,
        required=required,
        recorded_by=recorded_by,
    )
    session.add(evidence)
    session.flush()
    lineage = create_runtime_lineage(
        session,
        project_id=release.project_id,
        workspace_release_id=release.release_id,
        revision_id=release.revision_id,
        environment_id=release.environment_id,
        consumer_kind="evidence",
        consumer_id=evidence.evidence_id,
        config_sha256=release.environment_config_sha256,
        strict_execution=False,
        created_by=recorded_by,
    )
    evidence.runtime_lineage_id = lineage.lineage_id
    session.flush()
    return evidence


def require_runtime_lineage(  # noqa: PLR0912
    session: Session,
    lineage_id: str | None,
    *,
    consumer_kind: str,
    consumer_id: str,
    require_current_release: bool = True,
) -> RuntimeLineageSnapshot:
    """Fail closed before strict execution when coordinates are absent/stale."""

    if not lineage_id:
        raise MissingRuntimeLineageError(
            f"strict {consumer_kind} execution requires runtime lineage"
        )
    snapshot = reconstruct_runtime_lineage(session, lineage_id)
    row = snapshot.lineage
    if row.consumer_kind != consumer_kind or row.consumer_id != consumer_id:
        raise RuntimeLineageError("runtime lineage consumer binding does not match execution")
    if row.eligibility_status != "eligible":
        raise RuntimeLineageError(
            f"runtime lineage is not eligible: {row.eligibility_reason or row.eligibility_status}"
        )
    if snapshot.release.revision_id != snapshot.revision.revision_id:
        raise RuntimeLineageError("runtime release revision is no longer consistent")
    if snapshot.release.environment_id != snapshot.environment.environment_id:
        raise RuntimeLineageError("runtime release environment is no longer consistent")
    if snapshot.revision.status != "ready":
        raise RuntimeLineageError("runtime revision is no longer ready")
    if snapshot.release.status != "approved":
        break_glass_operation = (
            session.get(CaliberWorkspaceReleaseOperation, consumer_id)
            if consumer_kind == "provider_operation"
            else None
        )
        if (
            break_glass_operation is None
            or break_glass_operation.break_glass_authorization_id is None
            or snapshot.release.status not in {"awaiting_quality_signoff", "awaiting_approval"}
        ):
            raise RuntimeLineageError("runtime release is no longer approved")
    if snapshot.environment.status != "active":
        raise RuntimeLineageError("runtime environment is not active")
    if consumer_kind == "run" and require_current_release:
        if snapshot.environment.current_release_id != snapshot.release.release_id:
            raise RuntimeLineageError("runtime release is not current in its environment")
        if snapshot.environment.operation_state != "idle":
            raise RuntimeLineageError("runtime environment has an unsettled provider operation")
    if (
        consumer_kind == "provider_operation"
        and snapshot.environment.pending_operation_id != consumer_id
    ):
        raise RuntimeLineageError("provider operation no longer owns the environment lock")
    return snapshot


__all__ = [
    "MissingRuntimeLineageError",
    "RuntimeLineageError",
    "RuntimeLineageSnapshot",
    "create_runtime_lineage",
    "reconstruct_runtime_lineage",
    "record_workspace_release_evidence",
    "require_runtime_lineage",
]
