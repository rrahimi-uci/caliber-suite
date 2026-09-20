"""`P4-B`/`P4-C` contract tests for ``POST /projects/{id}/revisions:snapshot``
against the ``mcp_server`` resource type specifically.

Mirrors ``test_workspace_revisions_snapshot_judge.py``'s route-test style (a
real ``client`` fixture wired to ``server.py::create_app``, so
``app.state.workspace_resource_adapter_registry`` already carries the real
``mcp_server`` adapter exactly as production does) -- kept in its own file
rather than appended to ``test_workspace_revisions_snapshot.py`` so this slice
and any concurrently-landing sibling resource-type slice don't both edit the
same large test file.
"""

from __future__ import annotations

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import CaliberMcpServer, CaliberProject, CaliberProjectMember
from caliber.workspace_release_mcp_server_adapter import (
    CURRENT_VERSION_REF,
    _content_payload,
    _content_sha256,
)

PREFIX = "/ajax-api/2.0/mlflow/caliber"


def _create_project(client: TestClient, name: str = "Snapshot mcp_server workspace") -> str:
    response = client.post(f"{PREFIX}/projects", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["data"]["project_id"]


def _seed_server(
    session: Session,
    *,
    server_id: str,
    project_id: str | None,
    owner: str = "@test",
    visibility: str | None = None,
    transport: str = "stdio",
    command: str = "npx test-server",
) -> CaliberMcpServer:
    server = CaliberMcpServer(
        server_id=server_id,
        name=server_id,
        description="",
        transport=transport,
        uri="",
        command=command,
        args=[],
        env={},
        headers={},
        auth_type="none",
        auth_config={},
        discovered_tools=[{"name": "search", "description": "Search things"}],
        tool_policies={
            "search": {"allowed": True, "side_effect_level": "read", "requires_approval": False}
        },
        owner=owner,
        project_id=project_id,
        visibility=visibility or ("project" if project_id else "user"),
        status="active",
    )
    session.add(server)
    session.commit()
    return server


def _expected_content_sha(server: CaliberMcpServer) -> str:
    return _content_sha256(
        _content_payload(
            transport=server.transport,
            uri=server.uri,
            command=server.command,
            args=server.args or [],
            env=server.env or {},
            headers=server.headers or {},
            auth_type=server.auth_type,
            auth_config=server.auth_config or {},
            discovered_tools=[t for t in (server.discovered_tools or []) if isinstance(t, dict)],
            tool_policies=server.tool_policies or {},
        )
    )


def _snapshot_body(**overrides: object) -> dict[str, object]:
    resource: dict[str, object] = {
        "resource_type": "mcp_server",
        "resource_id": "MCP-support",
        "version_ref": CURRENT_VERSION_REF,
    }
    resource.update(overrides)
    return {"resources": [resource]}


def test_snapshot_pins_a_live_server_and_is_idempotent_by_content(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client)
    server = _seed_server(db_session, server_id="MCP-support", project_id=project_id)
    body = _snapshot_body()

    first = client.post(f"{PREFIX}/projects/{project_id}/revisions:snapshot", json=body)
    assert first.status_code == 201, first.text
    data = first.json()["data"]
    assert data["project_id"] == project_id
    assert data["source_kind"] == "managed"
    assert data["status"] == "ready"
    assert len(data["resources"]) == 1
    pin = data["resources"][0]
    assert pin["resource_type"] == "mcp_server"
    assert pin["resource_id"] == "MCP-support"
    assert pin["logical_name"] == "MCP-support"
    assert pin["provider_ref"] == "mcp-server:MCP-support"
    assert pin["purpose"] == "runtime"
    assert pin["resolution"]["strategy"] == "live_resource_adapter"
    expected_sha = _expected_content_sha(server)
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
    _seed_server(db_session, server_id="MCP-support", project_id=project_id)
    body = _snapshot_body(logical_name="support-mcp", purpose="eval")

    response = client.post(f"{PREFIX}/projects/{project_id}/revisions:snapshot", json=body)
    assert response.status_code == 201, response.text
    pin = response.json()["data"]["resources"][0]
    assert pin["logical_name"] == "support-mcp"
    assert pin["purpose"] == "eval"


def test_snapshot_a_different_server_content_produces_a_distinct_revision(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client)
    _seed_server(db_session, server_id="MCP-v1", project_id=project_id, command="cmd-v1")
    _seed_server(db_session, server_id="MCP-v2", project_id=project_id, command="cmd-v2")
    first = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        json=_snapshot_body(resource_id="MCP-v1"),
    )
    second = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        json=_snapshot_body(resource_id="MCP-v2"),
    )
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["data"]["revision_id"] != second.json()["data"]["revision_id"]
    assert first.json()["data"]["revision_number"] + 1 == second.json()["data"]["revision_number"]


def test_snapshot_refuses_a_missing_server(client: TestClient) -> None:
    project_id = _create_project(client)
    response = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        json=_snapshot_body(resource_id="MCP-ghost"),
    )
    assert response.status_code == 409
    assert "resource_resolve_failed" in response.json()["detail"]


def test_snapshot_refuses_a_non_current_version_ref(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client)
    _seed_server(db_session, server_id="MCP-support", project_id=project_id)
    response = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        json=_snapshot_body(version_ref="3"),
    )
    assert response.status_code == 409
    assert "resource_resolve_failed" in response.json()["detail"]


def test_snapshot_refuses_a_server_bound_to_a_different_project(
    client: TestClient, db_session: Session
) -> None:
    """The default test identity (``@test``) is a platform admin (see
    ``conftest.py::app_config``), and an admin bypasses the 3-tier
    visibility model everywhere in this codebase -- so proving this refusal
    for real needs a genuinely non-admin caller: one who has the
    ``caliber.operator`` platform scope (to reach the route at all) and an
    editor role on the *calling* project, but is neither a member of the
    *other* project nor the server's owner. Mirrors
    ``test_workspace_revisions_snapshot_judge.py``'s equivalent test."""
    project_id = _create_project(client, "Snapshot mcp_server owner")
    other_project_id = _create_project(client, "Snapshot mcp_server other")
    _seed_server(db_session, server_id="MCP-owned-elsewhere", project_id=other_project_id)
    db_session.add(
        CaliberProjectMember(
            member_id="PM-snapshot-mcp-operator",
            project_id=project_id,
            user_id="@snapshot-mcp-operator",
            role="editor",
            status="active",
            created_by="@test",
        )
    )
    db_session.commit()
    client.app.state.config = client.app.state.config.model_copy(
        update={"operator_users": "@snapshot-mcp-operator"}
    )
    response = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        headers={"X-CALIBER-User": "@snapshot-mcp-operator", "X-CALIBER-Project": project_id},
        json=_snapshot_body(resource_id="MCP-owned-elsewhere"),
    )
    assert response.status_code == 409, response.text
    assert "resource_resolve_failed" in response.json()["detail"]


def test_snapshot_allows_a_public_server_for_a_different_project_caller(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client, "Snapshot mcp_server caller")
    _seed_server(
        db_session,
        server_id="MCP-shared",
        project_id=None,
        owner="@publisher",
        visibility="public",
    )
    response = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        json=_snapshot_body(resource_id="MCP-shared"),
    )
    assert response.status_code == 201, response.text


def test_snapshot_refuses_archived_workspace(client: TestClient, db_session: Session) -> None:
    project_id = _create_project(client)
    _seed_server(db_session, server_id="MCP-support", project_id=project_id)
    project = db_session.get(CaliberProject, project_id)
    assert project is not None
    project.status = "archived"
    db_session.commit()

    response = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot", json=_snapshot_body()
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "archived_workspace"
