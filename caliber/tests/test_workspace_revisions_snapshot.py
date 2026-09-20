"""`P4-B`/`P4-C` contract tests for ``POST /projects/{id}/revisions:snapshot``.

Mirrors ``test_workspace_routes.py``'s route-test style (a real ``client``
fixture wired to ``server.py::create_app``, so ``app.state.
workspace_resource_adapter_registry`` already carries the real ``workflow``/
``prompt``/``tool``/``judge`` adapters exactly as production does) and
``test_routes_prompts.py``'s mlflow-stub pattern
(``monkeypatch.setitem(sys.modules, "mlflow", ...)`` -- offline and
deterministic, no real MLflow Prompt Registry network I/O).
"""

from __future__ import annotations

import hashlib
import sys
import types
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberAgentConfig,
    CaliberProject,
    CaliberProjectMember,
    CaliberToolRegistry,
)
from caliber.workspace_release_adapters import FakeWorkspaceResourceAdapter

PREFIX = "/ajax-api/2.0/mlflow/caliber"


def _create_project(client: TestClient, name: str = "Snapshot workspace") -> str:
    response = client.post(f"{PREFIX}/projects", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["data"]["project_id"]


def _install_mlflow(
    monkeypatch: pytest.MonkeyPatch, *, load_refs: dict[str, Any] | None = None
) -> None:
    load_refs = load_refs or {}

    def load_prompt(ref: str, allow_missing: bool = False) -> object | None:
        value = load_refs.get(ref)
        if isinstance(value, Exception):
            raise value
        return value

    mlflow_mod = types.ModuleType("mlflow")
    mlflow_mod.genai = SimpleNamespace(load_prompt=load_prompt)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlflow", mlflow_mod)


def _snapshot_body(**overrides: object) -> dict[str, object]:
    resource: dict[str, object] = {
        "resource_type": "prompt",
        "resource_id": "support-agent",
        "version_ref": "3",
    }
    resource.update(overrides)
    return {"resources": [resource]}


def test_snapshot_pins_a_live_prompt_and_is_idempotent_by_content(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = _create_project(client)
    _install_mlflow(
        monkeypatch,
        load_refs={
            "prompts:/support-agent/3": SimpleNamespace(
                name="support-agent", version=3, template="Hello {{name}}", tags={}
            )
        },
    )
    body = _snapshot_body()

    first = client.post(f"{PREFIX}/projects/{project_id}/revisions:snapshot", json=body)
    assert first.status_code == 201, first.text
    data = first.json()["data"]
    assert data["project_id"] == project_id
    assert data["source_kind"] == "managed"
    assert data["status"] == "ready"
    assert data["manifest_sha256"] is None
    assert data["source_bundle_sha256"] is None
    assert data["source_id"] is None
    assert data["source_commit_sha"] is None
    assert len(data["resources"]) == 1
    pin = data["resources"][0]
    assert pin["resource_type"] == "prompt"
    assert pin["resource_id"] == "support-agent"
    assert pin["version_ref"] == "3"
    assert pin["logical_name"] == "support-agent"
    assert pin["provider_ref"] == "prompts:/support-agent/3"
    assert pin["purpose"] == "runtime"
    assert pin["resolution"]["strategy"] == "live_resource_adapter"
    expected_sha = hashlib.sha256(b"Hello {{name}}").hexdigest()
    assert pin["content_sha256"] == expected_sha

    # Idempotent by content: an identical retry converges on the same
    # revision (project_id, revision_sha256) rather than creating a
    # duplicate -- no Idempotency-Key header is needed for this route.
    second = client.post(f"{PREFIX}/projects/{project_id}/revisions:snapshot", json=body)
    assert second.status_code == 201, second.text
    assert second.json()["data"]["revision_id"] == data["revision_id"]
    assert second.json()["data"]["revision_number"] == data["revision_number"]

    listing = client.get(f"{PREFIX}/projects/{project_id}/revisions")
    assert listing.status_code == 200
    assert len(listing.json()["data"]["items"]) == 1


def test_snapshot_honors_an_explicit_logical_name_and_purpose(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = _create_project(client)
    _install_mlflow(
        monkeypatch,
        load_refs={
            "prompts:/support-agent/3": SimpleNamespace(
                name="support-agent", version=3, template="Hello", tags={}
            )
        },
    )
    body = _snapshot_body(logical_name="triage-prompt", purpose="eval")

    response = client.post(f"{PREFIX}/projects/{project_id}/revisions:snapshot", json=body)
    assert response.status_code == 201, response.text
    pin = response.json()["data"]["resources"][0]
    assert pin["logical_name"] == "triage-prompt"
    assert pin["purpose"] == "eval"


def test_snapshot_a_different_version_produces_a_distinct_revision(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = _create_project(client)
    _install_mlflow(
        monkeypatch,
        load_refs={
            "prompts:/support-agent/3": SimpleNamespace(
                name="support-agent", version=3, template="v3", tags={}
            ),
            "prompts:/support-agent/4": SimpleNamespace(
                name="support-agent", version=4, template="v4", tags={}
            ),
        },
    )
    first = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot", json=_snapshot_body(version_ref="3")
    )
    second = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot", json=_snapshot_body(version_ref="4")
    )
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["data"]["revision_id"] != second.json()["data"]["revision_id"]
    assert first.json()["data"]["revision_number"] + 1 == second.json()["data"]["revision_number"]


def test_snapshot_refuses_resource_type_with_no_registered_adapter(client: TestClient) -> None:
    project_id = _create_project(client)
    # `skill` remains one of the still-unregistered follow-up resource types
    # named by docs/workspace-plan.md's `P4-C` row (`tool` and `judge` each
    # gained a real adapter in earlier slices, so neither can stand in for
    # "unregistered" anymore).
    body = {
        "resources": [{"resource_type": "skill", "resource_id": "some-skill", "version_ref": "1"}]
    }
    response = client.post(f"{PREFIX}/projects/{project_id}/revisions:snapshot", json=body)
    assert response.status_code == 409
    assert "resource_type_adapter_unavailable" in response.json()["detail"]


def test_snapshot_refuses_an_adapter_whose_snapshot_is_not_content_addressed(
    client: TestClient,
) -> None:
    """``workflow`` is registered (`P5-E`) but its own ``snapshot()`` is still
    the documented placeholder -- prove the route fails closed on *any*
    adapter that does not return a :class:`SnapshotPin`, not only a missing
    one, using a directly-registered fake to isolate that check from the
    real ``workflow`` adapter's unrelated declaration-shape mismatch."""
    project_id = _create_project(client)
    registry = client.app.state.workspace_resource_adapter_registry
    registry.register(FakeWorkspaceResourceAdapter(resource_type="widget"), replace=True)
    body = {"resources": [{"resource_type": "widget", "resource_id": "w-1", "version_ref": "1"}]}
    response = client.post(f"{PREFIX}/projects/{project_id}/revisions:snapshot", json=body)
    assert response.status_code == 409
    assert "does not support managed snapshotting yet" in response.json()["detail"]


def test_snapshot_refuses_duplicate_pins_in_one_request(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = _create_project(client)
    _install_mlflow(
        monkeypatch,
        load_refs={"prompts:/dup/1": SimpleNamespace(name="dup", version=1, template="a", tags={})},
    )
    body = {
        "resources": [
            {"resource_type": "prompt", "resource_id": "dup", "version_ref": "1"},
            {"resource_type": "prompt", "resource_id": "dup", "version_ref": "1"},
        ]
    }
    response = client.post(f"{PREFIX}/projects/{project_id}/revisions:snapshot", json=body)
    assert response.status_code == 400


def test_snapshot_rejects_an_empty_resource_list(client: TestClient) -> None:
    project_id = _create_project(client)
    response = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot", json={"resources": []}
    )
    assert response.status_code == 400


def test_snapshot_refuses_archived_workspace(client: TestClient, db_session: Session) -> None:
    project_id = _create_project(client)
    project = db_session.get(CaliberProject, project_id)
    assert project is not None
    project.status = "archived"
    db_session.commit()

    response = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot", json=_snapshot_body()
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "archived_workspace"


def test_snapshot_refuses_a_missing_prompt_version(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = _create_project(client)
    _install_mlflow(monkeypatch, load_refs={})
    response = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        json=_snapshot_body(resource_id="ghost", version_ref="9"),
    )
    assert response.status_code == 409
    assert "resource_resolve_failed" in response.json()["detail"]


def test_snapshot_refuses_a_prompt_bound_to_a_different_project(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default test identity (``@test``) is a platform admin (see
    ``conftest.py::app_config``), and an admin bypasses the 3-tier
    visibility model everywhere in this codebase (``db/scoping.py``'s own
    documented admin bypass) -- exactly like a real CALIBER admin should be
    able to. So proving this refusal for real needs a genuinely non-admin
    caller: one who has the ``caliber.operator`` platform scope (to reach
    the route at all) and an editor role on the *calling* project (to pass
    the project-role check), but is neither a member of the *other* project
    nor the prompt's owner."""
    project_id = _create_project(client, "Snapshot owner")
    other_project_id = _create_project(client, "Snapshot other")
    db_session.add(
        CaliberAgentConfig(
            agent_id="owned-elsewhere",
            experiment_id="exp-owned-elsewhere",
            name="owned-elsewhere",
            owner="@test",
            project_id=other_project_id,
            visibility="project",
            artifact_types=["prompt"],
            eval_thresholds={},
            optimizer_config={},
            approval_policy={},
        )
    )
    db_session.add(
        CaliberProjectMember(
            member_id="PM-snapshot-operator",
            project_id=project_id,
            user_id="@snapshot-operator",
            role="editor",
            status="active",
            created_by="@test",
        )
    )
    db_session.commit()
    client.app.state.config = client.app.state.config.model_copy(
        update={"operator_users": "@snapshot-operator"}
    )
    _install_mlflow(
        monkeypatch,
        load_refs={
            "prompts:/owned-elsewhere/1": SimpleNamespace(
                name="owned-elsewhere", version=1, template="x", tags={}
            )
        },
    )
    response = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        headers={"X-CALIBER-User": "@snapshot-operator", "X-CALIBER-Project": project_id},
        json=_snapshot_body(resource_id="owned-elsewhere", version_ref="1"),
    )
    assert response.status_code == 409, response.text
    assert "resource_resolve_failed" in response.json()["detail"]
    assert "resource_resolve_failed" in response.json()["detail"]


def test_snapshot_requires_operator_scope_and_matching_project_header(
    client: TestClient,
) -> None:
    project_id = _create_project(client)
    body = _snapshot_body()

    mismatch = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        headers={"X-CALIBER-Project": "PRJ-other"},
        json=body,
    )
    assert mismatch.status_code == 400
    assert mismatch.json()["detail"] == "workspace_context_mismatch"

    forbidden = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        headers={"X-CALIBER-User": "@viewer-only"},
        json=body,
    )
    assert forbidden.status_code == 403


