"""Tests for ``POST /caliber/workflows/import`` (plan §15.5)."""

from __future__ import annotations

import yaml
from starlette.testclient import TestClient

from tests.workflow_helpers import PREFIX, make_manifest


def test_import_from_manifest_dict(client: TestClient) -> None:
    manifest = make_manifest("imported_wf", name="Imported WF")
    r = client.post(f"{PREFIX}/workflows/import", json={"manifest": manifest})
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["workflow"]["workflow_id"].startswith("WF-")
    assert data["workflow"]["workflow_id"] != "imported_wf"
    assert data["version"]["manifest"]["workflow_id"] == data["workflow"]["workflow_id"]
    assert data["version"]["status"] == "draft"
    assert data["version"]["version_number"] == 1


def test_import_from_manifest_yaml(client: TestClient) -> None:
    manifest = make_manifest("yaml_wf", name="YAML WF")
    r = client.post(
        f"{PREFIX}/workflows/import",
        json={"manifest_yaml": yaml.safe_dump(manifest)},
    )
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["workflow"]["workflow_id"].startswith("WF-")
    assert data["workflow"]["workflow_id"] != "yaml_wf"


def test_import_same_source_creates_independent_workflow(client: TestClient) -> None:
    manifest = make_manifest("again_wf", name="Again WF")
    first = client.post(f"{PREFIX}/workflows/import", json={"manifest": manifest})
    r = client.post(
        f"{PREFIX}/workflows/import",
        json={"manifest": manifest, "name": "Again WF Copy"},
    )
    assert r.status_code == 201
    assert r.json()["data"]["version"]["version_number"] == 1
    assert (
        r.json()["data"]["workflow"]["workflow_id"]
        != first.json()["data"]["workflow"]["workflow_id"]
    )


def test_import_name_override(client: TestClient) -> None:
    manifest = make_manifest("ov_wf", name="Original")
    r = client.post(f"{PREFIX}/workflows/import", json={"manifest": manifest, "name": "Renamed"})
    assert r.json()["data"]["workflow"]["name"] == "Renamed"


def test_import_invalid_manifest_400(client: TestClient) -> None:
    bad = make_manifest("bad_wf")
    del bad["nodes"]["agent"]["model"]
    r = client.post(f"{PREFIX}/workflows/import", json={"manifest": bad})
    assert r.status_code == 400


def test_import_requires_exactly_one_source(client: TestClient) -> None:
    r = client.post(f"{PREFIX}/workflows/import", json={})
    assert r.status_code == 400
    r = client.post(
        f"{PREFIX}/workflows/import",
        json={"manifest": make_manifest("a"), "manifest_yaml": "x: 1"},
    )
    assert r.status_code == 400


def test_import_name_clash_409(client: TestClient) -> None:
    client.post(
        f"{PREFIX}/workflows/import", json={"manifest": make_manifest("wf_a", name="Shared")}
    )
    r = client.post(
        f"{PREFIX}/workflows/import", json={"manifest": make_manifest("wf_b", name="Shared")}
    )
    assert r.status_code == 409


def test_import_viewer_forbidden(client: TestClient) -> None:
    r = client.post(
        f"{PREFIX}/workflows/import",
        json={"manifest": make_manifest("vwf")},
        headers={"X-CALIBER-User": "@viewer"},
    )
    assert r.status_code == 403


def test_import_rejects_inline_secret(client: TestClient) -> None:
    manifest = make_manifest("sec_wf")
    manifest["nodes"]["agent"]["output_type"] = {"api_key": "sk-leaked-123"}
    r = client.post(f"{PREFIX}/workflows/import", json={"manifest": manifest})
    assert r.status_code == 400
    assert "secret" in r.json()["detail"].lower()


def test_import_preview_is_read_only_and_reports_graph_validation(client: TestClient) -> None:
    manifest = make_manifest("preview_wf", name="Preview WF")
    del manifest["nodes"]["final"]
    manifest["edges"] = [manifest["edges"][0]]

    r = client.post(f"{PREFIX}/workflows/import/preview", json={"manifest": manifest})

    assert r.status_code == 200
    data = r.json()["data"]
    assert data["source_workflow_id"] == "preview_wf"
    assert data["ready_to_import"] is False
    assert any(error["code"] == "no_output_node" for error in data["validation"]["errors"])
    assert client.get(f"{PREFIX}/workflows").json()["data"] == []


def test_import_rejects_valid_shape_with_unresolved_skill(client: TestClient) -> None:
    manifest = make_manifest("missing_skill_wf", name="Missing Skill WF")
    manifest["nodes"]["agent"]["skills"] = ["not-registered"]

    preview = client.post(f"{PREFIX}/workflows/import/preview", json={"manifest": manifest})
    assert preview.status_code == 200
    assert preview.json()["data"]["ready_to_import"] is False
    assert any(
        item["kind"] == "skill"
        and item["reference"] == "not-registered"
        and item["status"] == "unresolved"
        for item in preview.json()["data"]["dependencies"]
    )

    imported = client.post(f"{PREFIX}/workflows/import", json={"manifest": manifest})
    assert imported.status_code == 400
    assert "preflight" in imported.json()["detail"]


def test_import_rewrites_manifest_owner_to_authenticated_actor(client: TestClient) -> None:
    manifest = make_manifest("owner_wf", name="Owner WF", owner="@spoofed")
    r = client.post(f"{PREFIX}/workflows/import", json={"manifest": manifest})
    assert r.status_code == 201
    data = r.json()["data"]
    assert data["workflow"]["owner"] == "@test"
    assert data["version"]["manifest"]["owner"] == "@test"


def _managed_file_manifest(workflow_id: str, immutable_ref: dict[str, object]) -> dict:
    manifest = make_manifest(workflow_id, name="Managed Import")
    manifest["nodes"]["agent"] = {
        "id": "agent",
        "type": "file_input",
        "file_ref": immutable_ref,
    }
    manifest["edges"] = [
        {"id": "e1", "from": "start", "to": "agent", "map": {"msg": "path"}},
        {"id": "e2", "from": "agent", "to": "final", "map": {"text": "response"}},
    ]
    return manifest


def test_import_preflight_resolves_managed_file_only_in_selected_project(
    client: TestClient,
) -> None:
    project = client.post(
        f"{PREFIX}/projects",
        json={"name": "Managed import project"},
    ).json()["data"]
    project_headers = {"X-CALIBER-Project": project["project_id"]}
    uploaded = client.post(
        f"{PREFIX}/projects/{project['project_id']}/files",
        files={"file": ("source.txt", b"pinned import bytes", "text/plain")},
        data={"kind": "input"},
        headers=project_headers,
    )
    assert uploaded.status_code == 201, uploaded.text
    immutable_ref = uploaded.json()["data"]["immutable_ref"]
    manifest = _managed_file_manifest("managed_import", immutable_ref)

    preview = client.post(
        f"{PREFIX}/workflows/import/preview",
        json={"manifest": manifest},
        headers=project_headers,
    )

    assert preview.status_code == 200, preview.text
    assert preview.json()["data"]["ready_to_import"] is True
    assert any(
        item["kind"] == "managed_file"
        and item["status"] == "resolved"
        and item["reference"] == immutable_ref["file_ref"]
        for item in preview.json()["data"]["dependencies"]
    )

    without_project = client.post(
        f"{PREFIX}/workflows/import/preview",
        json={"manifest": manifest},
    )
    assert without_project.status_code == 200
    assert without_project.json()["data"]["ready_to_import"] is False
    assert any(
        item["kind"] == "managed_file" and item["status"] == "unresolved"
        for item in without_project.json()["data"]["dependencies"]
    )
