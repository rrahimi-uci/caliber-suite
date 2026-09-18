"""`P5-E`: the first real Workspace release adapter -- workflow alias rotation.

Mirrors ``test_workspace_release_operations.py``'s fixture pattern
(``_seed``/``_registry``/``_prepare``) but swaps
:class:`~caliber.workspace_release_adapters.FakeWorkspaceResourceAdapter`
for :class:`~caliber.workspace_release_workflow_adapter.WorkflowWorkspaceResourceAdapter`,
proving the same operation state machine actually deploys a real workflow.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.db.models import (
    CaliberProject,
    CaliberWorkflow,
    CaliberWorkflowDeployment,
    CaliberWorkflowVersion,
    CaliberWorkspaceEnvironment,
    CaliberWorkspaceRelease,
    CaliberWorkspaceRevision,
    CaliberWorkspaceRevisionResource,
)
from caliber.workspace_release_adapters import (
    PreparedAction,
    WorkspaceReleaseAdapterError,
    WorkspaceResourceAdapterRegistry,
)
from caliber.workspace_release_operations import (
    apply_workspace_release_operation,
    prepare_workspace_release_operation,
)
from caliber.workspace_release_service import create_workspace_release
from caliber.workspace_release_workflow_adapter import WorkflowWorkspaceResourceAdapter
from tests.workflow_helpers import make_manifest

PROJECT_ID = "PRJ-p5e"
OTHER_PROJECT_ID = "PRJ-p5e-other"
ENV_ID = "WSE-p5e-prod"
WORKFLOW_ID = "wf-p5e"
HEX = "b" * 64

ADAPTER = WorkflowWorkspaceResourceAdapter(actor="admin")
REGISTRY = WorkspaceResourceAdapterRegistry({"workflow": ADAPTER})


def _seed_workflow(
    session: Session,
    *,
    workflow_id: str = WORKFLOW_ID,
    project_id: str = PROJECT_ID,
    version_id: str = "WFV-p5e-1",
    version_number: int = 1,
    gated_for: list[str] | None = None,
) -> str:
    """Insert a published workflow version. Creates the parent workflow row
    only on ``version_number == 1`` -- callers seeding a second version of
    the same workflow pass a higher ``version_number`` and skip re-insert."""
    if version_number == 1:
        session.add(
            CaliberWorkflow(
                workflow_id=workflow_id,
                name="P5-E workflow",
                owner="developer",
                status="active",
                project_id=project_id,
                visibility="project",
            )
        )
    manifest = make_manifest(workflow_id)
    if gated_for:
        manifest["deploy_gates"] = {
            "quality_gate": {
                "type": "deploy_gate",
                "dataset_ref": "does-not-matter",
                "required_for_aliases": gated_for,
                "thresholds": {"min_completion_rate": 1.0},
            }
        }
    session.add(
        CaliberWorkflowVersion(
            version_id=version_id,
            workflow_id=workflow_id,
            version_number=version_number,
            status="published",
            manifest=manifest,
            manifest_hash=HEX,
            published_by="developer",
        )
    )
    session.flush()
    return version_id


def _seed_project_and_environment(
    session: Session, *, project_id: str = PROJECT_ID, env_id: str = ENV_ID
) -> None:
    session.add(CaliberProject(project_id=project_id, name="P5-E", owner="admin"))
    session.add(
        CaliberWorkspaceEnvironment(
            environment_id=env_id,
            project_id=project_id,
            name="prod",
            environment_class="production",
            promotion_order=40,
            status="active",
            created_by="admin",
        )
    )
    session.flush()


def _seed_release(
    session: Session,
    *,
    version_id: str,
    revision_id: str,
    resource_pin_id: str,
    release_key: str,
    project_id: str = PROJECT_ID,
    env_id: str = ENV_ID,
    resource_workflow_id: str = WORKFLOW_ID,
    revision_number: int = 1,
) -> CaliberWorkspaceRelease:
    session.add(
        CaliberWorkspaceRevision(
            revision_id=revision_id,
            project_id=project_id,
            revision_number=revision_number,
            manifest={"apiVersion": "caliber/v1alpha1"},
            manifest_sha256=HEX,
            # Distinct per revision -- uq_workspace_revision_project_sha256 is
            # a real (project_id, revision_sha256) unique constraint.
            source_bundle_sha256=(revision_id + HEX)[:64],
            revision_sha256=(revision_id + HEX)[:64],
            status="ready",
            created_by="developer",
        )
    )
    session.add(
        CaliberWorkspaceRevisionResource(
            resource_pin_id=resource_pin_id,
            revision_id=revision_id,
            resource_type="workflow",
            logical_name="main",
            resource_id=resource_workflow_id,
            version_ref=version_id,
            content_sha256=HEX,
            provider_ref=version_id,
            purpose="runtime",
            resolution={},
        )
    )
    release = create_workspace_release(
        session,
        project_id=project_id,
        revision_id=revision_id,
        environment_id=env_id,
        environment_config_sha256=HEX,
        runtime_dependencies_sha256=HEX,
        policy_sha256=HEX,
        request_idempotency_key=release_key,
        requested_by="developer",
    )
    release.status = "approved"
    session.flush()
    return release


def _prepare(
    session: Session,
    release: CaliberWorkspaceRelease,
    *,
    key: str,
    kind: str = "apply",
    expected_current: str | None = None,
    target: str | None = None,
    expected_lock: int = 1,
    env_id: str = ENV_ID,
    project_id: str = PROJECT_ID,
):
    return prepare_workspace_release_operation(
        session,
        project_id=project_id,
        workspace_release_id=release.release_id,
        environment_id=env_id,
        kind=kind,
        idempotency_key=key,
        expected_environment_lock_version=expected_lock,
        expected_current_release_id=expected_current,
        target_release_id=target,
        requested_by="admin",
        adapters=REGISTRY,
    )


def test_prepare_builds_workflow_refs_and_promote_action(db_session: Session) -> None:
    version_id = _seed_workflow(db_session)
    _seed_project_and_environment(db_session)
    release = _seed_release(
        db_session,
        version_id=version_id,
        revision_id="WSR-p5e-1",
        resource_pin_id="WSRR-p5e-1",
        release_key="release-1",
    )

    prepared = _prepare(db_session, release, key="op-1")
    assert prepared.operation.status == "prepared"
    assert len(prepared.items) == 1
    item = prepared.items[0]
    assert item.action == "promote"
    assert item.target_ref == f"workflow:{WORKFLOW_ID}@prod"
    assert item.before_ref is None
    assert item.after_ref == version_id
    # `P5-E`: the metadata-rehydration fix -- prepare persisted this, and it
    # must survive being read back for apply/observe/rollback.
    assert item.provider_result["workflow_id"] == WORKFLOW_ID
    assert item.provider_result["project_id"] == PROJECT_ID


def test_apply_actually_rotates_the_deployment_alias(db_session: Session) -> None:
    version_id = _seed_workflow(db_session)
    _seed_project_and_environment(db_session)
    release = _seed_release(
        db_session,
        version_id=version_id,
        revision_id="WSR-p5e-1",
        resource_pin_id="WSRR-p5e-1",
        release_key="release-1",
    )
    prepared = _prepare(db_session, release, key="op-1")

    result = apply_workspace_release_operation(
        db_session, prepared.operation.operation_id, adapters=REGISTRY, actor="admin"
    )
    assert result.operation.status == "applied"
    assert all(item.status == "applied" for item in result.items)

    deployment = db_session.execute(
        select(CaliberWorkflowDeployment).where(
            CaliberWorkflowDeployment.workflow_id == WORKFLOW_ID,
            CaliberWorkflowDeployment.alias == "prod",
        )
    ).scalar_one()
    assert deployment.version_id == version_id

    environment = db_session.get(CaliberWorkspaceEnvironment, ENV_ID)
    assert environment is not None
    assert environment.current_release_id == release.release_id


def test_second_release_rotates_again_and_records_a_rollback_checkpoint(
    db_session: Session,
) -> None:
    v1 = _seed_workflow(db_session, version_id="WFV-p5e-1", version_number=1)
    _seed_project_and_environment(db_session)
    release1 = _seed_release(
        db_session,
        version_id=v1,
        revision_id="WSR-p5e-1",
        resource_pin_id="WSRR-p5e-1",
        release_key="release-1",
    )
    prepared1 = _prepare(db_session, release1, key="op-1")
    apply_workspace_release_operation(
        db_session, prepared1.operation.operation_id, adapters=REGISTRY, actor="admin"
    )
    environment = db_session.get(CaliberWorkspaceEnvironment, ENV_ID)
    assert environment is not None
    db_session.refresh(environment)

    v2 = _seed_workflow(db_session, version_id="WFV-p5e-2", version_number=2)
    release2 = _seed_release(
        db_session,
        version_id=v2,
        revision_id="WSR-p5e-2",
        resource_pin_id="WSRR-p5e-2",
        release_key="release-2",
        revision_number=2,
    )
    prepared2 = _prepare(
        db_session,
        release2,
        key="op-2",
        expected_current=release1.release_id,
        expected_lock=environment.lock_version,
    )
    assert prepared2.items[0].before_ref == v1
    assert prepared2.items[0].after_ref == v2
    assert prepared2.items[0].action == "promote"

    result = apply_workspace_release_operation(
        db_session, prepared2.operation.operation_id, adapters=REGISTRY, actor="admin"
    )
    assert result.operation.status == "applied"

    deployment = db_session.execute(
        select(CaliberWorkflowDeployment).where(
            CaliberWorkflowDeployment.workflow_id == WORKFLOW_ID,
            CaliberWorkflowDeployment.alias == "prod",
        )
    ).scalar_one()
    assert deployment.version_id == v2
    assert deployment.rollback_checkpoint
    assert deployment.rollback_checkpoint[-1]["version_id"] == v1


def test_rollback_restores_the_exact_prior_version(db_session: Session) -> None:
    v1 = _seed_workflow(db_session, version_id="WFV-p5e-1", version_number=1)
    _seed_project_and_environment(db_session)
    release1 = _seed_release(
        db_session,
        version_id=v1,
        revision_id="WSR-p5e-1",
        resource_pin_id="WSRR-p5e-1",
        release_key="release-1",
    )
    prepared1 = _prepare(db_session, release1, key="op-1")
    apply_workspace_release_operation(
        db_session, prepared1.operation.operation_id, adapters=REGISTRY, actor="admin"
    )
    environment = db_session.get(CaliberWorkspaceEnvironment, ENV_ID)
    assert environment is not None
    db_session.refresh(environment)

    v2 = _seed_workflow(db_session, version_id="WFV-p5e-2", version_number=2)
    release2 = _seed_release(
        db_session,
        version_id=v2,
        revision_id="WSR-p5e-2",
        resource_pin_id="WSRR-p5e-2",
        release_key="release-2",
        revision_number=2,
    )
    prepared2 = _prepare(
        db_session,
        release2,
        key="op-2",
        expected_current=release1.release_id,
        expected_lock=environment.lock_version,
    )
    apply_workspace_release_operation(
        db_session, prepared2.operation.operation_id, adapters=REGISTRY, actor="admin"
    )
    db_session.refresh(environment)

    rollback_prepared = _prepare(
        db_session,
        release2,
        key="op-rollback",
        kind="rollback",
        target=release1.release_id,
        expected_current=release2.release_id,
        expected_lock=environment.lock_version,
    )
    assert rollback_prepared.items[0].after_ref == v1
    result = apply_workspace_release_operation(
        db_session, rollback_prepared.operation.operation_id, adapters=REGISTRY, actor="admin"
    )
    assert result.operation.status == "applied"

    deployment = db_session.execute(
        select(CaliberWorkflowDeployment).where(
            CaliberWorkflowDeployment.workflow_id == WORKFLOW_ID,
            CaliberWorkflowDeployment.alias == "prod",
        )
    ).scalar_one()
    assert deployment.version_id == v1
    environment = db_session.get(CaliberWorkspaceEnvironment, ENV_ID)
    assert environment is not None
    assert environment.current_release_id == release1.release_id


def test_no_op_when_before_and_after_refs_already_match(db_session: Session) -> None:
    """A release re-declaring the alias's already-deployed version must not
    re-run preflight or touch the deployment row's rollback_checkpoint."""
    version_id = _seed_workflow(db_session)
    _seed_project_and_environment(db_session)
    release1 = _seed_release(
        db_session,
        version_id=version_id,
        revision_id="WSR-p5e-1",
        resource_pin_id="WSRR-p5e-1",
        release_key="release-1",
    )
    prepared1 = _prepare(db_session, release1, key="op-1")
    apply_workspace_release_operation(
        db_session, prepared1.operation.operation_id, adapters=REGISTRY, actor="admin"
    )
    environment = db_session.get(CaliberWorkspaceEnvironment, ENV_ID)
    assert environment is not None
    db_session.refresh(environment)

    release2 = _seed_release(
        db_session,
        version_id=version_id,
        revision_id="WSR-p5e-2",
        resource_pin_id="WSRR-p5e-2",
        release_key="release-2",
        revision_number=2,
    )
    prepared2 = _prepare(
        db_session,
        release2,
        key="op-2",
        expected_current=release1.release_id,
        expected_lock=environment.lock_version,
    )
    assert prepared2.items[0].action == "no_op"
    result = apply_workspace_release_operation(
        db_session, prepared2.operation.operation_id, adapters=REGISTRY, actor="admin"
    )
    assert result.operation.status == "applied"
    deployment = db_session.execute(
        select(CaliberWorkflowDeployment).where(
            CaliberWorkflowDeployment.workflow_id == WORKFLOW_ID,
            CaliberWorkflowDeployment.alias == "prod",
        )
    ).scalar_one()
    assert len(deployment.rollback_checkpoint) == 0