# -- tool resource type (slice 2 of the managed-snapshot epic) --------------


def _seed_tool(
    session: Session,
    *,
    tool_id: str,
    name: str,
    version: str = "1",
    project_id: str | None,
    owner: str = "@test",
    visibility: str | None = None,
) -> CaliberToolRegistry:
    tool = CaliberToolRegistry(
        tool_id=tool_id,
        name=name,
        version=version,
        description="a test tool",
        module_path="caliber.tools.example",
        callable_name="run",
        execution_backend="python_callable",
        side_effect_level="read",
        owner=owner,
        project_id=project_id,
        visibility=visibility or ("project" if project_id else "user"),
        status="active",
    )
    session.add(tool)
    session.commit()
    return tool


def _tool_snapshot_body(**overrides: object) -> dict[str, object]:
    resource: dict[str, object] = {
        "resource_type": "tool",
        "resource_id": "search-tool",
        "version_ref": "1",
    }
    resource.update(overrides)
    return {"resources": [resource]}


def test_snapshot_pins_a_live_tool_and_is_idempotent_by_content(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client)
    _seed_tool(db_session, tool_id="TOOL-snap-1", name="search-tool", project_id=project_id)
    body = _tool_snapshot_body()

    first = client.post(f"{PREFIX}/projects/{project_id}/revisions:snapshot", json=body)
    assert first.status_code == 201, first.text
    data = first.json()["data"]
    assert data["source_kind"] == "managed"
    assert len(data["resources"]) == 1
    pin = data["resources"][0]
    assert pin["resource_type"] == "tool"
    assert pin["resource_id"] == "search-tool"
    assert pin["version_ref"] == "1"
    assert pin["provider_ref"] == "caliber-tool-registry:/TOOL-snap-1"
    assert pin["resolution"]["strategy"] == "live_resource_adapter"

    # Idempotent by content, same as the prompt resource type above.
    second = client.post(f"{PREFIX}/projects/{project_id}/revisions:snapshot", json=body)
    assert second.status_code == 201, second.text
    assert second.json()["data"]["revision_id"] == data["revision_id"]


