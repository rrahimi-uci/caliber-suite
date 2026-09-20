"""`P4-B`/`P4-C` contract tests for ``POST /projects/{id}/revisions:snapshot``
against the ``workflow`` resource type specifically -- the managed-snapshot
epic's seventh and final resource-type slice.

Mirrors ``test_workspace_revisions_snapshot_skill.py``'s route-test style (a
real ``client`` fixture wired to ``server.py::create_app``, so
``app.state.workspace_resource_adapter_registry`` already carries the real
``workflow`` adapter exactly as production does) -- kept in its own file
rather than appended to ``test_workspace_revisions_snapshot.py`` for the same
reason every other resource-type slice's route test is separate.
"""

from __future__ import annotations

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberProject,
    CaliberProjectMember,
    CaliberWorkflow,
    CaliberWorkflowVersion,
)
from caliber.workflows.manifest import compute_manifest_hash
from tests.workflow_helpers import make_manifest

PREFIX = "/ajax-api/2.0/mlflow/caliber"
HEX = "a" * 64


def _create_project(client: TestClient, name: str = "Snapshot workflow workspace") -> str:
    response = client.post(f"{PREFIX}/projects", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["data"]["project_id"]


def _seed_workflow(
    session: Session,
    *,
    workflow_id: str,
    version_id: str,
    project_id: str | None,
    owner: str = "@test",
    visibility: str | None = None,
    status: str = "published",
    version_number: int = 1,
    manifest: dict[str, object] | None = None,
) -> CaliberWorkflow:
    workflow = CaliberWorkflow(
        workflow_id=workflow_id,
        name=workflow_id,
        description="",
        owner=owner,
        project_id=project_id,
        visibility=visibility or ("project" if project_id else "user"),
        status="active",
    )
    session.add(workflow)
    session.add(
        CaliberWorkflowVersion(
            version_id=version_id,
            workflow_id=workflow_id,
            version_number=version_number,
            status=status,
            manifest=manifest if manifest is not None else make_manifest(workflow_id),
            manifest_hash=HEX,
            created_by=owner,
        )
    )
    session.commit()
    return workflow


def _snapshot_body(**overrides: object) -> dict[str, object]:
    resource: dict[str, object] = {
        "resource_type": "workflow",
        "resource_id": "wf-support",
        "version_ref": "WFV-support-1",
    }
    resource.update(overrides)
    return {"resources": [resource]}


def test_snapshot_pins_a_live_workflow_version_and_is_idempotent_by_content(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client)
    _seed_workflow(
        db_session, workflow_id="wf-support", version_id="WFV-support-1", project_id=project_id
    )
    body = _snapshot_body()

    first = client.post(f"{PREFIX}/projects/{project_id}/revisions:snapshot", json=body)
    assert first.status_code == 201, first.text
    data = first.json()["data"]
    assert data["project_id"] == project_id
    assert data["source_kind"] == "managed"
    assert data["status"] == "ready"
    assert len(data["resources"]) == 1
    pin = data["resources"][0]
    assert pin["resource_type"] == "workflow"
    assert pin["resource_id"] == "wf-support"
    assert pin["logical_name"] == "wf-support"
    assert pin["version_ref"] == "WFV-support-1"
    assert pin["provider_ref"] == "caliber-workflow-version:/wf-support/WFV-support-1"
    assert pin["purpose"] == "runtime"
    assert pin["resolution"]["strategy"] == "live_resource_adapter"
    expected_sha = compute_manifest_hash(make_manifest("wf-support"))
    assert pin["content_sha256"] == expected_sha

    # Idempotent by content: an identical retry converges on the same
    # revision rather than creating a duplicate.
    second = client.post(f"{PREFIX}/projects/{project_id}/revisions:snapshot", json=body)
    assert second.status_code == 201, second.text
    assert second.json()["data"]["revision_id"] == data["revision_id"]

    listing = client.get(f"{PREFIX}/projects/{project_id}/revisions")
    assert listing.status_code == 200
    assert len(listing.json()["data"]["items"]) == 1


def test_snapshot_honors_an_explicit_logical_name_and_purpose(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client)
    _seed_workflow(
        db_session, workflow_id="wf-support", version_id="WFV-support-1", project_id=project_id
    )
    body = _snapshot_body(logical_name="triage-workflow", purpose="eval")

    response = client.post(f"{PREFIX}/projects/{project_id}/revisions:snapshot", json=body)
    assert response.status_code == 201, response.text
    pin = response.json()["data"]["resources"][0]
    assert pin["logical_name"] == "triage-workflow"
    assert pin["purpose"] == "eval"


def test_snapshot_a_different_workflow_produces_a_distinct_revision(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client)
    _seed_workflow(
        db_session,
        workflow_id="wf-1",
        version_id="WFV-1-1",
        project_id=project_id,
        manifest=make_manifest("wf-1", name="Workflow One"),
    )
    _seed_workflow(
        db_session,
        workflow_id="wf-2",
        version_id="WFV-2-1",
        project_id=project_id,
        manifest=make_manifest("wf-2", name="Workflow Two"),
    )
    first = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        json=_snapshot_body(resource_id="wf-1", version_ref="WFV-1-1"),
    )
    second = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        json=_snapshot_body(resource_id="wf-2", version_ref="WFV-2-1"),
    )
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["data"]["revision_id"] != second.json()["data"]["revision_id"]
    assert first.json()["data"]["revision_number"] + 1 == second.json()["data"]["revision_number"]


