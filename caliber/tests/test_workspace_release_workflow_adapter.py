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

from caliber.auth import SCOPE_ADMIN, CaliberIdentity
from caliber.db.models import (
    CaliberProject,
    CaliberProjectMember,
    CaliberWorkflow,
    CaliberWorkflowDeployment,
    CaliberWorkflowVersion,
    CaliberWorkspaceEnvironment,
    CaliberWorkspaceRelease,
    CaliberWorkspaceRevision,
    CaliberWorkspaceRevisionResource,
)
from caliber.workflows.manifest import compute_manifest_hash
from caliber.workspace_release_adapters import (
    PreparedAction,
    SnapshotPin,
    WorkspaceReleaseAdapterError,
    WorkspaceResourceAdapterRegistry,
)
from caliber.workspace_release_operations import (
    apply_workspace_release_operation,
    prepare_workspace_release_operation,
)
from caliber.workspace_release_service import create_workspace_release
from caliber.workspace_release_workflow_adapter import (
    WORKFLOW_ADAPTER_VERSION,
    ResolvedWorkflowVersion,
    WorkflowWorkspaceResourceAdapter,
)
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


def _identity(
    user_id: str = "@caller", *, active_project_id: str | None = None, admin: bool = False
) -> CaliberIdentity:
    return CaliberIdentity(
        user_id=user_id,
        scopes=frozenset({SCOPE_ADMIN}) if admin else frozenset(),
        active_project_id=active_project_id,
    )


# -- resolve --------------------------------------------------------------
# `P4-B`/`P4-C`'s managed-snapshot epic's seventh and final slice: proving
# `resolve()`/`snapshot()` are real now, mirroring
# `test_workspace_release_skill_adapter.py`'s coverage shape (the closest
# structural precedent -- a mutable parent row plus an immutable, numbered
# content history).


def test_resolve_loads_the_exact_published_version_and_its_manifest(
    db_session: Session,
) -> None:
    version_id = _seed_workflow(db_session, project_id=PROJECT_ID)
    resolved = ADAPTER.resolve(
        db_session,
        None,
        {"resource_id": WORKFLOW_ID, "version_ref": version_id},
        _identity("developer", active_project_id=PROJECT_ID),
    )
    assert isinstance(resolved, ResolvedWorkflowVersion)
    assert resolved.workflow_id == WORKFLOW_ID
    assert resolved.version_id == version_id
    assert resolved.version_number == 1
    assert resolved.manifest["workflow_id"] == WORKFLOW_ID
    assert resolved.provider_ref == f"caliber-workflow-version:/{WORKFLOW_ID}/{version_id}"


def test_resolve_loads_an_older_pinned_version_not_just_the_latest(db_session: Session) -> None:
    v1 = _seed_workflow(db_session, version_id="WFV-resolve-1", version_number=1)
    v2 = _seed_workflow(db_session, version_id="WFV-resolve-2", version_number=2)
    resolved = ADAPTER.resolve(
        db_session,
        None,
        {"resource_id": WORKFLOW_ID, "version_ref": v1},
        _identity("developer", active_project_id=PROJECT_ID),
    )
    assert resolved.version_id == v1
    assert resolved.version_number == 1
    assert resolved.version_id != v2


def test_resolve_requires_resource_id_and_version_ref(db_session: Session) -> None:
    identity = _identity()
    with pytest.raises(WorkspaceReleaseAdapterError, match="requires"):
        ADAPTER.resolve(db_session, None, {"resource_id": "x"}, identity)
    with pytest.raises(WorkspaceReleaseAdapterError, match="requires"):
        ADAPTER.resolve(db_session, None, {}, identity)
    with pytest.raises(WorkspaceReleaseAdapterError):
        ADAPTER.resolve(db_session, None, "not-a-mapping", identity)  # type: ignore[arg-type]


def test_resolve_raises_when_the_workflow_does_not_exist(db_session: Session) -> None:
    with pytest.raises(WorkspaceReleaseAdapterError, match="not found or not visible"):
        ADAPTER.resolve(
            db_session, None, {"resource_id": "wf-ghost", "version_ref": "WFV-ghost"}, _identity()
        )


def test_resolve_raises_when_the_version_does_not_exist(db_session: Session) -> None:
    _seed_workflow(db_session)
    with pytest.raises(WorkspaceReleaseAdapterError, match="no recorded version"):
        ADAPTER.resolve(
            db_session,
            None,
            {"resource_id": WORKFLOW_ID, "version_ref": "WFV-ghost"},
            _identity("developer", active_project_id=PROJECT_ID),
        )


