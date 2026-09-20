"""`P4-B`/`P4-C` contract tests for ``POST /projects/{id}/revisions:snapshot``
against the ``skill`` resource type specifically.

Mirrors ``test_workspace_revisions_snapshot_judge.py``'s route-test style (a
real ``client`` fixture wired to ``server.py::create_app``, so
``app.state.workspace_resource_adapter_registry`` already carries the real
``skill`` adapter exactly as production does) -- kept in its own file rather
than appended to ``test_workspace_revisions_snapshot.py`` so this slice and
any concurrently-landing sibling resource-type slice don't both edit the same
large test file.
"""

from __future__ import annotations

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberProject,
    CaliberProjectMember,
    CaliberSkill,
    CaliberSkillVersion,
)
from caliber.workspace_release_skill_adapter import _content_sha256

PREFIX = "/ajax-api/2.0/mlflow/caliber"


def _create_project(client: TestClient, name: str = "Snapshot skill workspace") -> str:
    response = client.post(f"{PREFIX}/projects", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["data"]["project_id"]


def _seed_skill(
    session: Session,
    *,
    skill_id: str,
    project_id: str | None,
    owner: str = "@test",
    visibility: str | None = None,
    content: str = "Full instructions for the skill.",
    summary: str = "When to use this skill.",
    version: int = 1,
) -> CaliberSkill:
    skill = CaliberSkill(
        skill_id=skill_id,
        name=skill_id,
        description="",
        summary=summary,
        content=content,
        owner=owner,
        project_id=project_id,
        visibility=visibility or ("project" if project_id else "user"),
        category="custom",
        tags=[],
        skill_metadata={},
        depends_on=[],
        status="active",
        version=version,
    )
    session.add(skill)
    session.add(
        CaliberSkillVersion(
            skill_version_id=f"SKV-{skill_id}-{version}",
            skill_id=skill_id,
            version_number=version,
            content=content,
            summary=summary,
            created_by=owner,
        )
    )
    session.commit()
    return skill


def _snapshot_body(**overrides: object) -> dict[str, object]:
    resource: dict[str, object] = {
        "resource_type": "skill",
        "resource_id": "SKL-support",
        "version_ref": "1",
    }
    resource.update(overrides)
    return {"resources": [resource]}


def test_snapshot_pins_a_live_skill_version_and_is_idempotent_by_content(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client)
    _seed_skill(db_session, skill_id="SKL-support", project_id=project_id)
    body = _snapshot_body()

    first = client.post(f"{PREFIX}/projects/{project_id}/revisions:snapshot", json=body)
    assert first.status_code == 201, first.text
    data = first.json()["data"]
    assert data["project_id"] == project_id
    assert data["source_kind"] == "managed"
    assert data["status"] == "ready"
    assert len(data["resources"]) == 1
    pin = data["resources"][0]
    assert pin["resource_type"] == "skill"
    assert pin["resource_id"] == "SKL-support"
    assert pin["logical_name"] == "SKL-support"
    assert pin["version_ref"] == "1"
    assert pin["provider_ref"] == "caliber-skill-version:/SKL-support/1"
    assert pin["purpose"] == "runtime"
    assert pin["resolution"]["strategy"] == "live_resource_adapter"
    expected_sha = _content_sha256("Full instructions for the skill.", "When to use this skill.")
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
    _seed_skill(db_session, skill_id="SKL-support", project_id=project_id)
    body = _snapshot_body(logical_name="triage-skill", purpose="eval")

    response = client.post(f"{PREFIX}/projects/{project_id}/revisions:snapshot", json=body)
    assert response.status_code == 201, response.text
    pin = response.json()["data"]["resources"][0]
    assert pin["logical_name"] == "triage-skill"
    assert pin["purpose"] == "eval"


def test_snapshot_a_different_skill_version_produces_a_distinct_revision(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client)
    _seed_skill(db_session, skill_id="SKL-1", project_id=project_id, content="v1 instructions")
    _seed_skill(db_session, skill_id="SKL-2", project_id=project_id, content="v2 instructions")
    first = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        json=_snapshot_body(resource_id="SKL-1"),
    )
    second = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        json=_snapshot_body(resource_id="SKL-2"),
    )
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["data"]["revision_id"] != second.json()["data"]["revision_id"]
    assert first.json()["data"]["revision_number"] + 1 == second.json()["data"]["revision_number"]


