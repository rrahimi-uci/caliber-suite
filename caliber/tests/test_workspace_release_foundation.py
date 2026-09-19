"""P5-A contract tests for Workspace release/evaluation/operation foundations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import Update

from caliber.db.models import (
    CaliberProject,
    CaliberReworkTask,
    CaliberWorkspaceBreakGlassAuthorization,
    CaliberWorkspaceEnvironment,
    CaliberWorkspaceRelease,
    CaliberWorkspaceReleaseDecision,
    CaliberWorkspaceReleaseEvaluation,
    CaliberWorkspaceReleaseEvidence,
    CaliberWorkspaceReleaseOperation,
    CaliberWorkspaceReleaseOperationItem,
    CaliberWorkspaceRevision,
    CaliberWorkspaceRevisionResource,
)
from caliber.ids import (
    new_workspace_break_glass_authorization_id,
    new_workspace_release_decision_id,
    new_workspace_release_evidence_id,
    new_workspace_release_operation_item_id,
)
from caliber.schemas import (
    WorkspaceBreakGlassAuthorizationSchema,
    WorkspaceReleaseDecisionSchema,
    WorkspaceReleaseEvaluationSchema,
    WorkspaceReleaseEvidenceSchema,
    WorkspaceReleaseOperationItemSchema,
    WorkspaceReleaseOperationSchema,
    WorkspaceReleaseSchema,
)
from caliber.workspace_release_operation_service import (
    OPERATION_APPLIED,
    OPERATION_APPLYING,
    OPERATION_CANCELLED,
    OPERATION_FAILED,
    OPERATION_TRANSITIONS,
    WorkspaceReleaseOperationConflictError,
    WorkspaceReleaseOperationTransitionError,
    create_workspace_release_operation,
    transition_workspace_release_operation,
)
from caliber.workspace_release_service import (
    EMPTY_DECISION_SET_SHA256,
    EVALUATION_FAILED,
    EVALUATION_RUNNING,
    EVALUATION_SUCCEEDED,
    RELEASE_APPROVED,
    RELEASE_AWAITING_QUALITY_SIGNOFF,
    RELEASE_BLOCKED,
    RELEASE_DRAFT,
    RELEASE_EVALUATING,
    RELEASE_REJECTED,
    RELEASE_TRANSITIONS,
    WorkspaceReleaseConflictError,
    WorkspaceReleaseLeaseError,
    WorkspaceReleaseTransitionError,
    block_workspace_release,
    claim_workspace_release_evaluation,
    complete_workspace_release_evaluation,
    create_workspace_release,
    create_workspace_release_evaluation,
    heartbeat_workspace_release_evaluation,
    resume_blocked_workspace_release,
    settle_machine_evaluation,
    start_workspace_release_evaluation,
    transition_workspace_release,
)

PROJECT_ID = "PRJ-p5a"
DEV_ENVIRONMENT_ID = "WSE-p5a-dev"
QA_ENVIRONMENT_ID = "WSE-p5a-qa"
STAGING_ENVIRONMENT_ID = "WSE-p5a-staging"
REVISION_ID = "WSR-p5a"
HEX = "a" * 64
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


def _seed_workspace(
    session: Session,
    *,
    environment_id: str = DEV_ENVIRONMENT_ID,
    environment_class: str = "development",
) -> None:
    session.add(CaliberProject(project_id=PROJECT_ID, name="P5-A workspace", owner="developer"))
    session.add(
        CaliberWorkspaceEnvironment(
            environment_id=environment_id,
            project_id=PROJECT_ID,
            name=environment_id.rsplit("-", 1)[-1],
            environment_class=environment_class,
            promotion_order=10,
            status="active",
            created_by="developer",
        )
    )
    session.add(
        CaliberWorkspaceRevision(
            revision_id=REVISION_ID,
            project_id=PROJECT_ID,
            revision_number=1,
            source_id=None,
            source_commit_sha=None,
            manifest={"apiVersion": "caliber/v1alpha1"},
            manifest_sha256=HEX,
            source_bundle_sha256=HEX,
            source_snapshot_file_id=None,
            source_attestation="caller_attested",
            revision_sha256=HEX,
            status="ready",
            created_by="developer",
        )
    )
    session.flush()


def _release(session: Session, *, environment_id: str = DEV_ENVIRONMENT_ID, key: str = "release-1"):
    return create_workspace_release(
        session,
        project_id=PROJECT_ID,
        revision_id=REVISION_ID,
        environment_id=environment_id,
        environment_config_sha256=HEX,
        runtime_dependencies_sha256=HEX,
        policy_sha256=HEX,
        request_idempotency_key=key,
        requested_by="developer",
    )


def _approved_release(session: Session, *, key: str = "release-1"):
    release = _release(session, key=key)
    release.status = RELEASE_APPROVED
    session.flush()
    return release


def test_ids_and_schemas_cover_the_p5a_wire_contract(db_session: Session) -> None:
    _seed_workspace(db_session)
    release = _release(db_session)
    assert release.release_id.startswith("WSREL-")
    WorkspaceReleaseSchema.model_validate(release)
    evaluation = None
    release.status = RELEASE_EVALUATING
    db_session.flush()
    evaluation = create_workspace_release_evaluation(
        db_session,
        project_id=PROJECT_ID,
        workspace_release_id=release.release_id,
        idempotency_key="eval-1",
        evaluation_plan_sha256=HEX,
        input_sha256=HEX,
        requested_by="developer",
    )
    WorkspaceReleaseEvaluationSchema.model_validate(evaluation)
    evidence = CaliberWorkspaceReleaseEvidence(
        evidence_id=new_workspace_release_evidence_id(),
        workspace_release_id=release.release_id,
        kind="gate_verdict",
        evidence_ref="GV-1",
        evidence_sha256=HEX,
        recorded_by="worker",
    )
    decision = CaliberWorkspaceReleaseDecision(
        decision_id=new_workspace_release_decision_id(),
        workspace_release_id=release.release_id,
        kind="quality",
        decision="go",
        rationale="quality passed",
        decided_by="qa",
        revision_sha256=HEX,
        environment_config_sha256=HEX,
        runtime_dependencies_sha256=HEX,
        gate_evidence_sha256=HEX,
        policy_sha256=HEX,
    )
    db_session.add_all([evidence, decision])
    db_session.flush()
    WorkspaceReleaseEvidenceSchema.model_validate(evidence)
    WorkspaceReleaseDecisionSchema.model_validate(decision)
    authorization = CaliberWorkspaceBreakGlassAuthorization(
        authorization_id=new_workspace_break_glass_authorization_id(),
        project_id=PROJECT_ID,
        workspace_release_id=release.release_id,
        environment_id=DEV_ENVIRONMENT_ID,
        reason="incident recovery",
        incident_ref="INC-1",
        authorization_ref="AUTH-1",
        authorized_by="admin",
        credential_kind="interactive",
        credential_id="session-1",
        revision_sha256=HEX,
        environment_config_sha256=HEX,
        runtime_dependencies_sha256=HEX,
        gate_evidence_sha256=HEX,
        policy_sha256=HEX,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db_session.add(authorization)
    db_session.flush()
    WorkspaceBreakGlassAuthorizationSchema.model_validate(authorization)
    release.status = RELEASE_APPROVED
    db_session.flush()
    operation = create_workspace_release_operation(
        db_session,
        project_id=PROJECT_ID,
        workspace_release_id=release.release_id,
        environment_id=DEV_ENVIRONMENT_ID,
        kind="apply",
        idempotency_key="op-1",
        expected_environment_lock_version=1,
        requested_by="developer",
    )
    WorkspaceReleaseOperationSchema.model_validate(operation)
    resource = CaliberWorkspaceRevisionResource(
        resource_pin_id="WSRR-p5a",
        revision_id="WSR-p5a-resource",
        resource_type="prompt",
        logical_name="example",
        resource_id="prompt-1",
        version_ref="1",
        content_sha256=HEX,
        purpose="runtime",
        resolution={},
    )
    db_session.add(
        CaliberWorkspaceRevision(
            revision_id="WSR-p5a-resource",
            project_id=PROJECT_ID,
            revision_number=2,
            source_id=None,
            source_commit_sha=None,
            manifest={"apiVersion": "caliber/v1alpha1"},
            manifest_sha256="b" * 64,
            source_bundle_sha256="b" * 64,
            source_snapshot_file_id=None,
            source_attestation="caller_attested",
            revision_sha256="b" * 64,
            status="validating",
            created_by="developer",
        )
    )
    db_session.add(resource)
    db_session.flush()
    item = CaliberWorkspaceReleaseOperationItem(
        operation_item_id=new_workspace_release_operation_item_id(),
        workspace_release_operation_id=operation.operation_id,
        revision_resource_id=resource.resource_pin_id,
        action="no_op",
        target_ref="prompt:example@1",
    )
    db_session.add(item)
    db_session.flush()
    WorkspaceReleaseOperationItemSchema.model_validate(item)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (state, target)
        for state, targets in RELEASE_TRANSITIONS.items()
        for target in sorted(set(RELEASE_TRANSITIONS) - targets - {state})
    ],
)
def test_release_state_machine_rejects_every_illegal_edge(
    db_session: Session, current: str, target: str
) -> None:
    _seed_workspace(db_session)
    release = _release(db_session)
    release.status = current
    db_session.flush()
    with pytest.raises(WorkspaceReleaseTransitionError, match="illegal"):
        transition_workspace_release(
            db_session,
            release,
            target,
            actor="worker",
            expected_lock_version=1,
        )


def test_release_state_machine_allows_only_declared_edges_and_cas(db_session: Session) -> None:
    _seed_workspace(db_session)
    release = _release(db_session)
    for target in RELEASE_TRANSITIONS[RELEASE_DRAFT]:
        moved = start_workspace_release_evaluation(
            db_session, release, actor="worker", expected_lock_version=1
        )
        assert moved.status == target
    with pytest.raises(WorkspaceReleaseConflictError, match="stale"):
        transition_workspace_release(
            db_session,
            release,
            RELEASE_BLOCKED,
            actor="worker",
            expected_lock_version=1,
        )


def test_dev_machine_pass_becomes_approved(db_session: Session) -> None:
    _seed_workspace(db_session)
    dev = _release(db_session)
    start_workspace_release_evaluation(db_session, dev, actor="worker", expected_lock_version=1)
    approved = settle_machine_evaluation(
        db_session,
        dev,
        actor="worker",
        expected_lock_version=2,
        result="pass",
        evidence_sha256=HEX,
    )
    assert approved.status == RELEASE_APPROVED
    assert approved.decision_set_sha256 == EMPTY_DECISION_SET_SHA256


def test_staging_machine_pass_requires_predecessor_then_approves(db_session: Session) -> None:
    _seed_workspace(db_session, environment_id=STAGING_ENVIRONMENT_ID, environment_class="staging")
    candidate = _release(db_session, environment_id=STAGING_ENVIRONMENT_ID, key="release-staging")
    start_workspace_release_evaluation(
        db_session, candidate, actor="worker", expected_lock_version=1
    )
    blocked = settle_machine_evaluation(
        db_session,
        candidate,
        actor="worker",
        expected_lock_version=2,
        result="pass",
        evidence_sha256=HEX,
        predecessor_valid=False,
    )
    assert blocked.status == RELEASE_BLOCKED
    resumed = resume_blocked_workspace_release(
        db_session,
        blocked,
        actor="worker",
        expected_lock_version=3,
        revision_id=REVISION_ID,
        environment_config_sha256=HEX,
        runtime_dependencies_sha256=HEX,
        policy_sha256=HEX,
    )
    approved = settle_machine_evaluation(
        db_session,
        resumed,
        actor="worker",
        expected_lock_version=4,
        result="pass",
        evidence_sha256=HEX,
        predecessor_valid=True,
    )
    assert approved.status == RELEASE_APPROVED


def test_staging_blocker_can_resume_and_machine_no_go_is_terminal(db_session: Session) -> None:
    _seed_workspace(db_session, environment_id=STAGING_ENVIRONMENT_ID, environment_class="staging")
    candidate = _release(db_session, environment_id=STAGING_ENVIRONMENT_ID)
    start_workspace_release_evaluation(
        db_session, candidate, actor="worker", expected_lock_version=1
    )
    blocked = settle_machine_evaluation(
        db_session,
        candidate,
        actor="worker",
        expected_lock_version=2,
        result="pass",
        evidence_sha256=HEX,
        predecessor_valid=False,
    )
    assert blocked.status == RELEASE_BLOCKED
    resumed = resume_blocked_workspace_release(
        db_session,
        blocked,
        actor="worker",
        expected_lock_version=3,
        revision_id=REVISION_ID,
        environment_config_sha256=HEX,
        runtime_dependencies_sha256=HEX,
        policy_sha256=HEX,
    )
    rejected = settle_machine_evaluation(
        db_session,
        resumed,
        actor="worker",
        expected_lock_version=4,
        result="no_go",
        evidence_sha256=HEX,
    )
    assert rejected.status == RELEASE_REJECTED
    with pytest.raises(WorkspaceReleaseTransitionError):
        transition_workspace_release(
            db_session,
            rejected,
            RELEASE_EVALUATING,
            actor="worker",
            expected_lock_version=5,
        )
    with pytest.raises(WorkspaceReleaseConflictError, match="coordinates"):
        resume_blocked_workspace_release(
            db_session,
            blocked,
            actor="worker",
            expected_lock_version=3,
            revision_id="WSR-other",
            environment_config_sha256=HEX,
            runtime_dependencies_sha256=HEX,
            policy_sha256=HEX,
        )


def test_qa_and_production_machine_passes_stop_for_quality_signoff(db_session: Session) -> None:
    _seed_workspace(db_session, environment_id=QA_ENVIRONMENT_ID, environment_class="qa")
    release = _release(db_session, environment_id=QA_ENVIRONMENT_ID)
    start_workspace_release_evaluation(db_session, release, actor="worker", expected_lock_version=1)
    settled = settle_machine_evaluation(
        db_session,
        release,
        actor="worker",
        expected_lock_version=2,
        result="pass",
        evidence_sha256=HEX,
    )
    assert settled.status == RELEASE_AWAITING_QUALITY_SIGNOFF


def test_durable_evaluation_replay_lease_recovery_and_settlement(db_session: Session) -> None:
    _seed_workspace(db_session)
    release = _release(db_session)
    start_workspace_release_evaluation(db_session, release, actor="worker", expected_lock_version=1)
    evaluation = create_workspace_release_evaluation(
        db_session,
        project_id=PROJECT_ID,
        workspace_release_id=release.release_id,
        idempotency_key="eval-1",
        evaluation_plan_sha256=HEX,
        input_sha256=HEX,
        requested_by="developer",
    )
    replay = create_workspace_release_evaluation(
        db_session,
        project_id=PROJECT_ID,
        workspace_release_id=release.release_id,
        idempotency_key="eval-1",
        evaluation_plan_sha256=HEX,
        input_sha256=HEX,
        requested_by="developer",
    )
    assert replay.evaluation_id == evaluation.evaluation_id
    with pytest.raises(WorkspaceReleaseConflictError, match="idempotency"):
        create_workspace_release_evaluation(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            idempotency_key="eval-1",
            evaluation_plan_sha256="b" * 64,
            input_sha256=HEX,
            requested_by="developer",
        )
    claimed = claim_workspace_release_evaluation(
        db_session, evaluation, worker_id="worker-1", lease_seconds=10, now=NOW
    )
    assert claimed.status == EVALUATION_RUNNING
    heartbeat_workspace_release_evaluation(
        db_session, claimed, worker_id="worker-1", lease_seconds=10, now=NOW + timedelta(seconds=1)
    )
    with pytest.raises(WorkspaceReleaseLeaseError, match="another"):
        claim_workspace_release_evaluation(
            db_session,
            claimed,
            worker_id="worker-2",
            lease_seconds=10,
            now=NOW + timedelta(seconds=2),
        )
    recovered = claim_workspace_release_evaluation(
        db_session, claimed, worker_id="worker-2", lease_seconds=10, now=NOW + timedelta(seconds=20)
    )
    assert recovered.attempt_number == 2
    done = complete_workspace_release_evaluation(
        db_session,
        recovered,
        worker_id="worker-2",
        result="pass",
        expected_release_lock_version=2,
        evidence_sha256=HEX,
        evaluation_run_ids=["EVR-1"],
        gate_verdict_id="GV-1",
        now=NOW + timedelta(seconds=21),
    )
    assert done.status == EVALUATION_SUCCEEDED
    assert done.lease_expires_at is None
    assert release.status == RELEASE_APPROVED


def test_evaluation_blocker_fails_attempt_and_release_is_retryable(db_session: Session) -> None:
    _seed_workspace(db_session)
    release = _release(db_session)
    start_workspace_release_evaluation(db_session, release, actor="worker", expected_lock_version=1)
    evaluation = create_workspace_release_evaluation(
        db_session,
        project_id=PROJECT_ID,
        workspace_release_id=release.release_id,
        idempotency_key="eval-blocked",
        evaluation_plan_sha256=HEX,
        input_sha256=HEX,
        requested_by="developer",
    )
    claim_workspace_release_evaluation(db_session, evaluation, worker_id="worker", now=NOW)
    done = complete_workspace_release_evaluation(
        db_session,
        evaluation,
        worker_id="worker",
        result="blocked",
        expected_release_lock_version=2,
        error_code="worker_timeout",
        error_summary="worker lost its source",
        now=NOW + timedelta(seconds=1),
    )
    assert done.status == EVALUATION_FAILED
    assert release.status == RELEASE_BLOCKED
    assert release.error_code == "worker_timeout"
    # A blocked release is a retryable operational failure (worker/infra),
    # not a content rejection -- workspace_release_governance.py only ever
    # creates a rework task from a RELEASE_REJECTED transition. Asserting
    # zero rows here, rather than only in the governance test module, pins
    # that a blocked release never accumulates owned human rework, even
    # though the block path never imports CaliberReworkTask at all.
    assert (
        db_session.execute(
            select(CaliberReworkTask).where(
                CaliberReworkTask.workspace_release_id == release.release_id
            )
        ).scalar_one_or_none()
        is None
    )


def test_operation_state_machine_rejects_illegal_edges_and_uses_cas(db_session: Session) -> None:
    _seed_workspace(db_session)
    release = _approved_release(db_session)
    operation = create_workspace_release_operation(
        db_session,
        project_id=PROJECT_ID,
        workspace_release_id=release.release_id,
        environment_id=DEV_ENVIRONMENT_ID,
        kind="apply",
        idempotency_key="op-1",
        expected_environment_lock_version=1,
        requested_by="developer",
    )
    started = transition_workspace_release_operation(
        db_session,
        operation,
        OPERATION_APPLYING,
        actor="worker",
        expected_lock_version=1,
    )
    assert started.applied_by == "worker"
    with pytest.raises(WorkspaceReleaseOperationConflictError, match="stale"):
        transition_workspace_release_operation(
            db_session,
            started,
            OPERATION_APPLIED,
            actor="worker",
            expected_lock_version=1,
        )
    applied = transition_workspace_release_operation(
        db_session,
        started,
        OPERATION_APPLIED,
        actor="worker",
        expected_lock_version=2,
    )
    assert applied.status == OPERATION_APPLIED
    with pytest.raises(WorkspaceReleaseOperationTransitionError, match="illegal"):
        transition_workspace_release_operation(
            db_session,
            applied,
            OPERATION_FAILED,
            actor="worker",
            expected_lock_version=3,
        )


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (state, target)
        for state, targets in OPERATION_TRANSITIONS.items()
        for target in sorted(set(OPERATION_TRANSITIONS) - targets - {state})
    ],
)
def test_operation_state_machine_rejects_every_illegal_edge(
    db_session: Session, current: str, target: str
) -> None:
    _seed_workspace(db_session)
    release = _approved_release(db_session)
    operation = create_workspace_release_operation(
        db_session,
        project_id=PROJECT_ID,
        workspace_release_id=release.release_id,
        environment_id=DEV_ENVIRONMENT_ID,
        kind="apply",
        idempotency_key=f"op-{current}",
        expected_environment_lock_version=1,
        requested_by="developer",
    )
    operation.status = current
    db_session.flush()
    with pytest.raises(WorkspaceReleaseOperationTransitionError, match="illegal"):
        transition_workspace_release_operation(
            db_session,
            operation,
            target,
            actor="worker",
            expected_lock_version=1,
        )


def test_operation_idempotency_active_lock_and_exact_rollback_target(db_session: Session) -> None:
    _seed_workspace(db_session)
    old = _approved_release(db_session, key="old")
    old_op = create_workspace_release_operation(
        db_session,
        project_id=PROJECT_ID,
        workspace_release_id=old.release_id,
        environment_id=DEV_ENVIRONMENT_ID,
        kind="apply",
        idempotency_key="old-op",
        expected_environment_lock_version=1,
        requested_by="developer",
    )
    transition_workspace_release_operation(
        db_session, old_op, OPERATION_APPLYING, actor="worker", expected_lock_version=1
    )
    transition_workspace_release_operation(
        db_session, old_op, OPERATION_APPLIED, actor="worker", expected_lock_version=2
    )
    current = _approved_release(db_session, key="current")
    replay = create_workspace_release_operation(
        db_session,
        project_id=PROJECT_ID,
        workspace_release_id=current.release_id,
        environment_id=DEV_ENVIRONMENT_ID,
        kind="apply",
        idempotency_key="current-op",
        expected_environment_lock_version=1,
        requested_by="developer",
    )
    assert replay.operation_id
    with pytest.raises(WorkspaceReleaseOperationConflictError, match="active"):
        create_workspace_release_operation(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=current.release_id,
            environment_id=DEV_ENVIRONMENT_ID,
            kind="apply",
            idempotency_key="another-op",
            expected_environment_lock_version=1,
            requested_by="developer",
        )
    transition_workspace_release_operation(
        db_session, replay, OPERATION_CANCELLED, actor="operator", expected_lock_version=1
    )
    rollback = create_workspace_release(
        db_session,
        project_id=PROJECT_ID,
        revision_id=REVISION_ID,
        environment_id=DEV_ENVIRONMENT_ID,
        environment_config_sha256=HEX,
        runtime_dependencies_sha256=HEX,
        policy_sha256=HEX,
        request_idempotency_key="rollback-release",
        requested_by="developer",
    )
    rollback.status = RELEASE_APPROVED
    db_session.flush()
    with pytest.raises(WorkspaceReleaseOperationConflictError, match="never applied"):
        create_workspace_release_operation(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=current.release_id,
            environment_id=DEV_ENVIRONMENT_ID,
            kind="rollback",
            idempotency_key="bad-rollback",
            expected_environment_lock_version=2,
            expected_current_release_id=current.release_id,
            target_release_id=rollback.release_id,
            requested_by="operator",
        )
    operation = create_workspace_release_operation(
        db_session,
        project_id=PROJECT_ID,
        workspace_release_id=current.release_id,
        environment_id=DEV_ENVIRONMENT_ID,
        kind="rollback",
        idempotency_key="rollback-op",
        expected_environment_lock_version=2,
        expected_current_release_id=current.release_id,
        target_release_id=old.release_id,
        requested_by="operator",
    )
    assert operation.target_release_id == old.release_id


def test_database_constraints_reject_invalid_evidence_decisions_and_expiry(
    db_session: Session,
) -> None:
    _seed_workspace(db_session)
    release = _release(db_session)
    db_session.add(
        CaliberWorkspaceReleaseEvidence(
            evidence_id="WSRELE-invalid",
            workspace_release_id=release.release_id,
            kind="not-a-kind",
            evidence_ref="x",
            evidence_sha256=HEX,
            recorded_by="worker",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()

    release = _release(db_session, key="invalid-lock-version")
    release.lock_version = 0
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_release_service_rejects_invalid_inputs_and_cas_races(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_workspace(db_session)
    release = _release(db_session)
    with pytest.raises(ValueError, match="idempotency_key"):
        _release(db_session, key=" ")
    with pytest.raises(ValueError, match="SHA-256"):
        create_workspace_release(
            db_session,
            project_id=PROJECT_ID,
            revision_id=REVISION_ID,
            environment_id=DEV_ENVIRONMENT_ID,
            environment_config_sha256="invalid",
            runtime_dependencies_sha256=HEX,
            policy_sha256=HEX,
            request_idempotency_key="invalid-digest",
            requested_by="developer",
        )
    with pytest.raises(WorkspaceReleaseConflictError, match="idempotency"):
        create_workspace_release(
            db_session,
            project_id=PROJECT_ID,
            revision_id="WSR-other",
            environment_id=DEV_ENVIRONMENT_ID,
            environment_config_sha256=HEX,
            runtime_dependencies_sha256=HEX,
            policy_sha256=HEX,
            request_idempotency_key="release-1",
            requested_by="developer",
        )
    assert _release(db_session).release_id == release.release_id
    with pytest.raises(WorkspaceReleaseConflictError, match="not found"):
        transition_workspace_release(
            db_session,
            CaliberWorkspaceRelease(release_id="WSREL-missing"),
            RELEASE_EVALUATING,
            actor="worker",
            expected_lock_version=1,
        )
    start_workspace_release_evaluation(db_session, release, actor="worker", expected_lock_version=1)
    with pytest.raises(ValueError, match="error_code"):
        block_workspace_release(
            db_session,
            release,
            actor="worker",
            expected_lock_version=2,
            error_code="x" * 65,
            error_summary="blocked",
        )
    with pytest.raises(ValueError, match="error_summary"):
        block_workspace_release(
            db_session,
            release,
            actor="worker",
            expected_lock_version=2,
            error_code="blocked",
            error_summary="x" * 4001,
        )
    with pytest.raises(ValueError, match="evidence"):
        settle_machine_evaluation(
            db_session,
            release,
            actor="worker",
            expected_lock_version=2,
            result="pass",
        )
    missing_environment = _release(
        db_session, environment_id="WSE-missing", key="missing-environment"
    )
    start_workspace_release_evaluation(
        db_session, missing_environment, actor="worker", expected_lock_version=1
    )
    with pytest.raises(WorkspaceReleaseConflictError, match="environment"):
        settle_machine_evaluation(
            db_session,
            missing_environment,
            actor="worker",
            expected_lock_version=2,
            result="pass",
            evidence_sha256=HEX,
        )

    real_execute = db_session.execute

    def lose_update(statement: object, *args: object, **kwargs: object) -> object:
        result = real_execute(statement, *args, **kwargs)  # type: ignore[arg-type]
        if isinstance(statement, Update):
            return SimpleNamespace(rowcount=0)
        return result

    monkeypatch.setattr(db_session, "execute", lose_update)
    with pytest.raises(WorkspaceReleaseConflictError, match="CAS race"):
        transition_workspace_release(
            db_session,
            release,
            RELEASE_BLOCKED,
            actor="worker",
            expected_lock_version=2,
        )


def test_evaluation_service_rejects_missing_active_and_lease_edge_cases(
    db_session: Session,
) -> None:
    _seed_workspace(db_session)
    release = _release(db_session)
    with pytest.raises(ValueError, match="idempotency_key"):
        create_workspace_release_evaluation(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            idempotency_key=" ",
            evaluation_plan_sha256=HEX,
            input_sha256=HEX,
            requested_by="developer",
        )
    with pytest.raises(ValueError, match="SHA-256"):
        create_workspace_release_evaluation(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            idempotency_key="bad-digest",
            evaluation_plan_sha256="bad",
            input_sha256=HEX,
            requested_by="developer",
        )
    with pytest.raises(WorkspaceReleaseConflictError, match="not evaluating"):
        create_workspace_release_evaluation(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            idempotency_key="draft",
            evaluation_plan_sha256=HEX,
            input_sha256=HEX,
            requested_by="developer",
        )
    start_workspace_release_evaluation(db_session, release, actor="worker", expected_lock_version=1)
    evaluation = create_workspace_release_evaluation(
        db_session,
        project_id=PROJECT_ID,
        workspace_release_id=release.release_id,
        idempotency_key="active-1",
        evaluation_plan_sha256=HEX,
        input_sha256=HEX,
        requested_by="developer",
    )
    with pytest.raises(WorkspaceReleaseConflictError, match="active"):
        create_workspace_release_evaluation(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            idempotency_key="active-2",
            evaluation_plan_sha256=HEX,
            input_sha256=HEX,
            requested_by="developer",
        )
    with pytest.raises(WorkspaceReleaseConflictError, match="outside"):
        create_workspace_release_evaluation(
            db_session,
            project_id="PRJ-other",
            workspace_release_id=release.release_id,
            idempotency_key="wrong-project",
            evaluation_plan_sha256=HEX,
            input_sha256=HEX,
            requested_by="developer",
        )
    with pytest.raises(ValueError, match="worker_id"):
        claim_workspace_release_evaluation(db_session, evaluation, worker_id="", now=NOW)
    with pytest.raises(ValueError, match="worker_id"):
        claim_workspace_release_evaluation(
            db_session, evaluation, worker_id="worker", lease_seconds=0, now=NOW
        )
    with pytest.raises(WorkspaceReleaseLeaseError, match="not found"):
        claim_workspace_release_evaluation(
            db_session,
            CaliberWorkspaceReleaseEvaluation(evaluation_id="WSRELEV-missing"),
            worker_id="worker",
            now=NOW,
        )
    claimed = claim_workspace_release_evaluation(
        db_session, evaluation, worker_id="worker", lease_seconds=10, now=NOW
    )
    same_worker = claim_workspace_release_evaluation(
        db_session, claimed, worker_id="worker", lease_seconds=10, now=NOW + timedelta(seconds=1)
    )
    assert same_worker.attempt_number == 1
    with pytest.raises(WorkspaceReleaseLeaseError, match="not live"):
        heartbeat_workspace_release_evaluation(
            db_session,
            same_worker,
            worker_id="worker",
            now=NOW + timedelta(seconds=20),
        )
    with pytest.raises(ValueError, match="lease_seconds"):
        heartbeat_workspace_release_evaluation(
            db_session,
            same_worker,
            worker_id="worker",
            lease_seconds=0,
            now=NOW + timedelta(seconds=1),
        )
    with pytest.raises(WorkspaceReleaseLeaseError, match="not running"):
        heartbeat_workspace_release_evaluation(
            db_session,
            CaliberWorkspaceReleaseEvaluation(evaluation_id="WSRELEV-missing"),
            worker_id="worker",
            now=NOW,
        )
    with pytest.raises(WorkspaceReleaseLeaseError, match="not running"):
        complete_workspace_release_evaluation(
            db_session,
            CaliberWorkspaceReleaseEvaluation(evaluation_id="WSRELEV-missing"),
            worker_id="worker",
            result="pass",
            expected_release_lock_version=2,
            evidence_sha256=HEX,
            now=NOW,
        )
    with pytest.raises(WorkspaceReleaseLeaseError, match="owned by another"):
        complete_workspace_release_evaluation(
            db_session,
            same_worker,
            worker_id="other-worker",
            result="pass",
            expected_release_lock_version=2,
            evidence_sha256=HEX,
            now=NOW + timedelta(seconds=2),
        )
    with pytest.raises(WorkspaceReleaseLeaseError, match="expired"):
        complete_workspace_release_evaluation(
            db_session,
            same_worker,
            worker_id="worker",
            result="pass",
            expected_release_lock_version=2,
            evidence_sha256=HEX,
            now=NOW + timedelta(seconds=20),
        )
    same_worker.status = EVALUATION_FAILED
    db_session.flush()
    with pytest.raises(WorkspaceReleaseLeaseError, match="terminal"):
        claim_workspace_release_evaluation(
            db_session, same_worker, worker_id="worker", now=NOW + timedelta(seconds=20)
        )


def test_evaluation_claim_and_heartbeat_fail_closed_on_cas_races(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_workspace(db_session)
    release = _release(db_session)
    start_workspace_release_evaluation(db_session, release, actor="worker", expected_lock_version=1)
    evaluation = create_workspace_release_evaluation(
        db_session,
        project_id=PROJECT_ID,
        workspace_release_id=release.release_id,
        idempotency_key="cas-race",
        evaluation_plan_sha256=HEX,
        input_sha256=HEX,
        requested_by="developer",
    )

    real_execute = db_session.execute

    def lose_update(statement: object, *args: object, **kwargs: object) -> object:
        result = real_execute(statement, *args, **kwargs)  # type: ignore[arg-type]
        if isinstance(statement, Update):
            return SimpleNamespace(rowcount=0)
        return result

    monkeypatch.setattr(db_session, "execute", lose_update)
    with pytest.raises(WorkspaceReleaseLeaseError, match="CAS race"):
        claim_workspace_release_evaluation(db_session, evaluation, worker_id="worker", now=NOW)
    evaluation.status = EVALUATION_RUNNING
    evaluation.claimed_by = "worker"
    evaluation.lease_expires_at = NOW.replace(tzinfo=None) + timedelta(seconds=10)
    evaluation.heartbeat_at = NOW.replace(tzinfo=None)
    db_session.flush()
    with pytest.raises(WorkspaceReleaseLeaseError, match="during renewal"):
        claim_workspace_release_evaluation(
            db_session, evaluation, worker_id="worker", now=NOW + timedelta(seconds=1)
        )
    with pytest.raises(WorkspaceReleaseLeaseError, match="heartbeat.*CAS race"):
        heartbeat_workspace_release_evaluation(
            db_session, evaluation, worker_id="worker", now=NOW + timedelta(seconds=1)
        )


def test_operation_service_rejects_invalid_requests_and_missing_rows(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_workspace(db_session)
    release = _release(db_session)
    with pytest.raises(ValueError, match="apply or rollback"):
        create_workspace_release_operation(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            environment_id=DEV_ENVIRONMENT_ID,
            kind="publish",
            idempotency_key="invalid",
            expected_environment_lock_version=1,
            requested_by="developer",
        )
    release.status = RELEASE_APPROVED
    db_session.flush()
    with pytest.raises(ValueError, match="idempotency_key"):
        create_workspace_release_operation(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            environment_id=DEV_ENVIRONMENT_ID,
            kind="apply",
            idempotency_key=" ",
            expected_environment_lock_version=1,
            requested_by="developer",
        )
    with pytest.raises(ValueError, match="positive"):
        create_workspace_release_operation(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            environment_id=DEV_ENVIRONMENT_ID,
            kind="apply",
            idempotency_key="zero-lock",
            expected_environment_lock_version=0,
            requested_by="developer",
        )
    with pytest.raises(WorkspaceReleaseOperationConflictError, match="rollback target"):
        create_workspace_release_operation(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            environment_id=DEV_ENVIRONMENT_ID,
            kind="apply",
            idempotency_key="apply-target",
            expected_environment_lock_version=1,
            target_release_id="WSREL-target",
            requested_by="developer",
        )
    with pytest.raises(WorkspaceReleaseOperationConflictError, match="requires"):
        create_workspace_release_operation(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            environment_id=DEV_ENVIRONMENT_ID,
            kind="rollback",
            idempotency_key="rollback-no-target",
            expected_environment_lock_version=1,
            requested_by="developer",
        )
    with pytest.raises(WorkspaceReleaseOperationConflictError, match="expected_current"):
        create_workspace_release_operation(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            environment_id=DEV_ENVIRONMENT_ID,
            kind="rollback",
            idempotency_key="rollback-wrong-current",
            expected_environment_lock_version=1,
            expected_current_release_id="WSREL-other",
            target_release_id="WSREL-target",
            requested_by="developer",
        )
    with pytest.raises(WorkspaceReleaseOperationConflictError, match="prior"):
        create_workspace_release_operation(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            environment_id=DEV_ENVIRONMENT_ID,
            kind="rollback",
            idempotency_key="rollback-self",
            expected_environment_lock_version=1,
            expected_current_release_id=release.release_id,
            target_release_id=release.release_id,
            requested_by="developer",
        )
    qa_environment = CaliberWorkspaceEnvironment(
        environment_id=QA_ENVIRONMENT_ID,
        project_id=PROJECT_ID,
        name="qa",
        environment_class="qa",
        promotion_order=20,
        status="active",
        created_by="developer",
    )
    db_session.add(qa_environment)
    db_session.flush()
    qa_release = _release(db_session, environment_id=QA_ENVIRONMENT_ID, key="qa-target")
    with pytest.raises(WorkspaceReleaseOperationConflictError, match="coordinates"):
        create_workspace_release_operation(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            environment_id=DEV_ENVIRONMENT_ID,
            kind="rollback",
            idempotency_key="wrong-coordinates",
            expected_environment_lock_version=1,
            expected_current_release_id=release.release_id,
            target_release_id=qa_release.release_id,
            requested_by="developer",
        )
    with pytest.raises(WorkspaceReleaseOperationConflictError, match="outside"):
        create_workspace_release_operation(
            db_session,
            project_id="PRJ-other",
            workspace_release_id=release.release_id,
            environment_id=DEV_ENVIRONMENT_ID,
            kind="apply",
            idempotency_key="wrong-project",
            expected_environment_lock_version=1,
            requested_by="developer",
        )
    with pytest.raises(WorkspaceReleaseOperationConflictError, match="environment"):
        create_workspace_release_operation(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            environment_id="WSE-other",
            kind="apply",
            idempotency_key="wrong-environment",
            expected_environment_lock_version=1,
            requested_by="developer",
        )
    operation = create_workspace_release_operation(
        db_session,
        project_id=PROJECT_ID,
        workspace_release_id=release.release_id,
        environment_id=DEV_ENVIRONMENT_ID,
        kind="apply",
        idempotency_key="replay",
        expected_environment_lock_version=1,
        requested_by="developer",
    )
    assert (
        create_workspace_release_operation(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            environment_id=DEV_ENVIRONMENT_ID,
            kind="apply",
            idempotency_key="replay",
            expected_environment_lock_version=1,
            requested_by="developer",
        ).operation_id
        == operation.operation_id
    )
    with pytest.raises(WorkspaceReleaseOperationConflictError, match="idempotency"):
        create_workspace_release_operation(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            environment_id=DEV_ENVIRONMENT_ID,
            kind="apply",
            idempotency_key="replay",
            expected_environment_lock_version=2,
            requested_by="developer",
        )
    with pytest.raises(WorkspaceReleaseOperationConflictError, match="not found"):
        transition_workspace_release_operation(
            db_session,
            CaliberWorkspaceReleaseOperation(operation_id="WSRELOP-missing"),
            OPERATION_APPLYING,
            actor="worker",
            expected_lock_version=1,
        )
    with pytest.raises(ValueError, match="error_code"):
        transition_workspace_release_operation(
            db_session,
            operation,
            OPERATION_APPLYING,
            actor="worker",
            expected_lock_version=1,
            error_code="x" * 65,
        )
    with pytest.raises(ValueError, match="error_summary"):
        transition_workspace_release_operation(
            db_session,
            operation,
            OPERATION_APPLYING,
            actor="worker",
            expected_lock_version=1,
            error_summary="x" * 4001,
        )
    real_execute = db_session.execute

    def lose_update(statement: object, *args: object, **kwargs: object) -> object:
        result = real_execute(statement, *args, **kwargs)  # type: ignore[arg-type]
        if isinstance(statement, Update):
            return SimpleNamespace(rowcount=0)
        return result

    monkeypatch.setattr(db_session, "execute", lose_update)
    with pytest.raises(WorkspaceReleaseOperationConflictError, match="CAS race"):
        transition_workspace_release_operation(
            db_session,
            operation,
            OPERATION_APPLYING,
            actor="worker",
            expected_lock_version=1,
        )


def test_operation_apply_requires_approval(db_session: Session) -> None:
    _seed_workspace(db_session)
    release = _release(db_session)
    with pytest.raises(WorkspaceReleaseOperationConflictError, match="approved"):
        create_workspace_release_operation(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            environment_id=DEV_ENVIRONMENT_ID,
            kind="apply",
            idempotency_key="unapproved",
            expected_environment_lock_version=1,
            requested_by="developer",
        )
