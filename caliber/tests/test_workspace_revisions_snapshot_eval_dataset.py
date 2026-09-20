"""`P4-B`/`P4-C` contract tests for ``POST /projects/{id}/revisions:snapshot``
against the ``eval_dataset`` resource type specifically.

Mirrors ``test_workspace_revisions_snapshot_judge.py``'s route-test style (a
real ``client`` fixture wired to ``server.py::create_app``, so
``app.state.workspace_resource_adapter_registry`` already carries the real
``eval_dataset`` adapter exactly as production does) -- kept in its own file
rather than appended to that one so this slice and any concurrently-landing
sibling resource-type slice (e.g. ``skill``/``mcp_server``) don't both edit
the same large test file.
"""

from __future__ import annotations

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberEvalDataset,
    CaliberEvalDatasetExample,
    CaliberProject,
    CaliberProjectMember,
)
from caliber.workspace_release_eval_dataset_adapter import _content_sha256

PREFIX = "/ajax-api/2.0/mlflow/caliber"


def _create_project(client: TestClient, name: str = "Snapshot eval-dataset workspace") -> str:
    response = client.post(f"{PREFIX}/projects", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["data"]["project_id"]


def _seed_dataset(
    session: Session,
    *,
    dataset_id: str,
    project_id: str | None,
    owner: str = "@test",
    visibility: str | None = None,
    version: int = 1,
) -> CaliberEvalDataset:
    dataset = CaliberEvalDataset(
        dataset_id=dataset_id,
        name=dataset_id,
        description="",
        owner=owner,
        project_id=project_id,
        visibility=visibility or ("project" if project_id else "user"),
        tags=[],
        status="active",
        version=version,
    )
    session.add(dataset)
    session.commit()
    return dataset


def _add_example(
    session: Session,
    *,
    example_id: str,
    dataset_id: str,
    dataset_version: int,
) -> CaliberEvalDatasetExample:
    example = CaliberEvalDatasetExample(
        example_id=example_id,
        dataset_id=dataset_id,
        dataset_version=dataset_version,
        input={"q": example_id},
        expected={"a": example_id},
        weight=1.0,
        tags=[],
    )
    session.add(example)
    session.commit()
    return example


def _snapshot_body(**overrides: object) -> dict[str, object]:
    resource: dict[str, object] = {
        "resource_type": "eval_dataset",
        "resource_id": "ED-support",
        "version_ref": "1",
    }
    resource.update(overrides)
    return {"resources": [resource]}


def test_snapshot_pins_a_live_dataset_and_is_idempotent_by_content(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client)
    _seed_dataset(db_session, dataset_id="ED-support", project_id=project_id, version=1)
    _add_example(db_session, example_id="EX-1", dataset_id="ED-support", dataset_version=1)
    body = _snapshot_body()

    first = client.post(f"{PREFIX}/projects/{project_id}/revisions:snapshot", json=body)
    assert first.status_code == 201, first.text
    data = first.json()["data"]
    assert data["project_id"] == project_id
    assert data["source_kind"] == "managed"
    assert data["status"] == "ready"
    assert len(data["resources"]) == 1
    pin = data["resources"][0]
    assert pin["resource_type"] == "eval_dataset"
    assert pin["resource_id"] == "ED-support"
    assert pin["logical_name"] == "ED-support"
    assert pin["provider_ref"] == "caliber-eval-dataset:/ED-support@1"
    assert pin["purpose"] == "runtime"
    assert pin["resolution"]["strategy"] == "live_resource_adapter"
    assert pin["resolution"]["example_count"] == 1
    expected_sha = _content_sha256(
        (
            {
                "example_id": "EX-1",
                "input": {"q": "EX-1"},
                "expected": {"a": "EX-1"},
                "weight": 1.0,
                "tags": [],
            },
        )
    )
    assert pin["content_sha256"] == expected_sha
    assert pin["version_ref"] == "1"

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
    _seed_dataset(db_session, dataset_id="ED-support", project_id=project_id, version=1)
    body = _snapshot_body(logical_name="regression-set", purpose="eval")

    response = client.post(f"{PREFIX}/projects/{project_id}/revisions:snapshot", json=body)
    assert response.status_code == 201, response.text
    pin = response.json()["data"]["resources"][0]
    assert pin["logical_name"] == "regression-set"
    assert pin["purpose"] == "eval"


def test_snapshot_a_different_dataset_version_produces_a_distinct_revision(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client)
    _seed_dataset(db_session, dataset_id="ED-versioned", project_id=project_id, version=2)
    _add_example(db_session, example_id="EX-1", dataset_id="ED-versioned", dataset_version=1)
    _add_example(db_session, example_id="EX-2", dataset_id="ED-versioned", dataset_version=2)

    first = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        json=_snapshot_body(resource_id="ED-versioned", version_ref="1"),
    )
    second = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        json=_snapshot_body(resource_id="ED-versioned", version_ref="2"),
    )
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["data"]["revision_id"] != second.json()["data"]["revision_id"]
    assert first.json()["data"]["revision_number"] + 1 == second.json()["data"]["revision_number"]


