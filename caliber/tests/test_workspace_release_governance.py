"""P5-B tests for release decisions, actor separation, and break-glass policy."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.auth import SCOPE_ADMIN, SCOPE_APPROVER, SCOPE_OPERATOR, CaliberIdentity
from caliber.db.models import (
    CaliberAuditLog,
    CaliberProject,
    CaliberProjectMember,
    CaliberReworkTask,
    CaliberWorkspaceBreakGlassAuthorization,
    CaliberWorkspaceChangeRequest,
    CaliberWorkspaceChangeRequestHead,
    CaliberWorkspaceEnvironment,
    CaliberWorkspaceRelease,
    CaliberWorkspaceReleaseDecision,
    CaliberWorkspaceRevision,
)
from caliber.ids import new_workspace_release_operation_id
from caliber.workspace_release_governance import (
    WorkspaceBreakGlassError,
    WorkspaceReleaseAuthorizationError,
    WorkspaceReleaseDecisionConflictError,
    WorkspaceReleaseGovernanceError,
    create_workspace_break_glass_apply,
    record_workspace_release_decision,
)
from caliber.workspace_release_governance import (
    _utc as governance_utc,
)
from caliber.workspace_release_operation_service import (
    OPERATION_APPLIED,
    OPERATION_APPLYING,
    WorkspaceReleaseOperationConflictError,
    create_workspace_release_operation,
    transition_workspace_release_operation,
)
from caliber.workspace_release_operation_service import (
    _utc as operation_utc,
)
from caliber.workspace_release_service import (
    RELEASE_APPROVED,
    RELEASE_AWAITING_APPROVAL,
    RELEASE_AWAITING_QUALITY_SIGNOFF,
    RELEASE_REJECTED,
    create_workspace_release,
)

PROJECT_ID = "PRJ-p5b"
REVISION_ID = "WSR-p5b"
HEAD_ID = "WSCRH-p5b"
CR_ID = "WSCR-p5b"
HEX = "a" * 64
GATE = "b" * 64
ALL_ADMIN_SCOPES = frozenset({SCOPE_ADMIN, SCOPE_APPROVER, SCOPE_OPERATOR})


def _identity(
    user_id: str,
    *,
    scopes: frozenset[str] = ALL_ADMIN_SCOPES,
    credential_kind: str | None = "session",
    credential_id: str | None = "session-p5b",
) -> CaliberIdentity:
    return CaliberIdentity(
        user_id=user_id,
        scopes=scopes,
        credential_kind=credential_kind,
        credential_id=credential_id,
    )


def _seed(
    session: Session,
    *,
    request_status: str = "technically_approved",
    recovery_policy_enabled: bool = False,
    revision_created_by: str = "developer",
    revision_status: str = "ready",
) -> None:
    session.add(
        CaliberProject(project_id=PROJECT_ID, name="P5-B workspace", owner="workspace-admin")
    )
    session.add_all(
        [
            CaliberProjectMember(
                member_id="MEM-p5b-qa",
                project_id=PROJECT_ID,
                user_id="qa",
                role="reviewer",
                status="active",
                created_by="workspace-admin",
            ),
            CaliberProjectMember(
                member_id="MEM-p5b-admin",
                project_id=PROJECT_ID,
                user_id="workspace-admin",
                role="owner",
                status="active",
                created_by="workspace-admin",
            ),
        ]
    )
    session.add_all(
        [
            CaliberWorkspaceEnvironment(
                environment_id="WSE-p5b-qa",
                project_id=PROJECT_ID,
                name="qa",
                environment_class="qa",
                promotion_order=20,
                status="active",
                created_by="workspace-admin",
            ),
            CaliberWorkspaceEnvironment(
                environment_id="WSE-p5b-staging",
                project_id=PROJECT_ID,
                name="staging",
                environment_class="staging",
                promotion_order=30,
                status="active",
                created_by="workspace-admin",
            ),
            CaliberWorkspaceEnvironment(
                environment_id="WSE-p5b-prod",
                project_id=PROJECT_ID,
                name="prod",
                environment_class="production",
                promotion_order=40,
                status="active",
                recovery_policy_enabled=recovery_policy_enabled,
                created_by="workspace-admin",
            ),
        ]
    )
    session.add(
        CaliberWorkspaceRevision(
            revision_id=REVISION_ID,
            project_id=PROJECT_ID,
            revision_number=1,
            manifest={"apiVersion": "caliber/v1alpha1"},
            manifest_sha256=HEX,
            source_bundle_sha256=HEX,
            source_attestation="caller_attested",
            revision_sha256=HEX,
            status=revision_status,
            created_by=revision_created_by,
        )
    )
    session.add(
        CaliberWorkspaceChangeRequest(
            change_request_id=CR_ID,
            project_id=PROJECT_ID,
            current_head_revision_id=REVISION_ID,
            created_by="developer",
            title="P5-B candidate",
            status=request_status,
            review_backend="caliber",
        )
    )
    session.add(
        CaliberWorkspaceChangeRequestHead(
            head_id=HEAD_ID,
            change_request_id=CR_ID,
            generation=1,
            revision_id=REVISION_ID,
            revision_sha256=HEX,
            review_policy_version="v1",
            review_policy_sha256=HEX,
            changed_by="developer",
        )
    )
    session.flush()


def _release(
    session: Session,
    *,
    environment_id: str = "WSE-p5b-qa",
    key: str = "release-p5b",
    predecessor_release_id: str | None = None,
) -> CaliberWorkspaceRelease:
    release = create_workspace_release(
        session,
        project_id=PROJECT_ID,
        revision_id=REVISION_ID,
        environment_id=environment_id,
        environment_config_sha256=HEX,
        runtime_dependencies_sha256=HEX,
        policy_sha256=HEX,
        request_idempotency_key=key,
        requested_by="developer",
        change_request_id=CR_ID,
        change_request_head_id=HEAD_ID,
        predecessor_release_id=predecessor_release_id,
    )
    release.status = RELEASE_AWAITING_QUALITY_SIGNOFF
    release.evaluation_evidence_sha256 = GATE
    session.flush()
    return release


def test_qa_quality_go_is_head_and_digest_bound_and_audited(db_session: Session) -> None:
    _seed(db_session)
    release = _release(db_session)

    decision = record_workspace_release_decision(
        db_session,
        project_id=PROJECT_ID,
        workspace_release_id=release.release_id,
        identity=_identity("qa"),
        kind="quality",
        decision="go",
        rationale="QA passed",
        gate_evidence_sha256=GATE,
        change_request_head_id=HEAD_ID,
    )

    assert decision.change_request_head_id == HEAD_ID
    assert decision.decided_by == "qa"
    assert decision.actor_role_snapshot == {"role": "reviewer"}
    assert decision.effective_scope_snapshot["scopes"] == sorted(ALL_ADMIN_SCOPES)
    assert release.status == RELEASE_APPROVED
    audit = db_session.execute(
        select(CaliberAuditLog).where(CaliberAuditLog.entity_id == release.release_id)
    ).scalar_one()
    assert audit.severity == "standard"
    assert audit.environment_id == "WSE-p5b-qa"


def test_production_quality_then_admin_final_decision_use_distinct_roles(
    db_session: Session,
) -> None:
    _seed(db_session, request_status="accepted")
    release = _release(db_session, environment_id="WSE-p5b-prod")

    quality = record_workspace_release_decision(
        db_session,
        project_id=PROJECT_ID,
        workspace_release_id=release.release_id,
        identity=_identity("qa"),
        kind="quality",
        decision="go",
        gate_evidence_sha256=GATE,
        change_request_head_id=HEAD_ID,
    )
    final = record_workspace_release_decision(
        db_session,
        project_id=PROJECT_ID,
        workspace_release_id=release.release_id,
        identity=_identity("workspace-admin"),
        kind="release",
        decision="go",
        gate_evidence_sha256=GATE,
    )

    assert quality.decision == "go"
    assert final.change_request_head_id is None
    assert release.status == RELEASE_APPROVED
    assert release.decision_set_sha256 is not None
    assert (
        len(
            db_session.execute(
                select(CaliberWorkspaceReleaseDecision).where(
                    CaliberWorkspaceReleaseDecision.workspace_release_id == release.release_id
                )
            )
            .scalars()
            .all()
        )
        == 2
    )


def test_decision_and_release_transition_roll_back_together(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(db_session)
    release = _release(db_session)

    def reject_transition(*args: object, **kwargs: object) -> None:
        raise WorkspaceReleaseDecisionConflictError("simulated stale release")

    monkeypatch.setattr(
        "caliber.workspace_release_governance.transition_workspace_release",
        reject_transition,
    )
    with pytest.raises(WorkspaceReleaseDecisionConflictError, match="simulated"):
        record_workspace_release_decision(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            identity=_identity("qa"),
            kind="quality",
            decision="go",
            gate_evidence_sha256=GATE,
            change_request_head_id=HEAD_ID,
        )
    assert (
        db_session.execute(
            select(CaliberWorkspaceReleaseDecision).where(
                CaliberWorkspaceReleaseDecision.workspace_release_id == release.release_id
            )
        ).scalar_one_or_none()
        is None
    )


def test_decisions_reject_stale_digest_head_and_duplicate_rows(db_session: Session) -> None:
    _seed(db_session)
    release = _release(db_session)
    with pytest.raises(WorkspaceReleaseDecisionConflictError, match="digest"):
        record_workspace_release_decision(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            identity=_identity("qa"),
            kind="quality",
            decision="go",
            gate_evidence_sha256=HEX,
            change_request_head_id=HEAD_ID,
        )
    with pytest.raises(WorkspaceReleaseDecisionConflictError, match="stale"):
        record_workspace_release_decision(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            identity=_identity("qa"),
            kind="quality",
            decision="go",
            gate_evidence_sha256=GATE,
            change_request_head_id="WSCRH-other",
        )
    record_workspace_release_decision(
        db_session,
        project_id=PROJECT_ID,
        workspace_release_id=release.release_id,
        identity=_identity("qa"),
        kind="quality",
        decision="no_go",
        gate_evidence_sha256=GATE,
        change_request_head_id=HEAD_ID,
    )
    with pytest.raises(WorkspaceReleaseDecisionConflictError, match="already exists"):
        record_workspace_release_decision(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            identity=_identity("qa"),
            kind="quality",
            decision="go",
            gate_evidence_sha256=GATE,
            change_request_head_id=HEAD_ID,
        )


def test_role_scope_and_originator_separation_are_enforced(db_session: Session) -> None:
    _seed(db_session, revision_created_by="qa")
    release = _release(db_session)
    with pytest.raises(WorkspaceReleaseAuthorizationError, match="workspace role"):
        record_workspace_release_decision(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            identity=_identity("developer"),
            kind="quality",
            decision="go",
            gate_evidence_sha256=GATE,
            change_request_head_id=HEAD_ID,
        )
    with pytest.raises(WorkspaceReleaseAuthorizationError, match="operator"):
        record_workspace_release_decision(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            identity=_identity("qa", scopes=frozenset({SCOPE_APPROVER})),
            kind="quality",
            decision="go",
            gate_evidence_sha256=GATE,
            change_request_head_id=HEAD_ID,
        )
    with pytest.raises(WorkspaceReleaseAuthorizationError, match="originator"):
        record_workspace_release_decision(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            identity=_identity("qa"),
            kind="quality",
            decision="go",
            gate_evidence_sha256=GATE,
            change_request_head_id=HEAD_ID,
        )


def test_admin_final_approval_requires_fresh_production_quality(db_session: Session) -> None:
    _seed(db_session, request_status="accepted")
    release = _release(db_session, environment_id="WSE-p5b-prod")
    release.status = RELEASE_AWAITING_APPROVAL
    with pytest.raises(WorkspaceReleaseDecisionConflictError, match="quality signoff"):
        record_workspace_release_decision(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            identity=_identity("workspace-admin"),
            kind="release",
            decision="go",
            gate_evidence_sha256=GATE,
        )


def test_admin_cannot_finally_approve_a_release_they_authored(db_session: Session) -> None:
    _seed(db_session, request_status="accepted", revision_created_by="workspace-admin")
    release = _release(db_session, environment_id="WSE-p5b-prod")
    record_workspace_release_decision(
        db_session,
        project_id=PROJECT_ID,
        workspace_release_id=release.release_id,
        identity=_identity("qa"),
        kind="quality",
        decision="go",
        gate_evidence_sha256=GATE,
        change_request_head_id=HEAD_ID,
    )
    with pytest.raises(WorkspaceReleaseAuthorizationError, match="originator"):
        record_workspace_release_decision(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            identity=_identity("workspace-admin"),
            kind="release",
            decision="go",
            gate_evidence_sha256=GATE,
        )


@pytest.mark.parametrize(
    ("kind", "decision", "rationale", "message"),
    [
        ("bad", "go", "", "kind"),
        ("quality", "bad", "", "decision"),
        ("quality", "go", "x" * 4001, "rationale"),
    ],
)
def test_decision_input_contract_is_strict(
    db_session: Session,
    kind: str,
    decision: str,
    rationale: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        record_workspace_release_decision(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id="WSREL-missing",
            identity=_identity("qa"),
            kind=kind,  # type: ignore[arg-type]
            decision=decision,  # type: ignore[arg-type]
            rationale=rationale,
            gate_evidence_sha256=GATE,
            change_request_head_id=HEAD_ID,
        )


def test_decision_context_and_state_checks_fail_closed(db_session: Session) -> None:
    with pytest.raises(WorkspaceReleaseGovernanceError, match="not found"):
        record_workspace_release_decision(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id="WSREL-missing",
            identity=_identity("qa"),
            kind="quality",
            decision="go",
            gate_evidence_sha256=GATE,
            change_request_head_id=HEAD_ID,
        )
    _seed(db_session)
    release = _release(db_session)
    release.change_request_id = None
    release.change_request_head_id = None
    with pytest.raises(WorkspaceReleaseDecisionConflictError, match="technical review"):
        record_workspace_release_decision(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            identity=_identity("qa"),
            kind="quality",
            decision="go",
            gate_evidence_sha256=GATE,
            change_request_head_id=HEAD_ID,
        )
    release.change_request_id = CR_ID
    release.change_request_head_id = HEAD_ID
    with pytest.raises(ValueError, match="SHA-256"):
        record_workspace_release_decision(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            identity=_identity("qa"),
            kind="quality",
            decision="go",
            gate_evidence_sha256="invalid",
            change_request_head_id=HEAD_ID,
        )
    db_session.get(CaliberWorkspaceEnvironment, "WSE-p5b-qa").project_id = "PRJ-other"
    with pytest.raises(WorkspaceReleaseGovernanceError, match="environment"):
        record_workspace_release_decision(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            identity=_identity("qa"),
            kind="quality",
            decision="go",
            gate_evidence_sha256=GATE,
            change_request_head_id=HEAD_ID,
        )


def test_revision_head_gate_and_environment_state_are_checked(db_session: Session) -> None:
    _seed(db_session, revision_status="validating")
    release = _release(db_session)
    with pytest.raises(WorkspaceReleaseGovernanceError, match="revision"):
        record_workspace_release_decision(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            identity=_identity("qa"),
            kind="quality",
            decision="go",
            gate_evidence_sha256=GATE,
            change_request_head_id=HEAD_ID,
        )
    db_session.rollback()
    _seed(db_session)
    release = _release(db_session)
    release.evaluation_evidence_sha256 = None
    with pytest.raises(WorkspaceReleaseDecisionConflictError, match="missing"):
        record_workspace_release_decision(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            identity=_identity("qa"),
            kind="quality",
            decision="go",
            gate_evidence_sha256=GATE,
            change_request_head_id=HEAD_ID,
        )


def test_quality_decision_rejects_wrong_environment_status_and_review_state(
    db_session: Session,
) -> None:
    _seed(db_session)
    release = _release(db_session)
    release.status = RELEASE_APPROVED
    with pytest.raises(WorkspaceReleaseDecisionConflictError, match="awaiting"):
        record_workspace_release_decision(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            identity=_identity("qa"),
            kind="quality",
            decision="go",
            gate_evidence_sha256=GATE,
            change_request_head_id=HEAD_ID,
        )
    db_session.rollback()
    _seed(db_session)
    db_session.add(
        CaliberWorkspaceEnvironment(
            environment_id="WSE-p5b-dev",
            project_id=PROJECT_ID,
            name="dev",
            environment_class="development",
            promotion_order=10,
            status="active",
            created_by="workspace-admin",
        )
    )
    db_session.flush()
    release = _release(db_session, environment_id="WSE-p5b-dev")
    with pytest.raises(WorkspaceReleaseDecisionConflictError, match="QA or production"):
        record_workspace_release_decision(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            identity=_identity("qa"),
            kind="quality",
            decision="go",
            gate_evidence_sha256=GATE,
            change_request_head_id=HEAD_ID,
        )
    db_session.rollback()
    _seed(db_session, request_status="draft")
    release = _release(db_session)
    with pytest.raises(WorkspaceReleaseDecisionConflictError, match="technical review"):
        record_workspace_release_decision(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            identity=_identity("qa"),
            kind="quality",
            decision="go",
            gate_evidence_sha256=GATE,
            change_request_head_id=HEAD_ID,
        )


def test_release_decision_rejects_nonproduction_and_invalid_state(db_session: Session) -> None:
    _seed(db_session, request_status="accepted")
    release = _release(db_session)
    with pytest.raises(WorkspaceReleaseDecisionConflictError, match="production"):
        record_workspace_release_decision(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            identity=_identity("workspace-admin"),
            kind="release",
            decision="go",
            gate_evidence_sha256=GATE,
        )
    db_session.rollback()
    _seed(db_session, request_status="accepted")
    release = _release(db_session, environment_id="WSE-p5b-prod")
    release.status = RELEASE_APPROVED
    with pytest.raises(WorkspaceReleaseDecisionConflictError, match="awaiting"):
        record_workspace_release_decision(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            identity=_identity("workspace-admin"),
            kind="release",
            decision="go",
            gate_evidence_sha256=GATE,
        )


def test_production_quality_requires_exact_head_and_technical_acceptance(
    db_session: Session,
) -> None:
    _seed(db_session, request_status="accepted")
    release = _release(db_session, environment_id="WSE-p5b-prod")
    head = db_session.get(CaliberWorkspaceChangeRequestHead, HEAD_ID)
    head.revision_sha256 = GATE
    with pytest.raises(WorkspaceReleaseDecisionConflictError, match="does not bind"):
        record_workspace_release_decision(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            identity=_identity("qa"),
            kind="quality",
            decision="go",
            gate_evidence_sha256=GATE,
            change_request_head_id=HEAD_ID,
        )


def _seed_break_glass_candidate(
    session: Session,
    *,
    recovery_policy_enabled: bool = True,
    request_status: str = "accepted",
    stage_operation: bool = True,
) -> CaliberWorkspaceRelease:
    _seed(session, request_status=request_status, recovery_policy_enabled=recovery_policy_enabled)
    qa = _release(session, environment_id="WSE-p5b-qa", key="qa-release")
    if request_status == "accepted":
        record_workspace_release_decision(
            session,
            project_id=PROJECT_ID,
            workspace_release_id=qa.release_id,
            identity=_identity("qa"),
            kind="quality",
            decision="go",
            gate_evidence_sha256=GATE,
            change_request_head_id=HEAD_ID,
        )
        qa_apply = create_workspace_release_operation(
            session,
            project_id=PROJECT_ID,
            workspace_release_id=qa.release_id,
            environment_id="WSE-p5b-qa",
            kind="apply",
            idempotency_key="qa-apply",
            expected_environment_lock_version=1,
            requested_by="workspace-admin",
        )
        transition_workspace_release_operation(
            session,
            qa_apply,
            OPERATION_APPLYING,
            actor="workspace-admin",
            expected_lock_version=1,
        )
        transition_workspace_release_operation(
            session,
            qa_apply,
            OPERATION_APPLIED,
            actor="workspace-admin",
            expected_lock_version=2,
        )
    staging = _release(
        session,
        environment_id="WSE-p5b-staging",
        key="staging-release",
        predecessor_release_id=qa.release_id,
    )
    staging.status = RELEASE_APPROVED
    session.flush()
    if not stage_operation:
        return _release(
            session,
            environment_id="WSE-p5b-prod",
            key="prod-release",
            predecessor_release_id=staging.release_id,
        )
    staged_operation = create_workspace_release_operation(
        session,
        project_id=PROJECT_ID,
        workspace_release_id=staging.release_id,
        environment_id="WSE-p5b-staging",
        kind="apply",
        idempotency_key="staging-apply",
        expected_environment_lock_version=1,
        requested_by="workspace-admin",
        operation_id=new_workspace_release_operation_id(),
    )
    transition_workspace_release_operation(
        session,
        staged_operation,
        OPERATION_APPLYING,
        actor="workspace-admin",
        expected_lock_version=1,
    )
    transition_workspace_release_operation(
        session,
        staged_operation,
        OPERATION_APPLIED,
        actor="workspace-admin",
        expected_lock_version=2,
    )
    return _release(
        session,
        environment_id="WSE-p5b-prod",
        key="prod-release",
        predecessor_release_id=staging.release_id,
    )


def test_break_glass_is_disabled_by_default_and_rejects_noninteractive_credentials(
    db_session: Session,
) -> None:
    _seed_break_glass_candidate(db_session, recovery_policy_enabled=False)
    release = db_session.execute(
        select(CaliberWorkspaceRelease).where(
            CaliberWorkspaceRelease.environment_id == "WSE-p5b-prod"
        )
    ).scalar_one()
    expires = datetime.now(timezone.utc) + timedelta(minutes=10)
    with pytest.raises(WorkspaceBreakGlassError, match="disabled"):
        create_workspace_break_glass_apply(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            identity=_identity("ops"),
            reason="production incident",
            incident_ref="INC-1",
            authorization_ref="AUTH-1",
            expires_at=expires,
            gate_evidence_sha256=GATE,
            expected_current_release_id=release.release_id,
            expected_environment_lock_version=1,
            idempotency_key="breakglass-1",
            machine_gates_passed=True,
            integrity_checks_passed=True,
        )

    db_session.rollback()
    _seed_break_glass_candidate(db_session)
    release = db_session.execute(
        select(CaliberWorkspaceRelease).where(
            CaliberWorkspaceRelease.environment_id == "WSE-p5b-prod"
        )
    ).scalar_one()
    with pytest.raises(WorkspaceBreakGlassError, match="interactive"):
        create_workspace_break_glass_apply(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            identity=_identity("ops-admin", credential_kind="pat", credential_id="pat-1"),
            reason="production incident",
            incident_ref="INC-PAT",
            authorization_ref="AUTH-PAT",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            gate_evidence_sha256=GATE,
            expected_current_release_id=release.release_id,
            expected_environment_lock_version=1,
            idempotency_key="breakglass-pat",
            machine_gates_passed=True,
            integrity_checks_passed=True,
        )
    with pytest.raises(WorkspaceReleaseAuthorizationError, match="caliber.admin"):
        create_workspace_break_glass_apply(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            identity=_identity("ops-admin", scopes=frozenset({SCOPE_OPERATOR, SCOPE_APPROVER})),
            reason="production incident",
            incident_ref="INC-NOADMIN",
            authorization_ref="AUTH-NOADMIN",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            gate_evidence_sha256=GATE,
            expected_current_release_id=release.release_id,
            expected_environment_lock_version=1,
            idempotency_key="breakglass-noadmin",
            machine_gates_passed=True,
            integrity_checks_passed=True,
        )


def test_break_glass_requires_admin_session_and_all_gates(db_session: Session) -> None:
    _seed_break_glass_candidate(db_session)
    release = db_session.execute(
        select(CaliberWorkspaceRelease).where(
            CaliberWorkspaceRelease.environment_id == "WSE-p5b-prod"
        )
    ).scalar_one()
    expires = datetime.now(timezone.utc) + timedelta(minutes=10)
    for identity in (
        _identity("ops", scopes=frozenset({SCOPE_APPROVER})),
        _identity("ops", credential_kind="trusted_header", credential_id="proxy"),
    ):
        with pytest.raises((WorkspaceReleaseAuthorizationError, WorkspaceBreakGlassError)):
            create_workspace_break_glass_apply(
                db_session,
                project_id=PROJECT_ID,
                workspace_release_id=release.release_id,
                identity=identity,
                reason="production incident",
                incident_ref="INC-1",
                authorization_ref="AUTH-1",
                expires_at=expires,
                gate_evidence_sha256=GATE,
                expected_current_release_id=release.release_id,
                expected_environment_lock_version=1,
                idempotency_key="breakglass-gates",
                machine_gates_passed=False,
                integrity_checks_passed=True,
            )


def test_valid_break_glass_is_atomic_single_use_and_high_severity_audited(
    db_session: Session,
) -> None:
    release = _seed_break_glass_candidate(db_session)
    expires = datetime.now(timezone.utc) + timedelta(minutes=10)
    result = create_workspace_break_glass_apply(
        db_session,
        project_id=PROJECT_ID,
        workspace_release_id=release.release_id,
        identity=_identity("ops-admin"),
        reason="production incident",
        incident_ref="INC-42",
        authorization_ref="AUTH-42",
        expires_at=expires,
        gate_evidence_sha256=GATE,
        expected_current_release_id=release.release_id,
        expected_environment_lock_version=1,
        idempotency_key="breakglass-valid",
        machine_gates_passed=True,
        integrity_checks_passed=True,
    )
    assert result.operation.break_glass_authorization_id == result.authorization.authorization_id
    assert release.status == RELEASE_AWAITING_QUALITY_SIGNOFF
    audit = db_session.execute(
        select(CaliberAuditLog).where(
            CaliberAuditLog.action == "workspace_release_break_glass_apply"
        )
    ).scalar_one()
    assert audit.severity == "high"
    assert audit.details["incident_ref"] == "INC-42"

    replay = create_workspace_break_glass_apply(
        db_session,
        project_id=PROJECT_ID,
        workspace_release_id=release.release_id,
        identity=_identity("ops-admin"),
        reason="production incident",
        incident_ref="INC-42",
        authorization_ref="AUTH-42",
        expires_at=expires,
        gate_evidence_sha256=GATE,
        expected_current_release_id=release.release_id,
        expected_environment_lock_version=1,
        idempotency_key="breakglass-valid",
        machine_gates_passed=True,
        integrity_checks_passed=True,
    )
    assert replay.operation.operation_id == result.operation.operation_id
    assert replay.authorization.authorization_id == result.authorization.authorization_id
    with pytest.raises(WorkspaceBreakGlassError, match="idempotency conflict"):
        create_workspace_break_glass_apply(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            identity=_identity("ops-admin"),
            reason="different incident",
            incident_ref="INC-42",
            authorization_ref="AUTH-42",
            expires_at=expires,
            gate_evidence_sha256=GATE,
            expected_current_release_id=release.release_id,
            expected_environment_lock_version=1,
            idempotency_key="breakglass-valid",
            machine_gates_passed=True,
            integrity_checks_passed=True,
        )


def test_operation_service_validates_break_glass_authorization_defensively(
    db_session: Session,
) -> None:
    release = _seed_break_glass_candidate(db_session)

    def direct(key: str, *, kind: str = "apply") -> None:
        create_workspace_release_operation(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            environment_id="WSE-p5b-prod",
            kind=kind,
            idempotency_key=key,
            expected_environment_lock_version=1,
            expected_current_release_id=release.release_id,
            target_release_id="WSREL-prior" if kind == "rollback" else None,
            requested_by="ops-admin",
            break_glass_authorization_id="WSBGA-missing",
        )

    with pytest.raises(WorkspaceReleaseOperationConflictError, match="not found"):
        direct("missing-auth")
    with pytest.raises(WorkspaceReleaseOperationConflictError, match="only create"):
        direct("rollback-auth", kind="rollback")

    result = create_workspace_break_glass_apply(
        db_session,
        project_id=PROJECT_ID,
        workspace_release_id=release.release_id,
        identity=_identity("ops-admin"),
        reason="incident",
        incident_ref="INC-DEFENSE",
        authorization_ref="AUTH-DEFENSE",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        gate_evidence_sha256=GATE,
        expected_current_release_id=release.release_id,
        expected_environment_lock_version=1,
        idempotency_key="breakglass-defense",
        machine_gates_passed=True,
        integrity_checks_passed=True,
    )
    auth = result.authorization
    release.status = RELEASE_APPROVED
    with pytest.raises(WorkspaceReleaseOperationConflictError, match="awaiting"):
        db_session.flush()
        create_workspace_release_operation(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            environment_id="WSE-p5b-prod",
            kind="apply",
            idempotency_key="invalid-status",
            expected_environment_lock_version=1,
            expected_current_release_id=release.release_id,
            requested_by="ops-admin",
            break_glass_authorization_id=auth.authorization_id,
        )
    release.status = RELEASE_AWAITING_QUALITY_SIGNOFF
    auth.project_id = "PRJ-other"
    with pytest.raises(WorkspaceReleaseOperationConflictError, match="coordinates"):
        create_workspace_release_operation(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            environment_id="WSE-p5b-prod",
            kind="apply",
            idempotency_key="bad-coordinates",
            expected_environment_lock_version=1,
            expected_current_release_id=release.release_id,
            requested_by="ops-admin",
            break_glass_authorization_id=auth.authorization_id,
        )
    auth.project_id = PROJECT_ID
    auth.credential_id = None
    with pytest.raises(WorkspaceReleaseOperationConflictError, match="coordinates"):
        create_workspace_release_operation(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            environment_id="WSE-p5b-prod",
            kind="apply",
            idempotency_key="missing-credential-id",
            expected_environment_lock_version=1,
            expected_current_release_id=release.release_id,
            requested_by="ops-admin",
            break_glass_authorization_id=auth.authorization_id,
        )
    auth.credential_id = "session-p5b"
    auth.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    with pytest.raises(WorkspaceReleaseOperationConflictError, match="expired"):
        create_workspace_release_operation(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            environment_id="WSE-p5b-prod",
            kind="apply",
            idempotency_key="expired-auth",
            expected_environment_lock_version=1,
            expected_current_release_id=release.release_id,
            requested_by="ops-admin",
            break_glass_authorization_id=auth.authorization_id,
        )
    auth.expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    auth.revision_sha256 = GATE
    with pytest.raises(WorkspaceReleaseOperationConflictError, match="revision"):
        create_workspace_release_operation(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            environment_id="WSE-p5b-prod",
            kind="apply",
            idempotency_key="bad-revision",
            expected_environment_lock_version=1,
            expected_current_release_id=release.release_id,
            requested_by="ops-admin",
            break_glass_authorization_id=auth.authorization_id,
        )
    auth.revision_sha256 = HEX
    auth.gate_evidence_sha256 = HEX
    with pytest.raises(WorkspaceReleaseOperationConflictError, match="digest"):
        create_workspace_release_operation(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            environment_id="WSE-p5b-prod",
            kind="apply",
            idempotency_key="bad-digest",
            expected_environment_lock_version=1,
            expected_current_release_id=release.release_id,
            requested_by="ops-admin",
            break_glass_authorization_id=auth.authorization_id,
        )
    assert operation_utc(datetime(2026, 1, 1)) is not None


def test_break_glass_requires_qa_acceptance_and_staging_verification(db_session: Session) -> None:
    release = _seed_break_glass_candidate(db_session, request_status="technically_approved")
    expires = datetime.now(timezone.utc) + timedelta(minutes=10)
    with pytest.raises(WorkspaceBreakGlassError, match="QA acceptance"):
        create_workspace_break_glass_apply(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            identity=_identity("ops-admin"),
            reason="incident",
            incident_ref="INC-QA",
            authorization_ref="AUTH-QA",
            expires_at=expires,
            gate_evidence_sha256=GATE,
            expected_current_release_id=release.release_id,
            expected_environment_lock_version=1,
            idempotency_key="breakglass-qa",
            machine_gates_passed=True,
            integrity_checks_passed=True,
        )
    db_session.rollback()
    release = _seed_break_glass_candidate(db_session, stage_operation=False)
    with pytest.raises(WorkspaceBreakGlassError, match="staging verification"):
        create_workspace_break_glass_apply(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            identity=_identity("ops-admin"),
            reason="incident",
            incident_ref="INC-STAGE",
            authorization_ref="AUTH-STAGE",
            expires_at=expires,
            gate_evidence_sha256=GATE,
            expected_current_release_id=release.release_id,
            expected_environment_lock_version=1,
            idempotency_key="breakglass-stage",
            machine_gates_passed=True,
            integrity_checks_passed=True,
        )

    db_session.rollback()
    _seed(db_session, request_status="accepted", recovery_policy_enabled=True)
    release = _release(db_session, environment_id="WSE-p5b-prod")
    with pytest.raises(WorkspaceBreakGlassError, match="staging verification"):
        create_workspace_break_glass_apply(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            identity=_identity("ops-admin"),
            reason="incident",
            incident_ref="INC-NO-PREDECESSOR",
            authorization_ref="AUTH-NO-PREDECESSOR",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            gate_evidence_sha256=GATE,
            expected_current_release_id=release.release_id,
            expected_environment_lock_version=1,
            idempotency_key="breakglass-no-predecessor",
            machine_gates_passed=True,
            integrity_checks_passed=True,
        )

    db_session.rollback()
    _seed(db_session, request_status="accepted", recovery_policy_enabled=True)
    release = _release(
        db_session,
        environment_id="WSE-p5b-prod",
        predecessor_release_id="WSREL-missing",
    )
    with pytest.raises(WorkspaceBreakGlassError, match="not valid"):
        create_workspace_break_glass_apply(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            identity=_identity("ops-admin"),
            reason="incident",
            incident_ref="INC-MISSING",
            authorization_ref="AUTH-MISSING",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            gate_evidence_sha256=GATE,
            expected_current_release_id=release.release_id,
            expected_environment_lock_version=1,
            idempotency_key="breakglass-missing",
            machine_gates_passed=True,
            integrity_checks_passed=True,
        )


def test_break_glass_rechecks_production_coordinates_status_and_expiry(
    db_session: Session,
) -> None:
    release = _seed_break_glass_candidate(db_session)
    prod = db_session.get(CaliberWorkspaceEnvironment, "WSE-p5b-prod")
    prod.environment_class = "qa"
    with pytest.raises(WorkspaceBreakGlassError, match="production environment"):
        create_workspace_break_glass_apply(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            identity=_identity("ops-admin"),
            reason="incident",
            incident_ref="INC-COORD",
            authorization_ref="AUTH-COORD",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            gate_evidence_sha256=GATE,
            expected_current_release_id=release.release_id,
            expected_environment_lock_version=1,
            idempotency_key="breakglass-coord",
            machine_gates_passed=True,
            integrity_checks_passed=True,
        )
    db_session.rollback()
    _seed_break_glass_candidate(db_session)
    release = db_session.execute(
        select(CaliberWorkspaceRelease).where(
            CaliberWorkspaceRelease.environment_id == "WSE-p5b-prod"
        )
    ).scalar_one()
    release.status = RELEASE_APPROVED
    with pytest.raises(WorkspaceBreakGlassError, match="not eligible"):
        create_workspace_break_glass_apply(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            identity=_identity("ops-admin"),
            reason="incident",
            incident_ref="INC-STATUS",
            authorization_ref="AUTH-STATUS",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            gate_evidence_sha256=GATE,
            expected_current_release_id=release.release_id,
            expected_environment_lock_version=1,
            idempotency_key="breakglass-status",
            machine_gates_passed=True,
            integrity_checks_passed=True,
        )
    release.status = RELEASE_AWAITING_QUALITY_SIGNOFF
    with pytest.raises(WorkspaceBreakGlassError, match="expiry"):
        create_workspace_break_glass_apply(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            identity=_identity("ops-admin"),
            reason="incident",
            incident_ref="INC-EXPIRY",
            authorization_ref="AUTH-EXPIRY",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
            gate_evidence_sha256=GATE,
            expected_current_release_id=release.release_id,
            expected_environment_lock_version=1,
            idempotency_key="breakglass-expiry",
            machine_gates_passed=True,
            integrity_checks_passed=True,
        )

    db_session.rollback()
    _seed_break_glass_candidate(db_session)
    release = db_session.execute(
        select(CaliberWorkspaceRelease).where(
            CaliberWorkspaceRelease.environment_id == "WSE-p5b-prod"
        )
    ).scalar_one()
    staging = db_session.get(CaliberWorkspaceEnvironment, "WSE-p5b-staging")
    staging.environment_class = "qa"
    with pytest.raises(WorkspaceBreakGlassError, match="coordinates"):
        create_workspace_break_glass_apply(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            identity=_identity("ops-admin"),
            reason="incident",
            incident_ref="INC-STAGE-COORD",
            authorization_ref="AUTH-STAGE-COORD",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            gate_evidence_sha256=GATE,
            expected_current_release_id=release.release_id,
            expected_environment_lock_version=1,
            idempotency_key="breakglass-stage-coord",
            machine_gates_passed=True,
            integrity_checks_passed=True,
        )


def test_break_glass_input_and_idempotency_conflicts_are_rejected(db_session: Session) -> None:
    release = _seed_break_glass_candidate(db_session)
    base = {
        "session": db_session,
        "project_id": PROJECT_ID,
        "workspace_release_id": release.release_id,
        "identity": _identity("ops-admin"),
        "reason": "incident",
        "incident_ref": "INC-INPUT",
        "authorization_ref": "AUTH-INPUT",
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
        "gate_evidence_sha256": GATE,
        "expected_current_release_id": release.release_id,
        "expected_environment_lock_version": 1,
        "idempotency_key": "breakglass-input",
        "machine_gates_passed": True,
        "integrity_checks_passed": True,
    }
    for key, value, message in (
        ("reason", " ", "reason"),
        ("incident_ref", " ", "incident_ref"),
        ("authorization_ref", " ", "authorization_ref"),
        ("expected_environment_lock_version", 0, "positive"),
        ("idempotency_key", " ", "idempotency_key"),
    ):
        with pytest.raises(ValueError, match=message):
            create_workspace_break_glass_apply(**{**base, key: value})
    with pytest.raises(WorkspaceBreakGlassError, match="stale"):
        create_workspace_break_glass_apply(**{**base, "expected_current_release_id": "WSREL-old"})
    with pytest.raises(WorkspaceBreakGlassError, match="machine"):
        create_workspace_break_glass_apply(**{**base, "machine_gates_passed": False})


def test_break_glass_idempotency_conflicts_with_normal_apply_and_changed_input(
    db_session: Session,
) -> None:
    release = _seed_break_glass_candidate(db_session)
    release.status = RELEASE_APPROVED
    normal = create_workspace_release_operation(
        db_session,
        project_id=PROJECT_ID,
        workspace_release_id=release.release_id,
        environment_id="WSE-p5b-prod",
        kind="apply",
        idempotency_key="shared-key",
        expected_environment_lock_version=1,
        requested_by="workspace-admin",
    )
    assert normal.break_glass_authorization_id is None
    release.status = RELEASE_AWAITING_QUALITY_SIGNOFF
    with pytest.raises(WorkspaceBreakGlassError, match="normal apply"):
        create_workspace_break_glass_apply(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            identity=_identity("ops-admin"),
            reason="incident",
            incident_ref="INC-SHARED",
            authorization_ref="AUTH-SHARED",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            gate_evidence_sha256=GATE,
            expected_current_release_id=release.release_id,
            expected_environment_lock_version=1,
            idempotency_key="shared-key",
            machine_gates_passed=True,
            integrity_checks_passed=True,
        )


def test_break_glass_replay_fails_closed_if_authorization_row_is_missing(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = _seed_break_glass_candidate(db_session)
    result = create_workspace_break_glass_apply(
        db_session,
        project_id=PROJECT_ID,
        workspace_release_id=release.release_id,
        identity=_identity("ops-admin"),
        reason="incident",
        incident_ref="INC-MISSING-AUTH",
        authorization_ref="AUTH-MISSING-AUTH",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        gate_evidence_sha256=GATE,
        expected_current_release_id=release.release_id,
        expected_environment_lock_version=1,
        idempotency_key="breakglass-missing-auth",
        machine_gates_passed=True,
        integrity_checks_passed=True,
    )
    real_get = db_session.get

    def missing_authorization(model: object, key: object, *args: object, **kwargs: object):
        if model is CaliberWorkspaceBreakGlassAuthorization:
            return None
        return real_get(model, key, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(db_session, "get", missing_authorization)
    with pytest.raises(WorkspaceBreakGlassError, match="missing"):
        create_workspace_break_glass_apply(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            identity=_identity("ops-admin"),
            reason="incident",
            incident_ref="INC-MISSING-AUTH",
            authorization_ref="AUTH-MISSING-AUTH",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            gate_evidence_sha256=GATE,
            expected_current_release_id=release.release_id,
            expected_environment_lock_version=1,
            idempotency_key="breakglass-missing-auth",
            machine_gates_passed=True,
            integrity_checks_passed=True,
        )
    assert result.operation.operation_id
    assert governance_utc(datetime(2026, 1, 1)) is not None


def test_operation_service_rejects_missing_environment(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = _seed_break_glass_candidate(db_session)
    real_get = db_session.get

    def missing_environment(model: object, key: object, *args: object, **kwargs: object):
        if model is CaliberWorkspaceEnvironment and key == "WSE-p5b-prod":
            return None
        return real_get(model, key, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(db_session, "get", missing_environment)
    with pytest.raises(WorkspaceReleaseOperationConflictError, match="environment"):
        create_workspace_release_operation(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            environment_id="WSE-p5b-prod",
            kind="apply",
            idempotency_key="missing-environment",
            expected_environment_lock_version=1,
            requested_by="ops-admin",
        )


# ---------------------------------------------------------------------------
# release_no_go rework tasks (P3-A release FK)
# ---------------------------------------------------------------------------
#
# record_workspace_release_decision computes ``target_status =
# RELEASE_REJECTED`` from two different governance branches -- a quality
# no_go (any environment) and a final production release no_go -- and both
# reach one shared creation site
# (``_create_release_rework_task``) rather than duplicating it per branch.
# A release is rejected at most once (REJECTED is terminal), so exactly one
# task is ever created per release; ``uq_rework_task_workspace_release``
# backs that at the DB level too (see ``test_models.py``).


def _rework_task_for_release(session: Session, release_id: str) -> CaliberReworkTask:
    return session.execute(
        select(CaliberReworkTask).where(CaliberReworkTask.workspace_release_id == release_id)
    ).scalar_one()


def test_qa_quality_no_go_creates_a_release_no_go_rework_task(db_session: Session) -> None:
    _seed(db_session)
    release = _release(db_session)

    decision = record_workspace_release_decision(
        db_session,
        project_id=PROJECT_ID,
        workspace_release_id=release.release_id,
        identity=_identity("qa"),
        kind="quality",
        decision="no_go",
        rationale="factual accuracy regressed",
        gate_evidence_sha256=GATE,
        change_request_head_id=HEAD_ID,
    )

    assert release.status == RELEASE_REJECTED
    task = _rework_task_for_release(db_session, release.release_id)
    assert task.failure_kind == "release_no_go"
    assert task.job_id is None
    assert task.agent_id is None
    assert task.workspace_release_id == release.release_id
    assert task.project_id == PROJECT_ID
    assert task.reason == "factual accuracy regressed"
    assert task.status == "open"
    assert task.created_by == "qa"
    assert task.gate_evidence == {
        "decision_id": decision.decision_id,
        "kind": "quality",
        "gate_evidence_sha256": GATE,
        "revision_sha256": HEX,
    }
    audit = db_session.execute(
        select(CaliberAuditLog).where(CaliberAuditLog.entity_id == task.task_id)
    ).scalar_one()
    assert audit.action == "create_rework_task"
    assert audit.actor == "qa"
    assert audit.details["workspace_release_id"] == release.release_id
    assert audit.details["failure_kind"] == "release_no_go"


def test_final_release_no_go_creates_a_release_no_go_rework_task(db_session: Session) -> None:
    _seed(db_session, request_status="accepted")
    release = _release(db_session, environment_id="WSE-p5b-prod")

    record_workspace_release_decision(
        db_session,
        project_id=PROJECT_ID,
        workspace_release_id=release.release_id,
        identity=_identity("qa"),
        kind="quality",
        decision="go",
        gate_evidence_sha256=GATE,
        change_request_head_id=HEAD_ID,
    )
    # The quality decision was a "go", so the release is only awaiting final
    # approval -- not yet rejected -- and no task exists yet.
    assert (
        db_session.execute(
            select(CaliberReworkTask).where(
                CaliberReworkTask.workspace_release_id == release.release_id
            )
        ).scalar_one_or_none()
        is None
    )

    record_workspace_release_decision(
        db_session,
        project_id=PROJECT_ID,
        workspace_release_id=release.release_id,
        identity=_identity("workspace-admin"),
        kind="release",
        decision="no_go",
        rationale="regressed in canary",
        gate_evidence_sha256=GATE,
    )

    assert release.status == RELEASE_REJECTED
    task = _rework_task_for_release(db_session, release.release_id)
    assert task.failure_kind == "release_no_go"
    assert task.reason == "regressed in canary"
    assert task.created_by == "workspace-admin"
    assert task.project_id == PROJECT_ID
    assert task.job_id is None
    assert task.agent_id is None


def test_qa_quality_go_does_not_create_a_rework_task(db_session: Session) -> None:
    """A "go" decision never rejects the release, so it never owes rework."""
    _seed(db_session)
    release = _release(db_session)

    record_workspace_release_decision(
        db_session,
        project_id=PROJECT_ID,
        workspace_release_id=release.release_id,
        identity=_identity("qa"),
        kind="quality",
        decision="go",
        gate_evidence_sha256=GATE,
        change_request_head_id=HEAD_ID,
    )

    assert release.status == RELEASE_APPROVED
    assert (
        db_session.execute(
            select(CaliberReworkTask).where(
                CaliberReworkTask.workspace_release_id == release.release_id
            )
        ).scalar_one_or_none()
        is None
    )