def test_snapshot_pins_an_older_version_distinctly_from_a_newer_one(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client)
    _seed_workflow(
        db_session,
        workflow_id="wf-support",
        version_id="WFV-support-1",
        project_id=project_id,
        manifest=make_manifest("wf-support", name="v1"),
    )
    db_session.add(
        CaliberWorkflowVersion(
            version_id="WFV-support-2",
            workflow_id="wf-support",
            version_number=2,
            status="published",
            manifest=make_manifest("wf-support", name="v2"),
            manifest_hash=HEX,
            created_by="@test",
        )
    )
    db_session.commit()

    old = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        json=_snapshot_body(version_ref="WFV-support-1"),
    )
    new = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        json=_snapshot_body(version_ref="WFV-support-2"),
    )
    assert old.status_code == 201, old.text
    assert new.status_code == 201, new.text
    assert (
        old.json()["data"]["resources"][0]["content_sha256"]
        != new.json()["data"]["resources"][0]["content_sha256"]
    )


def test_snapshot_refuses_a_missing_workflow(client: TestClient) -> None:
    project_id = _create_project(client)
    response = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        json=_snapshot_body(resource_id="wf-ghost", version_ref="WFV-ghost-1"),
    )
    assert response.status_code == 409
    assert "resource_resolve_failed" in response.json()["detail"]


def test_snapshot_refuses_a_version_not_recorded_for_the_workflow(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client)
    _seed_workflow(
        db_session, workflow_id="wf-support", version_id="WFV-support-1", project_id=project_id
    )
    response = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        json=_snapshot_body(version_ref="WFV-ghost"),
    )
    assert response.status_code == 409
    assert "resource_resolve_failed" in response.json()["detail"]


def test_snapshot_refuses_a_draft_version(client: TestClient, db_session: Session) -> None:
    project_id = _create_project(client)
    _seed_workflow(
        db_session,
        workflow_id="wf-support",
        version_id="WFV-support-1",
        project_id=project_id,
        status="draft",
    )
    response = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot", json=_snapshot_body()
    )
    assert response.status_code == 409
    assert "resource_resolve_failed" in response.json()["detail"]


def test_snapshot_refuses_a_workflow_bound_to_a_different_project(
    client: TestClient, db_session: Session
) -> None:
    """The default test identity (``@test``) is a platform admin (see
    ``conftest.py::app_config``), and an admin bypasses the 3-tier
    visibility model everywhere in this codebase -- so proving this refusal
    for real needs a genuinely non-admin caller: one who has the
    ``caliber.operator`` platform scope (to reach the route at all) and an
    editor role on the *calling* project, but is neither a member of the
    *other* project nor the workflow's owner. Mirrors
    ``test_workspace_revisions_snapshot_skill.py``'s equivalent test."""
    project_id = _create_project(client, "Snapshot workflow owner")
    other_project_id = _create_project(client, "Snapshot workflow other")
    _seed_workflow(
        db_session,
        workflow_id="wf-owned-elsewhere",
        version_id="WFV-owned-elsewhere-1",
        project_id=other_project_id,
    )
    db_session.add(
        CaliberProjectMember(
            member_id="PM-snapshot-workflow-operator",
            project_id=project_id,
            user_id="@snapshot-workflow-operator",
            role="editor",
            status="active",
            created_by="@test",
        )
    )
    db_session.commit()
    client.app.state.config = client.app.state.config.model_copy(
        update={"operator_users": "@snapshot-workflow-operator"}
    )
    response = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        headers={
            "X-CALIBER-User": "@snapshot-workflow-operator",
            "X-CALIBER-Project": project_id,
        },
        json=_snapshot_body(resource_id="wf-owned-elsewhere", version_ref="WFV-owned-elsewhere-1"),
    )
    assert response.status_code == 409, response.text
    assert "resource_resolve_failed" in response.json()["detail"]


def test_snapshot_allows_a_public_workflow_for_a_different_project_caller(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client, "Snapshot workflow caller")
    _seed_workflow(
        db_session,
        workflow_id="wf-shared",
        version_id="WFV-shared-1",
        project_id=None,
        owner="@publisher",
        visibility="public",
    )
    response = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        json=_snapshot_body(resource_id="wf-shared", version_ref="WFV-shared-1"),
    )
    assert response.status_code == 201, response.text


def test_snapshot_refuses_archived_workspace(client: TestClient, db_session: Session) -> None:
    project_id = _create_project(client)
    _seed_workflow(
        db_session, workflow_id="wf-support", version_id="WFV-support-1", project_id=project_id
    )
    project = db_session.get(CaliberProject, project_id)
    assert project is not None
    project.status = "archived"
    db_session.commit()

    response = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot", json=_snapshot_body()
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "archived_workspace"