def test_snapshot_a_different_tool_version_produces_a_distinct_revision(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client)
    _seed_tool(
        db_session, tool_id="TOOL-snap-2a", name="search-tool", version="1", project_id=project_id
    )
    _seed_tool(
        db_session, tool_id="TOOL-snap-2b", name="search-tool", version="2", project_id=project_id
    )
    first = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        json=_tool_snapshot_body(version_ref="1"),
    )
    second = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        json=_tool_snapshot_body(version_ref="2"),
    )
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["data"]["revision_id"] != second.json()["data"]["revision_id"]


def test_snapshot_refuses_a_missing_tool_version(client: TestClient) -> None:
    project_id = _create_project(client)
    response = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        json=_tool_snapshot_body(resource_id="ghost", version_ref="9"),
    )
    assert response.status_code == 409
    assert "resource_resolve_failed" in response.json()["detail"]


def test_snapshot_refuses_a_tool_bound_to_a_different_project(
    client: TestClient, db_session: Session
) -> None:
    """Mirrors ``test_snapshot_refuses_a_prompt_bound_to_a_different_project``:
    the default test identity is a platform admin and bypasses visibility, so
    this uses a genuinely non-admin caller (operator scope + editor role on
    the *calling* project) who is neither a member of the *other* project nor
    the tool's owner."""
    project_id = _create_project(client, "Snapshot tool owner")
    other_project_id = _create_project(client, "Snapshot tool other")
    _seed_tool(
        db_session,
        tool_id="TOOL-snap-cross",
        name="owned-elsewhere",
        project_id=other_project_id,
        owner="@test",
    )
    db_session.add(
        CaliberProjectMember(
            member_id="PM-snapshot-tool-operator",
            project_id=project_id,
            user_id="@snapshot-tool-operator",
            role="editor",
            status="active",
            created_by="@test",
        )
    )
    db_session.commit()
    client.app.state.config = client.app.state.config.model_copy(
        update={"operator_users": "@snapshot-tool-operator"}
    )
    response = client.post(
        f"{PREFIX}/projects/{project_id}/revisions:snapshot",
        headers={"X-CALIBER-User": "@snapshot-tool-operator", "X-CALIBER-Project": project_id},
        json=_tool_snapshot_body(resource_id="owned-elsewhere", version_ref="1"),
    )
    assert response.status_code == 409, response.text
    assert "resource_resolve_failed" in response.json()["detail"]
