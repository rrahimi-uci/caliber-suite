"""Phase 3: playground file routes (storage doc §6)."""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from caliber.config import WorkflowStorageConfig
from caliber.storage import LocalStorageBackend, WorkingDirectoryService

PREFIX = "/ajax-api/2.0/mlflow/caliber"


@pytest.fixture
def pg_client(client: TestClient, tmp_path: Path) -> TestClient:
    cfg = WorkflowStorageConfig(base_uri=f"file://{tmp_path}/ws")
    client.app.state.working_dir_service = WorkingDirectoryService(
        LocalStorageBackend(cfg.base_uri), cfg
    )
    return client


def test_playground_upload_list_download(pg_client: TestClient) -> None:
    run = "PGR-abc123"
    # upload (no pre-existing run row needed — playground namespace is lightweight)
    resp = pg_client.post(
        f"{PREFIX}/playground-runs/{run}/files",
        files={"file": ("source.txt", b"playground data", "text/plain")},
        data={"kind": "input"},
    )
    assert resp.status_code == 201, resp.text
    rec = resp.json()["data"]
    assert rec["file_ref"] == f"caliber://playground-runs/{run}/input/source.txt"
    assert rec["playground_run_id"] == run
    file_id = rec["file_id"]

    # list
    items = pg_client.get(f"{PREFIX}/playground-runs/{run}/files").json()["data"]["items"]
    assert [i["file_id"] for i in items] == [file_id]

    # download
    resp = pg_client.get(f"{PREFIX}/playground-runs/{run}/files/{file_id}/content")
    assert resp.status_code == 200
    assert resp.content == b"playground data"
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_playground_isolation(pg_client: TestClient) -> None:
    file_id = pg_client.post(
        f"{PREFIX}/playground-runs/PGR-1/files",
        files={"file": ("a.txt", b"x", "text/plain")},
        data={"kind": "input"},
    ).json()["data"]["file_id"]
    # another playground run cannot see/download it
    assert (
        pg_client.get(f"{PREFIX}/playground-runs/PGR-2/files/{file_id}/content").status_code == 404
    )
    assert pg_client.get(f"{PREFIX}/playground-runs/PGR-2/files").json()["data"]["items"] == []


def test_playground_upload_requires_operator(pg_client: TestClient) -> None:
    resp = pg_client.post(
        f"{PREFIX}/playground-runs/PGR-1/files",
        files={"file": ("a.txt", b"x", "text/plain")},
        data={"kind": "input"},
        headers={"X-CALIBER-User": "@viewer-only"},
    )
    assert resp.status_code == 403
