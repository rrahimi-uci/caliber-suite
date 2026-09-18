"""Security decisions for the provider-free Workspace release foundation.

P5-B deliberately stops at durable governance.  It binds human decisions and
exceptional recovery intents to the immutable release coordinates, but it does
not invoke a provider or move an environment pointer.  P5-C owns those effects
and must revalidate the authorization immediately before starting one.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.audit import record as audit_record
from caliber.auth import SCOPE_ADMIN, SCOPE_APPROVER, SCOPE_OPERATOR, CaliberIdentity
from caliber.db.models import (
    CaliberProject,
    CaliberWorkspaceBreakGlassAuthorization,
    CaliberWorkspaceChangeRequest,
    CaliberWorkspaceChangeRequestHead,
    CaliberWorkspaceEnvironment,
    CaliberWorkspaceRelease,
    CaliberWorkspaceReleaseDecision,
    CaliberWorkspaceReleaseOperation,
    CaliberWorkspaceRevision,
)
from caliber.ids import (
    new_workspace_break_glass_authorization_id,
    new_workspace_release_decision_id,
)
from caliber.resource_access import (
    POLICY_VERSION,
    ROLE_OWNER,
    ROLE_REVIEWER,
    authorize,
)
from caliber.workspace_release_operation_service import create_workspace_release_operation
from caliber.workspace_release_service import (
    RELEASE_APPROVED,
    RELEASE_AWAITING_APPROVAL,
    RELEASE_AWAITING_QUALITY_SIGNOFF,
    RELEASE_REJECTED,
    transition_workspace_release,
)

DECISION_QUALITY = "quality"
DECISION_RELEASE = "release"
DECISION_GO = "go"
DECISION_NO_GO = "no_go"

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_BREAK_GLASS_TTL = timedelta(hours=1)
_REQUIRED_QA_SCOPES = frozenset({SCOPE_OPERATOR, SCOPE_APPROVER})
_MAX_REASON_LENGTH = 4000
_MAX_REFERENCE_LENGTH = 256


class WorkspaceReleaseGovernanceError(RuntimeError):
    """A release decision or recovery authorization is not eligible."""


class WorkspaceReleaseDecisionConflictError(WorkspaceReleaseGovernanceError):
    """A decision already exists or the release changed concurrently."""


class WorkspaceReleaseAuthorizationError(WorkspaceReleaseGovernanceError):
    """The caller lacks the role, scope, credential, or provenance to decide."""


class WorkspaceBreakGlassError(WorkspaceReleaseGovernanceError):
    """An exceptional production authorization is refused."""


@dataclass(frozen=True)
class WorkspaceBreakGlassApplyResult:
    """The two durable rows created by one atomic break-glass request."""

    authorization: CaliberWorkspaceBreakGlassAuthorization
    operation: CaliberWorkspaceReleaseOperation


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _db_time(value: datetime) -> datetime:
    return value.replace(tzinfo=None)


def _digest(value: str, name: str) -> str:
    if not _DIGEST_RE.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _release_context(
    session: Session, project_id: str, release_id: str
) -> tuple[
    CaliberProject, CaliberWorkspaceRelease, CaliberWorkspaceEnvironment, CaliberWorkspaceRevision
]:
    project = session.get(CaliberProject, project_id)
    release = session.get(CaliberWorkspaceRelease, release_id)
    if project is None or release is None or release.project_id != project_id:
        raise WorkspaceReleaseGovernanceError("workspace release not found")
    environment = session.get(CaliberWorkspaceEnvironment, release.environment_id)
    revision = session.get(CaliberWorkspaceRevision, release.revision_id)
    if environment is None or environment.project_id != project_id:
        raise WorkspaceReleaseGovernanceError("workspace release environment not found")
    if revision is None or revision.project_id != project_id or revision.status != "ready":
        raise WorkspaceReleaseGovernanceError("workspace release revision is not ready")
    return project, release, environment, revision


def _authorize_decision(
    session: Session,
    identity: CaliberIdentity,
    project_id: str,
    action: str,
    *,
    expected_role: str,
) -> None:
    decision = authorize(session, identity, action, project_id)
    if not decision.allowed or decision.role != expected_role:
        raise WorkspaceReleaseAuthorizationError("caller lacks the required workspace role")
    if action == "release.quality_signoff" and not identity.scopes >= _REQUIRED_QA_SCOPES:
        raise WorkspaceReleaseAuthorizationError(
            "quality signoff requires caliber.operator and caliber.approver"
        )


def _change_request_context(
    session: Session,
    release: CaliberWorkspaceRelease,
    revision: CaliberWorkspaceRevision,
) -> tuple[CaliberWorkspaceChangeRequest, CaliberWorkspaceChangeRequestHead]:
    if release.change_request_id is None or release.change_request_head_id is None:
        raise WorkspaceReleaseDecisionConflictError(
            "technical review binding is required for a governed decision"
        )
    request = session.get(CaliberWorkspaceChangeRequest, release.change_request_id)
    head = session.get(CaliberWorkspaceChangeRequestHead, release.change_request_head_id)
    if (
        request is None
        or head is None
        or request.project_id != release.project_id
        or head.change_request_id != request.change_request_id
        or head.revision_id != release.revision_id
        or head.revision_sha256 != revision.revision_sha256
        or request.current_head_revision_id != revision.revision_id
    ):
        raise WorkspaceReleaseDecisionConflictError(
            "release change-request head does not bind the release revision"
        )
    return request, head


def _originators(
    release: CaliberWorkspaceRelease,
    revision: CaliberWorkspaceRevision,
    request: CaliberWorkspaceChangeRequest,
    head: CaliberWorkspaceChangeRequestHead,
) -> frozenset[str]:
    return frozenset(
        actor
        for actor in (
            revision.created_by,
            release.requested_by,
            request.created_by,
            head.changed_by,
        )
        if actor
    )


def _require_distinct_actor(identity: CaliberIdentity, originators: frozenset[str]) -> None:
    if identity.user_id in originators:
        raise WorkspaceReleaseAuthorizationError(
            "release decision maker cannot be a change originator or requester"
        )


def _require_gate_binding(release: CaliberWorkspaceRelease, gate_evidence_sha256: str) -> str:
    gate = _digest(gate_evidence_sha256, "gate_evidence_sha256")
    if release.evaluation_evidence_sha256 is None:
        raise WorkspaceReleaseDecisionConflictError("machine gate evidence is missing")
    if gate != release.evaluation_evidence_sha256:
        raise WorkspaceReleaseDecisionConflictError("gate evidence digest does not bind release")
    return gate


def _decision_set_digest(
    session: Session,
    release_id: str,
) -> str:
    rows = session.execute(
        select(CaliberWorkspaceReleaseDecision)
        .where(CaliberWorkspaceReleaseDecision.workspace_release_id == release_id)
        .order_by(
            CaliberWorkspaceReleaseDecision.kind,
            CaliberWorkspaceReleaseDecision.decision_id,
        )
    ).scalars()
    canonical = [
        {
            "kind": row.kind,
            "decision": row.decision,
            "decided_by": row.decided_by,
            "change_request_head_id": row.change_request_head_id,
            "actor_role_snapshot": row.actor_role_snapshot,
            "effective_scope_snapshot": row.effective_scope_snapshot,
            "revision_sha256": row.revision_sha256,
            "environment_config_sha256": row.environment_config_sha256,
            "runtime_dependencies_sha256": row.runtime_dependencies_sha256,
            "gate_evidence_sha256": row.gate_evidence_sha256,
            "policy_sha256": row.policy_sha256,
        }
        for row in rows
    ]
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def record_workspace_release_decision(  # noqa: PLR0912
    session: Session,
    *,
    project_id: str,
    workspace_release_id: str,
    identity: CaliberIdentity,
    kind: Literal["quality", "release"],
    decision: Literal["go", "no_go"],
    rationale: str = "",
    gate_evidence_sha256: str,
    change_request_head_id: str | None = None,
    decision_id: str | None = None,
) -> CaliberWorkspaceReleaseDecision:
    """Append one role-specific decision and advance the release by CAS."""

    if kind not in {DECISION_QUALITY, DECISION_RELEASE}:
        raise ValueError("kind must be quality or release")
    if decision not in {DECISION_GO, DECISION_NO_GO}:
        raise ValueError("decision must be go or no_go")
    if len(rationale) > _MAX_REASON_LENGTH:
        raise ValueError(f"rationale exceeds {_MAX_REASON_LENGTH} characters")
    project, release, environment, revision = _release_context(
        session, project_id, workspace_release_id
    )
    _require_gate_binding(release, gate_evidence_sha256)
    existing = session.execute(
        select(CaliberWorkspaceReleaseDecision).where(
            CaliberWorkspaceReleaseDecision.workspace_release_id == release.release_id,
            CaliberWorkspaceReleaseDecision.kind == kind,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise WorkspaceReleaseDecisionConflictError("release decision already exists")
    if kind == DECISION_QUALITY:
        _authorize_decision(
            session,
            identity,
            project.project_id,
            "release.quality_signoff",
            expected_role=ROLE_REVIEWER,
        )
        if environment.environment_class not in {"qa", "production"}:
            raise WorkspaceReleaseDecisionConflictError(
                "quality decisions are only valid for QA or production releases"
            )
        if release.status != RELEASE_AWAITING_QUALITY_SIGNOFF:
            raise WorkspaceReleaseDecisionConflictError(
                "release is not awaiting a quality decision"
            )
        request, head = _change_request_context(session, release, revision)
        if change_request_head_id != head.head_id:
            raise WorkspaceReleaseDecisionConflictError("quality decision head is stale")
        expected_statuses = (
            {"technically_approved", "qa_in_progress", "accepted"}
            if environment.environment_class == "qa"
            else {"accepted"}
        )
        if request.status not in expected_statuses:
            raise WorkspaceReleaseDecisionConflictError(
                "change request has not completed the required technical review"
            )
        _require_distinct_actor(identity, _originators(release, revision, request, head))
        target_status = (
            RELEASE_APPROVED
            if environment.environment_class == "qa" and decision == DECISION_GO
            else RELEASE_AWAITING_APPROVAL
            if environment.environment_class == "production" and decision == DECISION_GO
            else RELEASE_REJECTED
        )
        stored_head_id: str | None = head.head_id
    else:
        _authorize_decision(
            session,
            identity,
            project.project_id,
            "release.approve",
            expected_role=ROLE_OWNER,
        )
        if environment.environment_class != "production":
            raise WorkspaceReleaseDecisionConflictError(
                "final release approval is only valid for production"
            )
        if release.status != RELEASE_AWAITING_APPROVAL:
            raise WorkspaceReleaseDecisionConflictError("release is not awaiting final approval")
        request, head = _change_request_context(session, release, revision)
        _require_distinct_actor(identity, _originators(release, revision, request, head))
        prior_quality = session.execute(
            select(CaliberWorkspaceReleaseDecision).where(
                CaliberWorkspaceReleaseDecision.workspace_release_id == release.release_id,
                CaliberWorkspaceReleaseDecision.kind == DECISION_QUALITY,
            )
        ).scalar_one_or_none()
        if (
            prior_quality is None
            or prior_quality.decision != DECISION_GO
            or prior_quality.gate_evidence_sha256 != release.evaluation_evidence_sha256
            or prior_quality.revision_sha256 != revision.revision_sha256
            or prior_quality.environment_config_sha256 != release.environment_config_sha256
            or prior_quality.runtime_dependencies_sha256 != release.runtime_dependencies_sha256
            or prior_quality.policy_sha256 != release.policy_sha256
        ):
            raise WorkspaceReleaseDecisionConflictError(
                "fresh production quality signoff is required before final approval"
            )
        target_status = RELEASE_APPROVED if decision == DECISION_GO else RELEASE_REJECTED
        stored_head_id = None

    # The append-only decision and its release CAS are one durable unit. A
    # caller may catch a stale-lock or uniqueness error, so a savepoint keeps
    # an uncommitted decision from surviving independently of its transition.
    with session.begin_nested():
        row = CaliberWorkspaceReleaseDecision(
            decision_id=decision_id or new_workspace_release_decision_id(),
            workspace_release_id=release.release_id,
            kind=kind,
            decision=decision,
            rationale=rationale,
            decided_by=identity.user_id,
            actor_role_snapshot={"role": ROLE_REVIEWER if kind == DECISION_QUALITY else ROLE_OWNER},
            effective_scope_snapshot={
                "scopes": sorted(identity.scopes),
                "policy_version": POLICY_VERSION,
            },
            change_request_head_id=stored_head_id,
            revision_sha256=revision.revision_sha256,
            environment_config_sha256=release.environment_config_sha256,
            runtime_dependencies_sha256=release.runtime_dependencies_sha256,
            gate_evidence_sha256=_digest(gate_evidence_sha256, "gate_evidence_sha256"),
            policy_sha256=release.policy_sha256,
        )
        session.add(row)
        session.flush()
        digest = _decision_set_digest(session, release.release_id)
        transition_workspace_release(
            session,
            release,
            target_status,
            actor=identity.user_id,
            expected_lock_version=release.lock_version,
            evaluation_evidence_sha256=release.evaluation_evidence_sha256,
            decision_set_sha256=digest,
        )
        audit_record(
            session,
            actor=identity.user_id,
            action="workspace_release_decision",
            entity_type="workspace_release",
            entity_id=release.release_id,
            environment_id=environment.environment_id,
            details={
                "decision_id": row.decision_id,
                "kind": kind,
                "decision": decision,
                "revision_sha256": revision.revision_sha256,
                "gate_evidence_sha256": row.gate_evidence_sha256,
            },
        )
    return row


def _require_break_glass_preconditions(
    session: Session,
    release: CaliberWorkspaceRelease,
    revision: CaliberWorkspaceRevision,
) -> tuple[CaliberWorkspaceChangeRequest, CaliberWorkspaceChangeRequestHead]:
    request, head = _change_request_context(session, release, revision)
    if request.status != "accepted":
        raise WorkspaceBreakGlassError("QA acceptance is required before break-glass apply")
    if release.predecessor_release_id is None:
        raise WorkspaceBreakGlassError("staging verification is required before break-glass apply")
    predecessor = session.get(CaliberWorkspaceRelease, release.predecessor_release_id)
    if predecessor is None or predecessor.project_id != release.project_id:
        raise WorkspaceBreakGlassError("staging predecessor is not valid")
    predecessor_environment = session.get(CaliberWorkspaceEnvironment, predecessor.environment_id)
    if (
        predecessor.revision_id != release.revision_id
        or predecessor_environment is None
        or predecessor_environment.environment_class != "staging"
        or predecessor_environment.name != "staging"
    ):
        raise WorkspaceBreakGlassError("staging predecessor coordinates are invalid")
    if predecessor.predecessor_release_id is None:
        raise WorkspaceBreakGlassError(
            "QA release verification is required before break-glass apply"
        )
    qa_release = session.get(CaliberWorkspaceRelease, predecessor.predecessor_release_id)
    qa_environment = session.execute(
        select(CaliberWorkspaceEnvironment).where(
            CaliberWorkspaceEnvironment.project_id == release.project_id,
            CaliberWorkspaceEnvironment.name == "qa",
            CaliberWorkspaceEnvironment.environment_class == "qa",
        )
    ).scalar_one_or_none()
    if (
        qa_release is None
        or qa_environment is None
        or qa_release.project_id != release.project_id
        or qa_release.revision_id != release.revision_id
        or qa_release.environment_id != qa_environment.environment_id
        or qa_release.status != RELEASE_APPROVED
    ):
        raise WorkspaceBreakGlassError(
            "QA release verification is required before break-glass apply"
        )
    qa_quality = session.execute(
        select(CaliberWorkspaceReleaseDecision).where(
            CaliberWorkspaceReleaseDecision.workspace_release_id == qa_release.release_id,
            CaliberWorkspaceReleaseDecision.kind == DECISION_QUALITY,
            CaliberWorkspaceReleaseDecision.decision == DECISION_GO,
            CaliberWorkspaceReleaseDecision.change_request_head_id == head.head_id,
            CaliberWorkspaceReleaseDecision.revision_sha256 == revision.revision_sha256,
            CaliberWorkspaceReleaseDecision.environment_config_sha256
            == qa_release.environment_config_sha256,
            CaliberWorkspaceReleaseDecision.runtime_dependencies_sha256
            == qa_release.runtime_dependencies_sha256,
            CaliberWorkspaceReleaseDecision.gate_evidence_sha256
            == qa_release.evaluation_evidence_sha256,
            CaliberWorkspaceReleaseDecision.policy_sha256 == qa_release.policy_sha256,
        )
    ).scalar_one_or_none()
    if qa_quality is None:
        raise WorkspaceBreakGlassError("QA quality signoff is required before break-glass apply")
    qa_applied = session.execute(
        select(CaliberWorkspaceReleaseOperation.operation_id).where(
            CaliberWorkspaceReleaseOperation.project_id == release.project_id,
            CaliberWorkspaceReleaseOperation.workspace_release_id == qa_release.release_id,
            CaliberWorkspaceReleaseOperation.environment_id == qa_environment.environment_id,
            CaliberWorkspaceReleaseOperation.kind == "apply",
            CaliberWorkspaceReleaseOperation.status == "applied",
        )
    ).first()
    if qa_applied is None:
        raise WorkspaceBreakGlassError(
            "QA release verification is required before break-glass apply"
        )
    staged = session.execute(
        select(CaliberWorkspaceReleaseOperation.operation_id).where(
            CaliberWorkspaceReleaseOperation.project_id == release.project_id,
            CaliberWorkspaceReleaseOperation.workspace_release_id == predecessor.release_id,
            CaliberWorkspaceReleaseOperation.environment_id == predecessor.environment_id,
            CaliberWorkspaceReleaseOperation.kind == "apply",
            CaliberWorkspaceReleaseOperation.status == "applied",
        )
    ).first()
    if staged is None:
        raise WorkspaceBreakGlassError("staging verification is required before break-glass apply")
    return request, head


def create_workspace_break_glass_apply(  # noqa: PLR0912
    session: Session,
    *,
    project_id: str,
    workspace_release_id: str,
    identity: CaliberIdentity,
    reason: str,
    incident_ref: str,
    authorization_ref: str,
    expires_at: datetime,
    gate_evidence_sha256: str,
    expected_current_release_id: str,
    expected_environment_lock_version: int,
    idempotency_key: str,
    machine_gates_passed: bool,
    integrity_checks_passed: bool,
    authorization_id: str | None = None,
    operation_id: str | None = None,
    now: datetime | None = None,
) -> WorkspaceBreakGlassApplyResult:
    """Create one expiring production recovery authorization and operation.

    The explicit gate booleans represent the provider-neutral P5-B contract.
    P5-C must replace/revalidate them from persisted machine and integrity
    evidence immediately before the first external effect.
    """

    if not reason.strip() or len(reason) > _MAX_REASON_LENGTH:
        raise ValueError(f"reason must be between 1 and {_MAX_REASON_LENGTH} characters")
    if not incident_ref.strip() or len(incident_ref) > _MAX_REFERENCE_LENGTH:
        raise ValueError(f"incident_ref must be between 1 and {_MAX_REFERENCE_LENGTH} characters")
    if not authorization_ref.strip() or len(authorization_ref) > _MAX_REFERENCE_LENGTH:
        raise ValueError(
            f"authorization_ref must be between 1 and {_MAX_REFERENCE_LENGTH} characters"
        )
    if expected_environment_lock_version < 1:
        raise ValueError("expected_environment_lock_version must be positive")
    if not idempotency_key.strip():
        raise ValueError("idempotency_key must not be empty")
    if not machine_gates_passed or not integrity_checks_passed:
        raise WorkspaceBreakGlassError("machine and integrity gates must pass")
    if identity.credential_kind != "session" or not identity.credential_id:
        raise WorkspaceBreakGlassError("break-glass requires an interactive session credential")
    if not identity.has_scope(SCOPE_ADMIN):
        raise WorkspaceReleaseAuthorizationError("break-glass requires caliber.admin")

    _project, release, environment, revision = _release_context(
        session, project_id, workspace_release_id
    )
    if environment.name != "prod" or environment.environment_class != "production":
        raise WorkspaceBreakGlassError("break-glass is restricted to the production environment")
    if environment.status != "active" or not environment.recovery_policy_enabled:
        raise WorkspaceBreakGlassError("Workspace recovery policy is disabled")
    if release.status not in {RELEASE_AWAITING_QUALITY_SIGNOFF, RELEASE_AWAITING_APPROVAL}:
        raise WorkspaceBreakGlassError("release is not eligible for break-glass apply")
    if expected_current_release_id != release.release_id:
        raise WorkspaceBreakGlassError("expected current release is stale")
    gate = _require_gate_binding(release, gate_evidence_sha256)
    request, head = _require_break_glass_preconditions(session, release, revision)
    _require_distinct_actor(identity, _originators(release, revision, request, head))

    moment = _utc(now) or _now()
    expiry = _utc(expires_at)
    if expiry is None or expiry <= moment or expiry > moment + _MAX_BREAK_GLASS_TTL:
        raise WorkspaceBreakGlassError("break-glass expiry must be in the next hour")

    existing_operation = session.execute(
        select(CaliberWorkspaceReleaseOperation).where(
            CaliberWorkspaceReleaseOperation.project_id == project_id,
            CaliberWorkspaceReleaseOperation.kind == "apply",
            CaliberWorkspaceReleaseOperation.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    if existing_operation is not None:
        if existing_operation.break_glass_authorization_id is None:
            raise WorkspaceBreakGlassError("idempotency key belongs to a normal apply")
        existing_authorization = session.get(
            CaliberWorkspaceBreakGlassAuthorization,
            existing_operation.break_glass_authorization_id,
        )
        if existing_authorization is None:
            raise WorkspaceBreakGlassError("break-glass authorization is missing")
        if (
            existing_operation.workspace_release_id != release.release_id
            or existing_operation.environment_id != environment.environment_id
            or existing_operation.expected_environment_lock_version
            != expected_environment_lock_version
            or existing_authorization.reason != reason
            or existing_authorization.incident_ref != incident_ref
            or existing_authorization.authorization_ref != authorization_ref
        ):
            raise WorkspaceBreakGlassError("break-glass idempotency conflict")
        return WorkspaceBreakGlassApplyResult(existing_authorization, existing_operation)

    authorization = CaliberWorkspaceBreakGlassAuthorization(
        authorization_id=authorization_id or new_workspace_break_glass_authorization_id(),
        project_id=project_id,
        workspace_release_id=release.release_id,
        environment_id=environment.environment_id,
        reason=reason.strip(),
        incident_ref=incident_ref.strip(),
        authorization_ref=authorization_ref.strip(),
        authorized_by=identity.user_id,
        credential_kind=identity.credential_kind,
        credential_id=identity.credential_id,
        revision_sha256=revision.revision_sha256,
        environment_config_sha256=release.environment_config_sha256,
        runtime_dependencies_sha256=release.runtime_dependencies_sha256,
        gate_evidence_sha256=gate,
        policy_sha256=release.policy_sha256,
        expires_at=_db_time(expiry),
    )
    # A savepoint ensures an operation conflict cannot leave an authorization
    # row pending in the caller's transaction.  Both rows and the audit entry
    # become visible together when the caller commits the outer transaction.
    with session.begin_nested():
        session.add(authorization)
        session.flush()
        operation = create_workspace_release_operation(
            session,
            project_id=project_id,
            workspace_release_id=release.release_id,
            environment_id=environment.environment_id,
            kind="apply",
            idempotency_key=idempotency_key,
            expected_environment_lock_version=expected_environment_lock_version,
            expected_current_release_id=expected_current_release_id,
            requested_by=identity.user_id,
            break_glass_authorization_id=authorization.authorization_id,
            operation_id=operation_id,
        )
        audit_record(
            session,
            actor=identity.user_id,
            action="workspace_release_break_glass_apply",
            entity_type="workspace_release",
            entity_id=release.release_id,
            environment_id=environment.environment_id,
            severity="high",
            details={
                "authorization_id": authorization.authorization_id,
                "operation_id": operation.operation_id,
                "authorization_ref": authorization.authorization_ref,
                "incident_ref": authorization.incident_ref,
                "expires_at": expiry.isoformat(),
                "revision_sha256": revision.revision_sha256,
                "gate_evidence_sha256": gate,
            },
        )
    return WorkspaceBreakGlassApplyResult(authorization, operation)


__all__ = [
    "DECISION_GO",
    "DECISION_NO_GO",
    "DECISION_QUALITY",
    "DECISION_RELEASE",
    "WorkspaceBreakGlassApplyResult",
    "WorkspaceBreakGlassError",
    "WorkspaceReleaseAuthorizationError",
    "WorkspaceReleaseDecisionConflictError",
    "WorkspaceReleaseGovernanceError",
    "create_workspace_break_glass_apply",
    "record_workspace_release_decision",
]