def test_apply_fails_closed_when_the_target_version_has_a_configured_deploy_gate(
    db_session: Session,
) -> None:
    version_id = _seed_workflow(db_session, gated_for=["prod"])
    _seed_project_and_environment(db_session)
    release = _seed_release(
        db_session,
        version_id=version_id,
        revision_id="WSR-p5e-1",
        resource_pin_id="WSRR-p5e-1",
        release_key="release-1",
    )
    prepared = _prepare(db_session, release, key="op-1")

    result = apply_workspace_release_operation(
        db_session, prepared.operation.operation_id, adapters=REGISTRY, actor="admin"
    )
    assert result.operation.status == "failed"
    assert result.items[0].status == "failed"
    assert result.items[0].error_code == "deploy_gate_not_evaluated"

    assert (
        db_session.execute(
            select(CaliberWorkflowDeployment).where(
                CaliberWorkflowDeployment.workflow_id == WORKFLOW_ID,
                CaliberWorkflowDeployment.alias == "prod",
            )
        ).scalar_one_or_none()
        is None
    )
    environment = db_session.get(CaliberWorkspaceEnvironment, ENV_ID)
    assert environment is not None
    assert environment.current_release_id is None


def test_apply_refuses_a_workflow_pin_outside_the_release_project(db_session: Session) -> None:
    """A crafted pin naming a workflow from a different project must not
    deploy cross-project, even though `provider_ref`/`version_ref` alone
    would otherwise look like a perfectly valid target."""
    version_id = _seed_workflow(db_session, project_id=OTHER_PROJECT_ID)
    _seed_project_and_environment(db_session)
    release = _seed_release(
        db_session,
        version_id=version_id,
        revision_id="WSR-p5e-1",
        resource_pin_id="WSRR-p5e-1",
        release_key="release-1",
    )
    prepared = _prepare(db_session, release, key="op-1")

    result = apply_workspace_release_operation(
        db_session, prepared.operation.operation_id, adapters=REGISTRY, actor="admin"
    )
    assert result.operation.status == "failed"
    assert result.items[0].error_code == "resource_outside_project"


