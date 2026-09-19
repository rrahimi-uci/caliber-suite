"""`P4-B`/`P4-C` contract tests for ``POST /projects/{id}/revisions:snapshot``.

Mirrors ``test_workspace_routes.py``'s route-test style (a real ``client``
fixture wired to ``server.py::create_app``, so ``app.state.
workspace_resource_adapter_registry`` already carries the real ``workflow``/
``prompt`` adapters exactly as production does) and ``test_routes_prompts.py``'s
mlflow-stub pattern (``monkeypatch.setitem(sys.modules, "mlflow", ...)`` --
offline and deterministic, no real MLflow Prompt Registry network I/O).
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

from caliber.db.models import CaliberAgentConfig, CaliberProject
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
    body = {
        "resources": [{"resource_type": "tool", "resource_id": "some-tool", "version_ref": "1"}]
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
    db_session.commit()
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
        json=_snapshot_body(resource_id="owned-elsewhere", version_ref="1"),
    )
    assert response.status_code == 409
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
