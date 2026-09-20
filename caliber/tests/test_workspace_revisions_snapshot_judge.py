"""`P4-B`/`P4-C` contract tests for ``POST /projects/{id}/revisions:snapshot``
against the ``judge`` resource type specifically.

Mirrors ``test_workspace_revisions_snapshot.py``'s route-test style (a real
``client`` fixture wired to ``server.py::create_app``, so
``app.state.workspace_resource_adapter_registry`` already carries the real
``judge`` adapter exactly as production does) -- kept in its own file rather
than appended to that one so this slice and any concurrently-landing sibling
resource-type slice (e.g. ``tool``) don't both edit the same large test file.
"""

from __future__ import annotations

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import CaliberJudge, CaliberProject, CaliberProjectMember
from caliber.workspace_release_judge_adapter import CURRENT_VERSION_REF, _content_sha256

PREFIX = "/ajax-api/2.0/mlflow/caliber"


def _create_project(client: TestClient, name: str = "Snapshot judge workspace") -> str:
    response = client.post(f"{PREFIX}/projects", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["data"]["project_id"]


def _seed_judge(
    session: Session,
    *,
    judge_id: str,
    project_id: str | None,
    owner: str = "@test",
    visibility: str | None = None,
    instructions: str = "Grade the {{ outputs }} against {{ expectations }}.",
    model: str | None = "gpt-5.6-luna",
    feedback_value_type: str | None = "bool",
) -> CaliberJudge:
    judge = CaliberJudge(
        judge_id=judge_id,
        name=judge_id,
        description="",
        instructions=instructions,
        model=model,
        feedback_value_type=feedback_value_type,
        owner=owner,
        project_id=project_id,
        visibility=visibility or ("project" if project_id else "user"),
        tags=[],
        status="active",
    )
    session.add(judge)
    session.commit()
    return judge


def _snapshot_body(**overrides: object) -> dict[str, object]:
    resource: dict[str, object] = {
        "resource_type": "judge",
        "resource_id": "JDG-support",
        "version_ref": CURRENT_VERSION_REF,
    }
    resource.update(overrides)
    return {"resources": [resource]}


def test_snapshot_pins_a_live_judge_and_is_idempotent_by_content(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client)
    _seed_judge(db_session, judge_id="JDG-support", project_id=project_id)
    body = _snapshot_body()

    first = client.post(f"{PREFIX}/projects/{project_id}/revisions:snapshot", json=body)
    assert first.status_code == 201, first.text
    data = first.json()["data"]
    assert data["project_id"] == project_id
    assert data["source_kind"] == "managed"
    assert data["status"] == "ready"
    assert len(data["resources"]) == 1
    pin = data["resources"][0]
    assert pin["resource_type"] == "judge"
    assert pin["resource_id"] == "JDG-support"
    assert pin["logical_name"] == "JDG-support"
    assert pin["provider_ref"] == "judge:JDG-support"
    assert pin["purpose"] == "runtime"
    assert pin["resolution"]["strategy"] == "live_resource_adapter"
    expected_sha = _content_sha256(
        "JDG-support",
        "Grade the {{ outputs }} against {{ expectations }}.",
        "gpt-5.6-luna",
        "bool",
    )
    assert pin["content_sha256"] == expected_sha
    assert pin["version_ref"] == expected_sha

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
    _seed_judge(db_session, judge_id="JDG-support", project_id=project_id)
    body = _snapshot_body(logical_name="triage-judge", purpose="eval")

    response = client.post(f"{PREFIX}/projects/{project_id}/revisions:snapshot", json=body)
    assert response.status_code == 201, response.text
    pin = response.json()["data"]["resources"][0]
    assert pin["logical_name"] == "triage-judge"
    assert pin["purpose"] == "eval"


def test_snapshot_a_different_judge_content_produces_a_distinct_revision(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client)
    _seed_judge(
        db_session, judge_id="JDG-v1", project_id=project_id, instructions="v1 instructions"
    )
    _seed_judge(
        db_session, judge_id="JDG-v2", project_id=project_id, instructions="v2 instructions"
    )
    first = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        json=_snapshot_body(resource_id="JDG-v1"),
    )
    second = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        json=_snapshot_body(resource_id="JDG-v2"),
    )
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["data"]["revision_id"] != second.json()["data"]["revision_id"]
    assert first.json()["data"]["revision_number"] + 1 == second.json()["data"]["revision_number"]


def test_snapshot_refuses_a_missing_judge(client: TestClient) -> None:
    project_id = _create_project(client)
    response = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        json=_snapshot_body(resource_id="JDG-ghost"),
    )
    assert response.status_code == 409
    assert "resource_resolve_failed" in response.json()["detail"]


def test_snapshot_refuses_a_non_current_version_ref(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client)
    _seed_judge(db_session, judge_id="JDG-support", project_id=project_id)
    response = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        json=_snapshot_body(version_ref="3"),
    )
    assert response.status_code == 409
    assert "resource_resolve_failed" in response.json()["detail"]


def test_snapshot_refuses_a_judge_bound_to_a_different_project(
    client: TestClient, db_session: Session
) -> None:
    """The default test identity (``@test``) is a platform admin (see
    ``conftest.py::app_config``), and an admin bypasses the 3-tier
    visibility model everywhere in this codebase -- so proving this refusal
    for real needs a genuinely non-admin caller: one who has the
    ``caliber.operator`` platform scope (to reach the route at all) and an
    editor role on the *calling* project, but is neither a member of the
    *other* project nor the judge's owner. Mirrors
    ``test_workspace_revisions_snapshot.py``'s equivalent prompt test."""
    project_id = _create_project(client, "Snapshot judge owner")
    other_project_id = _create_project(client, "Snapshot judge other")
    _seed_judge(db_session, judge_id="JDG-owned-elsewhere", project_id=other_project_id)
    db_session.add(
        CaliberProjectMember(
            member_id="PM-snapshot-judge-operator",
            project_id=project_id,
            user_id="@snapshot-judge-operator",
            role="editor",
            status="active",
            created_by="@test",
        )
    )
    db_session.commit()
    client.app.state.config = client.app.state.config.model_copy(
        update={"operator_users": "@snapshot-judge-operator"}
    )
    response = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        headers={"X-CALIBER-User": "@snapshot-judge-operator", "X-CALIBER-Project": project_id},
        json=_snapshot_body(resource_id="JDG-owned-elsewhere"),
    )
    assert response.status_code == 409, response.text
    assert "resource_resolve_failed" in response.json()["detail"]


def test_snapshot_allows_a_public_judge_for_a_different_project_caller(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client, "Snapshot judge caller")
    _seed_judge(
        db_session,
        judge_id="JDG-shared",
        project_id=None,
        owner="@publisher",
        visibility="public",
    )
    response = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        json=_snapshot_body(resource_id="JDG-shared"),
    )
    assert response.status_code == 201, response.text


def test_snapshot_refuses_archived_workspace(client: TestClient, db_session: Session) -> None:
    project_id = _create_project(client)
    _seed_judge(db_session, judge_id="JDG-support", project_id=project_id)
    project = db_session.get(CaliberProject, project_id)
    assert project is not None
    project.status = "archived"
    db_session.commit()

    response = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot", json=_snapshot_body()
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "archived_workspace"