def test_observe_release_reads_real_deployment_state(db_session: Session) -> None:
    """Directly exercises observe_release's read-the-real-state contract,
    independent of how an item ended up needing observation."""
    version_id = _seed_workflow(db_session)
    db_session.commit()
    prepared_action = PreparedAction(
        resource_pin_id="WSRR-direct",
        resource_type="workflow",
        action="promote",
        target_ref=f"workflow:{WORKFLOW_ID}@prod",
        before_ref=None,
        after_ref=version_id,
        metadata={"workflow_id": WORKFLOW_ID, "project_id": PROJECT_ID},
    )

    not_yet = ADAPTER.observe_release(db_session, prepared_action)
    assert not_yet.status == "failed"
    assert not_yet.error_code == "deployment_not_found"

    outcome = ADAPTER.apply_release(db_session, prepared_action)
    assert outcome.status == "applied"

    settled = ADAPTER.observe_release(db_session, prepared_action)
    assert settled.status == "applied"
    assert settled.provider_result["version_id"] == version_id


def test_validate_refuses_a_configured_gate(db_session: Session) -> None:
    version_id = _seed_workflow(db_session, gated_for=["prod"])
    environment = CaliberWorkspaceEnvironment(
        environment_id="WSE-validate",
        project_id=PROJECT_ID,
        name="prod",
        environment_class="production",
        promotion_order=40,
        status="active",
        created_by="admin",
    )
    pin = CaliberWorkspaceRevisionResource(
        resource_pin_id="WSRR-validate",
        revision_id="WSR-validate",
        resource_type="workflow",
        logical_name="main",
        resource_id=WORKFLOW_ID,
        version_ref=version_id,
        content_sha256=HEX,
        provider_ref=version_id,
        purpose="runtime",
        resolution={},
    )

    with pytest.raises(WorkspaceReleaseAdapterError, match="deploy gate"):
        ADAPTER.validate(db_session, pin, environment)


def test_validate_refuses_a_workflow_outside_the_environments_project(db_session: Session) -> None:
    version_id = _seed_workflow(db_session, project_id=OTHER_PROJECT_ID)
    environment = CaliberWorkspaceEnvironment(
        environment_id="WSE-validate-2",
        project_id=PROJECT_ID,
        name="prod",
        environment_class="production",
        promotion_order=40,
        status="active",
        created_by="admin",
    )
    pin = CaliberWorkspaceRevisionResource(
        resource_pin_id="WSRR-validate-2",
        revision_id="WSR-validate-2",
        resource_type="workflow",
        logical_name="main",
        resource_id=WORKFLOW_ID,
        version_ref=version_id,
        content_sha256=HEX,
        provider_ref=version_id,
        purpose="runtime",
        resolution={},
    )

    with pytest.raises(WorkspaceReleaseAdapterError, match="project"):
        ADAPTER.validate(db_session, pin, environment)
