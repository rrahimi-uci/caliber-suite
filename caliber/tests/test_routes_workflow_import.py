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
    assert data["workflow"]["workflow_id"] == "imported_wf"
    assert data["version"]["status"] == "draft"
    assert data["version"]["version_number"] == 1


def test_import_from_manifest_yaml(client: TestClient) -> None:
    manifest = make_manifest("yaml_wf", name="YAML WF")
    r = client.post(
        f"{PREFIX}/workflows/import",
        json={"manifest_yaml": yaml.safe_dump(manifest)},
    )
    assert r.status_code == 201, r.text
    assert r.json()["data"]["workflow"]["workflow_id"] == "yaml_wf"


def test_import_existing_workflow_appends_version(client: TestClient) -> None:
    manifest = make_manifest("again_wf", name="Again WF")
    client.post(f"{PREFIX}/workflows/import", json={"manifest": manifest})
    r = client.post(f"{PREFIX}/workflows/import", json={"manifest": manifest})
    assert r.status_code == 201
    assert r.json()["data"]["version"]["version_number"] == 2


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