def test_resolve_refuses_a_version_belonging_to_a_different_workflow(db_session: Session) -> None:
    """A crafted declaration mixing one workflow's id with another workflow's
    real version_id must not resolve -- the version row is only ever loaded
    filtered by *this* workflow_id, not by a bare primary-key lookup."""
    v1 = _seed_workflow(db_session, workflow_id=WORKFLOW_ID, version_id="WFV-mix-1")
    db_session.add(
        CaliberWorkflow(
            workflow_id="wf-other-mix",
            name="A different workflow",
            owner="developer",
            status="active",
            project_id=PROJECT_ID,
            visibility="project",
        )
    )
    db_session.add(
        CaliberWorkflowVersion(
            version_id="WFV-mix-2",
            workflow_id="wf-other-mix",
            version_number=1,
            status="published",
            manifest=make_manifest("wf-other-mix"),
            manifest_hash=HEX,
            created_by="developer",
        )
    )
    db_session.flush()
    with pytest.raises(WorkspaceReleaseAdapterError, match="no recorded version"):
        ADAPTER.resolve(
            db_session,
            None,
            {"resource_id": "wf-other-mix", "version_ref": v1},
            _identity("developer", active_project_id=PROJECT_ID),
        )


def test_resolve_refuses_a_draft_version(db_session: Session) -> None:
    workflow_id = "wf-draft"
    db_session.add(
        CaliberWorkflow(
            workflow_id=workflow_id,
            name="Draft workflow",
            owner="developer",
            status="active",
            project_id=PROJECT_ID,
            visibility="project",
        )
    )
    db_session.add(
        CaliberWorkflowVersion(
            version_id="WFV-draft-1",
            workflow_id=workflow_id,
            version_number=1,
            status="draft",
            manifest=make_manifest(workflow_id),
            manifest_hash=HEX,
            created_by="developer",
        )
    )
    db_session.flush()
    with pytest.raises(WorkspaceReleaseAdapterError, match="only a published version"):
        ADAPTER.resolve(
            db_session,
            None,
            {"resource_id": workflow_id, "version_ref": "WFV-draft-1"},
            _identity("developer", active_project_id=PROJECT_ID),
        )


def test_resolve_refuses_a_workflow_bound_to_a_different_project(db_session: Session) -> None:
    version_id = _seed_workflow(db_session, project_id=OTHER_PROJECT_ID)
    caller = _identity("@caller", active_project_id=PROJECT_ID)
    with pytest.raises(WorkspaceReleaseAdapterError, match="not found or not visible"):
        ADAPTER.resolve(
            db_session, None, {"resource_id": WORKFLOW_ID, "version_ref": version_id}, caller
        )


def test_resolve_allows_a_project_scoped_workflow_for_an_active_member(
    db_session: Session,
) -> None:
    """The positive side of the project-tier check: a caller who is an
    active member of the target's own project (not its owner) can still
    resolve it -- proving this reuses the *real* visibility model
    (owner-or-member), not a stricter "owner only" shortcut."""
    version_id = _seed_workflow(db_session, project_id=PROJECT_ID)
    db_session.add(
        CaliberProjectMember(
            member_id="PM-workflow-adapter-1",
            project_id=PROJECT_ID,
            user_id="@teammate",
            role="editor",
            status="active",
            created_by="developer",
        )
    )
    db_session.flush()
    member = _identity("@teammate", active_project_id=PROJECT_ID)
    resolved = ADAPTER.resolve(
        db_session, None, {"resource_id": WORKFLOW_ID, "version_ref": version_id}, member
    )
    assert resolved.workflow_id == WORKFLOW_ID


def test_resolve_refuses_another_users_unshared_personal_workflow(db_session: Session) -> None:
    """A personal workflow (``project_id=None``, ``visibility="user"``) has
    the *same* ``None`` project id as a public workflow, so a bare
    ``workflow.project_id != caller's project`` comparison could not tell
    them apart and would let any operator snapshot another user's unshared
    personal workflow by id. Must be refused, the same way
    ``routes/workflows.py``'s lookup routes already refuse it."""
    workflow_id = "wf-personal"
    db_session.add(
        CaliberWorkflow(
            workflow_id=workflow_id,
            name="Personal workflow",
            owner="@someone-else",
            status="active",
            project_id=None,
            visibility="user",
        )
    )
    db_session.add(
        CaliberWorkflowVersion(
            version_id="WFV-personal-1",
            workflow_id=workflow_id,
            version_number=1,
            status="published",
            manifest=make_manifest(workflow_id),
            manifest_hash=HEX,
            created_by="@someone-else",
        )
    )
    db_session.flush()
    other_user = _identity("@a-different-user", active_project_id=PROJECT_ID)
    with pytest.raises(WorkspaceReleaseAdapterError, match="not found or not visible"):
        ADAPTER.resolve(
            db_session,
            None,
            {"resource_id": workflow_id, "version_ref": "WFV-personal-1"},
            other_user,
        )