def test_snapshot_pins_an_older_version_distinctly_from_the_current_head(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client)
    _seed_skill(
        db_session,
        skill_id="SKL-support",
        project_id=project_id,
        content="v1 instructions",
        version=1,
    )
    skill = db_session.get(CaliberSkill, "SKL-support")
    assert skill is not None
    skill.content = "v2 instructions"
    skill.version = 2
    db_session.add(
        CaliberSkillVersion(
            skill_version_id="SKV-SKL-support-2",
            skill_id="SKL-support",
            version_number=2,
            content="v2 instructions",
            summary=skill.summary,
            created_by="@test",
        )
    )
    db_session.commit()

    old = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        json=_snapshot_body(version_ref="1"),
    )
    new = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        json=_snapshot_body(version_ref="2"),
    )
    assert old.status_code == 201, old.text
    assert new.status_code == 201, new.text
    assert (
        old.json()["data"]["resources"][0]["content_sha256"]
        != (new.json()["data"]["resources"][0]["content_sha256"])
    )


def test_snapshot_refuses_a_missing_skill(client: TestClient) -> None:
    project_id = _create_project(client)
    response = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        json=_snapshot_body(resource_id="SKL-ghost"),
    )
    assert response.status_code == 409
    assert "resource_resolve_failed" in response.json()["detail"]


def test_snapshot_refuses_a_version_not_in_history(client: TestClient, db_session: Session) -> None:
    project_id = _create_project(client)
    _seed_skill(db_session, skill_id="SKL-support", project_id=project_id)
    response = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        json=_snapshot_body(version_ref="99"),
    )
    assert response.status_code == 409
    assert "resource_resolve_failed" in response.json()["detail"]


def test_snapshot_refuses_a_skill_bound_to_a_different_project(
    client: TestClient, db_session: Session
) -> None:
    """The default test identity (``@test``) is a platform admin (see
    ``conftest.py::app_config``), and an admin bypasses the 3-tier
    visibility model everywhere in this codebase -- so proving this refusal
    for real needs a genuinely non-admin caller: one who has the
    ``caliber.operator`` platform scope (to reach the route at all) and an
    editor role on the *calling* project, but is neither a member of the
    *other* project nor the skill's owner. Mirrors
    ``test_workspace_revisions_snapshot_judge.py``'s equivalent test."""
    project_id = _create_project(client, "Snapshot skill owner")
    other_project_id = _create_project(client, "Snapshot skill other")
    _seed_skill(db_session, skill_id="SKL-owned-elsewhere", project_id=other_project_id)
    db_session.add(
        CaliberProjectMember(
            member_id="PM-snapshot-skill-operator",
            project_id=project_id,
            user_id="@snapshot-skill-operator",
            role="editor",
            status="active",
            created_by="@test",
        )
    )
    db_session.commit()
    client.app.state.config = client.app.state.config.model_copy(
        update={"operator_users": "@snapshot-skill-operator"}
    )
    response = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        headers={"X-CALIBER-User": "@snapshot-skill-operator", "X-CALIBER-Project": project_id},
        json=_snapshot_body(resource_id="SKL-owned-elsewhere"),
    )
    assert response.status_code == 409, response.text
    assert "resource_resolve_failed" in response.json()["detail"]


def test_snapshot_allows_a_public_skill_for_a_different_project_caller(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client, "Snapshot skill caller")
    _seed_skill(
        db_session,
        skill_id="SKL-shared",
        project_id=None,
        owner="@publisher",
        visibility="public",
    )
    response = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        json=_snapshot_body(resource_id="SKL-shared"),
    )
    assert response.status_code == 201, response.text


def test_snapshot_refuses_archived_workspace(client: TestClient, db_session: Session) -> None:
    project_id = _create_project(client)
    _seed_skill(db_session, skill_id="SKL-support", project_id=project_id)
    project = db_session.get(CaliberProject, project_id)
    assert project is not None
    project.status = "archived"
    db_session.commit()

    response = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot", json=_snapshot_body()
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "archived_workspace"
