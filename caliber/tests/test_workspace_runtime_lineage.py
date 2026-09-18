"""P5-D runtime lineage and strict-execution contract tests."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from caliber.db.models import (
    CaliberProject,
    CaliberWorkspaceEnvironment,
    CaliberWorkspaceRelease,
    CaliberWorkspaceReleaseEvidence,
    CaliberWorkspaceReleaseOperation,
    CaliberWorkspaceRevision,
)
from caliber.workspace_release_service import create_workspace_release
from caliber.workspace_runtime_lineage import (
    MissingRuntimeLineageError,
    RuntimeLineageError,
    create_runtime_lineage,
    reconstruct_runtime_lineage,
    record_workspace_release_evidence,
    require_runtime_lineage,
)

PROJECT_ID = "PRJ-p5d"
ENVIRONMENT_ID = "WSE-p5d-dev"
REVISION_ID = "WSR-p5d"
HEX = "a" * 64


def _seed(
    session: Session,
    *,
    approved: bool = True,
    current: bool = True,
    environment_status: str = "active",
) -> tuple[CaliberWorkspaceRelease, CaliberWorkspaceEnvironment]:
    project = CaliberProject(project_id=PROJECT_ID, name="P5-D workspace", owner="developer")
    environment = CaliberWorkspaceEnvironment(
        environment_id=ENVIRONMENT_ID,
        project_id=PROJECT_ID,
        name="dev",
        environment_class="development",
        promotion_order=10,
        status=environment_status,
        created_by="developer",
    )
    revision = CaliberWorkspaceRevision(
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
    session.add_all([project, environment, revision])
    session.flush()
    release = create_workspace_release(
        session,
        project_id=PROJECT_ID,
        revision_id=REVISION_ID,
        environment_id=ENVIRONMENT_ID,
        environment_config_sha256=HEX,
        runtime_dependencies_sha256=HEX,
        policy_sha256=HEX,
        request_idempotency_key="release-p5d",
        requested_by="developer",
    )
    if approved:
        release.status = "approved"
    if current:
        environment.current_release_id = release.release_id
    session.flush()
    return release, environment


def test_reconstruct_lineage_returns_one_joined_provenance_projection(db_session: Session) -> None:
    release, _environment = _seed(db_session)
    lineage = create_runtime_lineage(
        db_session,
        project_id=PROJECT_ID,
        workspace_release_id=release.release_id,
        revision_id=REVISION_ID,
        environment_id=ENVIRONMENT_ID,
        consumer_kind="run",
        consumer_id="WR-p5d",
        model_id="model-a",
        created_by="developer",
    )

    snapshot = reconstruct_runtime_lineage(db_session, lineage.lineage_id, project_id=PROJECT_ID)

    assert snapshot.as_dict() == {
        "lineage_id": lineage.lineage_id,
        "project_id": PROJECT_ID,
        "consumer_kind": "run",
        "consumer_id": "WR-p5d",
        "workspace_release_id": release.release_id,
        "revision_id": REVISION_ID,
        "environment_id": ENVIRONMENT_ID,
        "model_id": "model-a",
        "config_sha256": HEX,
        "runtime_dependencies_sha256": HEX,
        "policy_sha256": HEX,
        "eligibility_status": "eligible",
        "eligibility_reason": "release, revision, and environment coordinates are bound",
        "strict_execution": True,
        "release_status": "approved",
        "environment_status": "active",
        "environment_operation_state": "idle",
        "current_release_id": release.release_id,
        "revision_status": "ready",
        "revision_sha256": HEX,
    }
    assert (
        require_runtime_lineage(
            db_session,
            lineage.lineage_id,
            consumer_kind="run",
            consumer_id="WR-p5d",
        ).lineage.lineage_id
        == lineage.lineage_id
    )


def test_lineage_is_idempotent_but_cannot_be_rebound(db_session: Session) -> None:
    release, _environment = _seed(db_session)
    first = create_runtime_lineage(
        db_session,
        project_id=PROJECT_ID,
        workspace_release_id=release.release_id,
        revision_id=REVISION_ID,
        environment_id=ENVIRONMENT_ID,
        consumer_kind="run",
        consumer_id="WR-replay",
        created_by="developer",
    )
    replay = create_runtime_lineage(
        db_session,
        project_id=PROJECT_ID,
        workspace_release_id=release.release_id,
        revision_id=REVISION_ID,
        environment_id=ENVIRONMENT_ID,
        consumer_kind="run",
        consumer_id="WR-replay",
        created_by="developer",
    )
    assert replay.lineage_id == first.lineage_id

    with pytest.raises(RuntimeLineageError, match="already bound differently"):
        create_runtime_lineage(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            revision_id=REVISION_ID,
            environment_id=ENVIRONMENT_ID,
            consumer_kind="run",
            consumer_id="WR-replay",
            model_id="different-model",
            created_by="developer",
        )


def test_strict_creation_rejects_bad_release_and_config(db_session: Session) -> None:
    release, _environment = _seed(db_session, approved=False)
    with pytest.raises(RuntimeLineageError, match="not eligible"):
        create_runtime_lineage(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            revision_id=REVISION_ID,
            environment_id=ENVIRONMENT_ID,
            consumer_kind="run",
            consumer_id="WR-blocked",
        )

    release.status = "approved"
    with pytest.raises(RuntimeLineageError, match="config digest"):
        create_runtime_lineage(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            revision_id=REVISION_ID,
            environment_id=ENVIRONMENT_ID,
            consumer_kind="run",
            consumer_id="WR-wrong-config",
            config_sha256="b" * 64,
        )


def test_non_strict_evidence_preserves_blocked_reason_and_is_idempotent(
    db_session: Session,
) -> None:
    release, environment = _seed(
        db_session, approved=False, current=False, environment_status="disabled"
    )
    evidence = record_workspace_release_evidence(
        db_session,
        workspace_release_id=release.release_id,
        kind="provider_preflight",
        evidence_ref="preflight-1",
        evidence_sha256=HEX,
        recorded_by="worker",
    )
    assert evidence.runtime_lineage_id
    lineage = reconstruct_runtime_lineage(db_session, evidence.runtime_lineage_id)
    assert lineage.lineage.eligibility_status == "stale"
    assert "release_status=draft" in lineage.lineage.eligibility_reason
    assert "environment_status=disabled" in lineage.lineage.eligibility_reason
    assert (
        record_workspace_release_evidence(
            db_session,
            workspace_release_id=release.release_id,
            kind="provider_preflight",
            evidence_ref="preflight-1",
            evidence_sha256=HEX,
            recorded_by="worker",
        ).evidence_id
        == evidence.evidence_id
    )
    assert environment.current_release_id is None


def test_evidence_replay_backfills_lineage_for_pre_migration_rows(db_session: Session) -> None:
    release, _environment = _seed(db_session)
    legacy = CaliberWorkspaceReleaseEvidence(
        evidence_id="WSEV-p5d-legacy",
        workspace_release_id=release.release_id,
        kind="provider_preflight",
        evidence_ref="legacy-1",
        evidence_sha256=HEX,
        required=True,
        recorded_by="legacy-worker",
    )
    db_session.add(legacy)
    db_session.flush()

    replay = record_workspace_release_evidence(
        db_session,
        workspace_release_id=release.release_id,
        kind="provider_preflight",
        evidence_ref="legacy-1",
        evidence_sha256=HEX,
        recorded_by="backfill-worker",
    )

    assert replay.evidence_id == legacy.evidence_id
    assert replay.runtime_lineage_id is not None
    assert (
        reconstruct_runtime_lineage(db_session, replay.runtime_lineage_id).lineage.consumer_id
        == legacy.evidence_id
    )


def test_provider_operation_lineage_is_fenced_to_the_pending_lock(db_session: Session) -> None:
    release, environment = _seed(db_session)
    lineage = create_runtime_lineage(
        db_session,
        project_id=PROJECT_ID,
        workspace_release_id=release.release_id,
        revision_id=REVISION_ID,
        environment_id=ENVIRONMENT_ID,
        consumer_kind="provider_operation",
        consumer_id="WSRELOP-p5d",
        created_by="developer",
    )
    with pytest.raises(RuntimeLineageError, match="no longer owns"):
        require_runtime_lineage(
            db_session,
            lineage.lineage_id,
            consumer_kind="provider_operation",
            consumer_id="WSRELOP-p5d",
            require_current_release=False,
        )

    environment.pending_operation_id = "WSRELOP-p5d"
    environment.operation_state = "applying"
    assert (
        require_runtime_lineage(
            db_session,
            lineage.lineage_id,
            consumer_kind="provider_operation",
            consumer_id="WSRELOP-p5d",
            require_current_release=False,
        ).lineage.lineage_id
        == lineage.lineage_id
    )


def test_missing_and_mismatched_coordinates_fail_closed(db_session: Session) -> None:
    release, _environment = _seed(db_session)
    with pytest.raises(MissingRuntimeLineageError):
        require_runtime_lineage(
            db_session,
            None,
            consumer_kind="run",
            consumer_id="WR-missing",
        )
    with pytest.raises(ValueError, match="consumer kind"):
        create_runtime_lineage(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            revision_id=REVISION_ID,
            environment_id=ENVIRONMENT_ID,
            consumer_kind="unknown",
            consumer_id="x",
        )
    with pytest.raises(RuntimeLineageError, match="not found"):
        reconstruct_runtime_lineage(db_session, "missing", project_id=PROJECT_ID)


def test_validation_edges_and_stale_runtime_state_are_explicit(db_session: Session) -> None:
    release, environment = _seed(db_session, current=False)
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        create_runtime_lineage(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            revision_id=REVISION_ID,
            environment_id=ENVIRONMENT_ID,
            consumer_kind="run",
            consumer_id="WR-bad-digest",
            config_sha256="not-a-digest",
        )
    with pytest.raises(ValueError, match="consumer_id"):
        create_runtime_lineage(
            db_session,
            project_id=PROJECT_ID,
            workspace_release_id=release.release_id,
            revision_id=REVISION_ID,
            environment_id=ENVIRONMENT_ID,
            consumer_kind="run",
            consumer_id=" ",
        )
    for kwargs, message in (
        ({"project_id": "other"}, "release is outside"),
        ({"revision_id": "missing"}, "revision is outside"),
        ({"environment_id": "missing"}, "environment is outside"),
    ):
        coordinates = {
            "project_id": PROJECT_ID,
            "workspace_release_id": release.release_id,
            "revision_id": REVISION_ID,
            "environment_id": ENVIRONMENT_ID,
            "consumer_kind": "run",
            "consumer_id": f"WR-{len(message)}",
        }
        coordinates.update(kwargs)
        with pytest.raises(RuntimeLineageError, match=message):
            create_runtime_lineage(db_session, **coordinates)

    stale = create_runtime_lineage(
        db_session,
        project_id=PROJECT_ID,
        workspace_release_id=release.release_id,
        revision_id=REVISION_ID,
        environment_id=ENVIRONMENT_ID,
        consumer_kind="run",
        consumer_id="WR-current-mismatch",
        strict_execution=False,
    )
    with pytest.raises(RuntimeLineageError, match="not eligible"):
        require_runtime_lineage(
            db_session,
            stale.lineage_id,
            consumer_kind="run",
            consumer_id="WR-current-mismatch",
        )
    environment.current_release_id = release.release_id
    environment.operation_state = "applying"
    operation_state_lineage = create_runtime_lineage(
        db_session,
        project_id=PROJECT_ID,
        workspace_release_id=release.release_id,
        revision_id=REVISION_ID,
        environment_id=ENVIRONMENT_ID,
        consumer_kind="run",
        consumer_id="WR-operation-state",
        strict_execution=False,
    )
    assert "environment_operation_state=applying" in operation_state_lineage.eligibility_reason
    environment.operation_state = "idle"
    environment.pending_operation_id = "another-operation"
    provider_lineage = create_runtime_lineage(
        db_session,
        project_id=PROJECT_ID,
        workspace_release_id=release.release_id,
        revision_id=REVISION_ID,
        environment_id=ENVIRONMENT_ID,
        consumer_kind="provider_operation",
        consumer_id="WSRELOP-owned",
        strict_execution=False,
    )
    assert "environment_owned_by_another_operation" in provider_lineage.eligibility_reason


def test_evidence_digest_conflict_and_strict_consumer_fencing(db_session: Session) -> None:
    release, environment = _seed(db_session)
    with pytest.raises(RuntimeLineageError, match="release not found"):
        record_workspace_release_evidence(
            db_session,
            workspace_release_id="missing",
            kind="gate_verdict",
            evidence_ref="missing",
            evidence_sha256=HEX,
            recorded_by="worker",
        )
    evidence = record_workspace_release_evidence(
        db_session,
        workspace_release_id=release.release_id,
        kind="gate_verdict",
        evidence_ref="gate-1",
        evidence_sha256=HEX,
        recorded_by="worker",
    )
    with pytest.raises(RuntimeLineageError, match="already bound differently"):
        record_workspace_release_evidence(
            db_session,
            workspace_release_id=release.release_id,
            kind="gate_verdict",
            evidence_ref="gate-1",
            evidence_sha256="b" * 64,
            recorded_by="worker",
        )
    with pytest.raises(RuntimeLineageError, match="consumer binding"):
        require_runtime_lineage(
            db_session,
            evidence.runtime_lineage_id,
            consumer_kind="run",
            consumer_id=evidence.evidence_id,
        )

    run_lineage = create_runtime_lineage(
        db_session,
        project_id=PROJECT_ID,
        workspace_release_id=release.release_id,
        revision_id=REVISION_ID,
        environment_id=ENVIRONMENT_ID,
        consumer_kind="provider_operation",
        consumer_id="WSRELOP-break-glass",
        strict_execution=False,
        allow_break_glass=True,
    )
    operation = CaliberWorkspaceReleaseOperation(
        operation_id="WSRELOP-break-glass",
        project_id=PROJECT_ID,
        workspace_release_id=release.release_id,
        environment_id=ENVIRONMENT_ID,
        kind="apply",
        idempotency_key="break-glass",
        expected_environment_lock_version=1,
        break_glass_authorization_id="WSBGA-1",
        requested_by="admin",
    )
    db_session.add(operation)
    environment.pending_operation_id = operation.operation_id
    environment.operation_state = "applying"
    db_session.flush()
    assert (
        require_runtime_lineage(
            db_session,
            run_lineage.lineage_id,
            consumer_kind="provider_operation",
            consumer_id=operation.operation_id,
            require_current_release=False,
        ).lineage.lineage_id
        == run_lineage.lineage_id
    )


def test_require_rejects_mutated_release_revision_and_environment_state(
    db_session: Session,
) -> None:
    release, environment = _seed(db_session)
    other_revision = CaliberWorkspaceRevision(
        revision_id="WSR-p5d-other",
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
        status="ready",
        created_by="developer",
    )
    other_environment = CaliberWorkspaceEnvironment(
        environment_id="WSE-p5d-other",
        project_id=PROJECT_ID,
        name="qa",
        environment_class="qa",
        promotion_order=20,
        status="active",
        created_by="developer",
    )
    db_session.add_all([other_revision, other_environment])
    db_session.flush()
    lineage = create_runtime_lineage(
        db_session,
        project_id=PROJECT_ID,
        workspace_release_id=release.release_id,
        revision_id=REVISION_ID,
        environment_id=ENVIRONMENT_ID,
        consumer_kind="run",
        consumer_id="WR-mutation-checks",
    )

    release.revision_id = other_revision.revision_id
    with pytest.raises(RuntimeLineageError, match="revision is no longer consistent"):
        require_runtime_lineage(
            db_session, lineage.lineage_id, consumer_kind="run", consumer_id="WR-mutation-checks"
        )
    release.revision_id = REVISION_ID
    release.environment_id = other_environment.environment_id
    with pytest.raises(RuntimeLineageError, match="environment is no longer consistent"):
        require_runtime_lineage(
            db_session, lineage.lineage_id, consumer_kind="run", consumer_id="WR-mutation-checks"
        )
    release.environment_id = ENVIRONMENT_ID

    revision = db_session.get(CaliberWorkspaceRevision, REVISION_ID)
    assert revision is not None
    revision.status = "draft"
    with pytest.raises(RuntimeLineageError, match="no longer ready"):
        require_runtime_lineage(
            db_session, lineage.lineage_id, consumer_kind="run", consumer_id="WR-mutation-checks"
        )
    revision.status = "ready"
    release.status = "draft"
    with pytest.raises(RuntimeLineageError, match="no longer approved"):
        require_runtime_lineage(
            db_session, lineage.lineage_id, consumer_kind="run", consumer_id="WR-mutation-checks"
        )
    release.status = "approved"
    environment.status = "disabled"
    with pytest.raises(RuntimeLineageError, match="not active"):
        require_runtime_lineage(
            db_session, lineage.lineage_id, consumer_kind="run", consumer_id="WR-mutation-checks"
        )
    environment.status = "active"
    environment.current_release_id = None
    with pytest.raises(RuntimeLineageError, match="not current"):
        require_runtime_lineage(
            db_session, lineage.lineage_id, consumer_kind="run", consumer_id="WR-mutation-checks"
        )
    environment.current_release_id = release.release_id
    environment.operation_state = "applying"
    with pytest.raises(RuntimeLineageError, match="unsettled"):
        require_runtime_lineage(
            db_session, lineage.lineage_id, consumer_kind="run", consumer_id="WR-mutation-checks"
        )