def test_snapshot_refuses_a_missing_dataset(client: TestClient) -> None:
    project_id = _create_project(client)
    response = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        json=_snapshot_body(resource_id="ED-ghost"),
    )
    assert response.status_code == 409
    assert "resource_resolve_failed" in response.json()["detail"]


def test_snapshot_refuses_a_version_not_yet_reached(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client)
    _seed_dataset(db_session, dataset_id="ED-support", project_id=project_id, version=1)
    response = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        json=_snapshot_body(version_ref="7"),
    )
    assert response.status_code == 409
    assert "resource_resolve_failed" in response.json()["detail"]


def test_snapshot_refuses_a_non_integer_version_ref(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client)
    _seed_dataset(db_session, dataset_id="ED-support", project_id=project_id, version=1)
    response = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        json=_snapshot_body(version_ref="latest"),
    )
    assert response.status_code == 409
    assert "resource_resolve_failed" in response.json()["detail"]


def test_snapshot_refuses_a_dataset_bound_to_a_different_project(
    client: TestClient, db_session: Session
) -> None:
    """The default test identity (``@test``) is a platform admin (see
    ``conftest.py::app_config``), and an admin bypasses the 3-tier
    visibility model everywhere in this codebase -- so proving this refusal
    for real needs a genuinely non-admin caller: one who has the
    ``caliber.operator`` platform scope (to reach the route at all) and an
    editor role on the *calling* project, but is neither a member of the
    *other* project nor the dataset's owner. Mirrors
    ``test_workspace_revisions_snapshot_judge.py``'s equivalent test."""
    project_id = _create_project(client, "Snapshot eval-dataset owner")
    other_project_id = _create_project(client, "Snapshot eval-dataset other")
    _seed_dataset(db_session, dataset_id="ED-owned-elsewhere", project_id=other_project_id)
    db_session.add(
        CaliberProjectMember(
            member_id="PM-snapshot-eval-dataset-operator",
            project_id=project_id,
            user_id="@snapshot-eval-dataset-operator",
            role="editor",
            status="active",
            created_by="@test",
        )
    )
    db_session.commit()
    client.app.state.config = client.app.state.config.model_copy(
        update={"operator_users": "@snapshot-eval-dataset-operator"}
    )
    response = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        headers={
            "X-CALIBER-User": "@snapshot-eval-dataset-operator",
            "X-CALIBER-Project": project_id,
        },
        json=_snapshot_body(resource_id="ED-owned-elsewhere"),
    )
    assert response.status_code == 409, response.text
    assert "resource_resolve_failed" in response.json()["detail"]


def test_snapshot_allows_a_public_dataset_for_a_different_project_caller(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client, "Snapshot eval-dataset caller")
    _seed_dataset(
        db_session,
        dataset_id="ED-shared",
        project_id=None,
        owner="@publisher",
        visibility="public",
    )
    response = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        json=_snapshot_body(resource_id="ED-shared"),
    )
    assert response.status_code == 201, response.text


def test_snapshot_refuses_archived_workspace(client: TestClient, db_session: Session) -> None:
    project_id = _create_project(client)
    _seed_dataset(db_session, dataset_id="ED-support", project_id=project_id, version=1)
    project = db_session.get(CaliberProject, project_id)
    assert project is not None
    project.status = "archived"
    db_session.commit()

    response = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot", json=_snapshot_body()
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "archived_workspace"