def test_resolve_allows_the_owners_own_personal_workflow(db_session: Session) -> None:
    workflow_id = "wf-mine"
    db_session.add(
        CaliberWorkflow(
            workflow_id=workflow_id,
            name="My workflow",
            owner="@owner-self",
            status="active",
            project_id=None,
            visibility="user",
        )
    )
    db_session.add(
        CaliberWorkflowVersion(
            version_id="WFV-mine-1",
            workflow_id=workflow_id,
            version_number=1,
            status="published",
            manifest=make_manifest(workflow_id),
            manifest_hash=HEX,
            created_by="@owner-self",
        )
    )
    db_session.flush()
    owner = _identity("@owner-self", active_project_id=None)
    resolved = ADAPTER.resolve(
        db_session, None, {"resource_id": workflow_id, "version_ref": "WFV-mine-1"}, owner
    )
    assert resolved.workflow_id == workflow_id


def test_resolve_allows_a_public_workflow_for_any_caller(db_session: Session) -> None:
    """A genuinely public workflow also has ``project_id=None`` -- proving
    this allows that tier too, not just refusing everything with a ``None``
    project id."""
    workflow_id = "wf-public"
    db_session.add(
        CaliberWorkflow(
            workflow_id=workflow_id,
            name="Public workflow",
            owner="@publisher",
            status="active",
            project_id=None,
            visibility="public",
        )
    )
    db_session.add(
        CaliberWorkflowVersion(
            version_id="WFV-public-1",
            workflow_id=workflow_id,
            version_number=1,
            status="published",
            manifest=make_manifest(workflow_id),
            manifest_hash=HEX,
            created_by="@publisher",
        )
    )
    db_session.flush()
    stranger = _identity("@total-stranger", active_project_id=PROJECT_ID)
    resolved = ADAPTER.resolve(
        db_session, None, {"resource_id": workflow_id, "version_ref": "WFV-public-1"}, stranger
    )
    assert resolved.workflow_id == workflow_id


def test_resolve_admin_bypasses_visibility(db_session: Session) -> None:
    version_id = _seed_workflow(db_session, project_id=OTHER_PROJECT_ID)
    admin = _identity("@platform-admin", active_project_id=PROJECT_ID, admin=True)
    resolved = ADAPTER.resolve(
        db_session, None, {"resource_id": WORKFLOW_ID, "version_ref": version_id}, admin
    )
    assert resolved.workflow_id == WORKFLOW_ID


# -- snapshot ---------------------------------------------------------------


def test_snapshot_computes_a_genuine_content_digest() -> None:
    manifest = make_manifest(WORKFLOW_ID)
    resolved = ResolvedWorkflowVersion(
        workflow_id=WORKFLOW_ID,
        name="P5-E workflow",
        version_id="WFV-snap-1",
        version_number=3,
        manifest=manifest,
        provider_ref=f"caliber-workflow-version:/{WORKFLOW_ID}/WFV-snap-1",
    )
    pin = ADAPTER.snapshot(None, resolved)
    assert isinstance(pin, SnapshotPin)
    assert pin.resource_id == WORKFLOW_ID
    assert pin.version_ref == "WFV-snap-1"
    assert pin.content_sha256 == compute_manifest_hash(manifest)
    assert pin.provider_ref == f"caliber-workflow-version:/{WORKFLOW_ID}/WFV-snap-1"
    assert pin.resolution["strategy"] == "live_resource_adapter"
    assert pin.resolution["adapter_version"] == WORKFLOW_ADAPTER_VERSION
    assert pin.resolution["version_number"] == 3


def test_snapshot_distinguishes_different_manifests() -> None:
    a = ADAPTER.snapshot(
        None,
        ResolvedWorkflowVersion(
            WORKFLOW_ID, "wf", "WFV-a", 1, make_manifest(WORKFLOW_ID, name="A"), "ref"
        ),
    )
    b = ADAPTER.snapshot(
        None,
        ResolvedWorkflowVersion(
            WORKFLOW_ID, "wf", "WFV-b", 1, make_manifest(WORKFLOW_ID, name="B"), "ref"
        ),
    )
    assert a.content_sha256 != b.content_sha256


def test_snapshot_is_key_order_independent() -> None:
    """`compute_manifest_hash` normalizes a manifest through `parse_manifest`
    before hashing, so two dicts differing only in key order (or with the
    same defaults spelled out explicitly) hash identically -- the same
    property every workflow route already relies on."""
    manifest = make_manifest(WORKFLOW_ID)
    reordered = dict(reversed(list(manifest.items())))
    a = ADAPTER.snapshot(
        None, ResolvedWorkflowVersion(WORKFLOW_ID, "wf", "WFV-a", 1, manifest, "ref")
    )
    b = ADAPTER.snapshot(
        None, ResolvedWorkflowVersion(WORKFLOW_ID, "wf", "WFV-b", 1, reordered, "ref")
    )
    assert a.content_sha256 == b.content_sha256


def test_snapshot_rejects_a_non_resolved_input() -> None:
    with pytest.raises(WorkspaceReleaseAdapterError, match="resolved workflow version"):
        ADAPTER.snapshot(None, {"not": "resolved"})


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
