"""File/workspace route tests (storage doc §4.7): upload/list/get/content/artifact + auth."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from caliber.config import WorkflowStorageConfig
from caliber.db.models import CaliberProject, CaliberWorkflowFile, CaliberWorkflowRun
from caliber.storage import LocalStorageBackend, WorkingDirectoryService

PREFIX = "/ajax-api/2.0/mlflow/caliber"


@pytest.fixture
def files_client(client: TestClient, tmp_path: Path) -> TestClient:
    """Client with the storage service pointed at an isolated temp dir."""
    cfg = WorkflowStorageConfig(base_uri=f"file://{tmp_path}/ws")
    client.app.state.working_dir_service = WorkingDirectoryService(
        LocalStorageBackend(cfg.base_uri), cfg
    )
    return client


@pytest.fixture
def run_id(session_factory: sessionmaker[Session]) -> str:
    with session_factory() as s:
        s.add(CaliberWorkflowRun(workflow_run_id="WR-T1", workflow_id="WF-1"))
        s.commit()
    return "WR-T1"


def _upload(client: TestClient, url: str, name: str = "data.csv", kind: str = "input"):
    return client.post(
        url,
        files={"file": (name, b"a,b\n1,2\n", "text/csv")},
        data={"kind": kind},
    )


def test_run_upload_list_get_content(files_client: TestClient, run_id: str) -> None:
    # upload
    resp = _upload(files_client, f"{PREFIX}/workflow-runs/{run_id}/files")
    assert resp.status_code == 201, resp.text
    rec = resp.json()["data"]
    file_id = rec["file_id"]
    assert rec["file_ref"] == f"caliber://workflow-runs/{run_id}/input/data.csv"
    assert rec["kind"] == "input"

    # list
    resp = files_client.get(f"{PREFIX}/workflow-runs/{run_id}/files")
    assert resp.status_code == 200
    items = resp.json()["data"]["items"]
    assert len(items) == 1 and items[0]["file_id"] == file_id

    # list with kind filter
    assert (
        files_client.get(f"{PREFIX}/workflow-runs/{run_id}/files?kind=work").json()["data"]["items"]
        == []
    )

    # get metadata
    resp = files_client.get(f"{PREFIX}/workflow-runs/{run_id}/files/{file_id}")
    assert resp.status_code == 200
    assert resp.json()["data"]["sha256"]

    # content proxy: attachment + nosniff, bytes match
    resp = files_client.get(f"{PREFIX}/workflow-runs/{run_id}/files/{file_id}/content")
    assert resp.status_code == 200
    assert resp.content == b"a,b\n1,2\n"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "attachment" in resp.headers["content-disposition"]


def test_staging_upload_then_list_empty_for_run(files_client: TestClient, run_id: str) -> None:
    resp = files_client.post(
        f"{PREFIX}/workflow-files",
        files={"file": ("in.txt", b"hi", "text/plain")},
        data={"kind": "input"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["workflow_run_id"] is None
    # staged file is not bound to the run, so the run lists nothing
    assert files_client.get(f"{PREFIX}/workflow-runs/{run_id}/files").json()["data"]["items"] == []


def test_project_staging_and_run_upload_keep_tenant_project_scope(
    files_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        session.add(
            CaliberProject(
                project_id="PRJ-files",
                tenant_id="tenant-files",
                name="Scoped files",
                owner="@test",
            )
        )
        session.add(
            CaliberWorkflowRun(
                workflow_run_id="WR-project",
                workflow_id="WF-project",
                project_id="PRJ-files",
                tenant_id="tenant-files",
            )
        )
        session.commit()
    headers = {"X-CALIBER-Project": "PRJ-files"}

    staged_response = files_client.post(
        f"{PREFIX}/workflow-files",
        headers=headers,
        files={"file": ("staged.txt", b"staged", "text/plain")},
        data={"kind": "input"},
    )
    assert staged_response.status_code == 201, staged_response.text
    staged_id = staged_response.json()["data"]["file_id"]

    run_response = files_client.post(
        f"{PREFIX}/workflow-runs/WR-project/files",
        headers=headers,
        files={"file": ("direct.txt", b"direct", "text/plain")},
        data={"kind": "input"},
    )
    assert run_response.status_code == 201, run_response.text
    direct_id = run_response.json()["data"]["file_id"]

    service = files_client.app.state.working_dir_service
    with session_factory() as session:
        staged = session.get(CaliberWorkflowFile, staged_id)
        direct = session.get(CaliberWorkflowFile, direct_id)
        assert staged is not None and direct is not None
        assert (staged.tenant_id, staged.project_id) == ("tenant-files", "PRJ-files")
        assert (direct.tenant_id, direct.project_id) == ("tenant-files", "PRJ-files")
        ctx = service.create_run_workspace(
            tenant_id="tenant-files",
            project_id="PRJ-files",
            workflow_id="WF-project",
            workflow_run_id="WR-project",
        )
        bound = service.materialize_input_files(
            session,
            ctx,
            [{"file_id": staged_id}],
            actor="@test",
        )
        assert len(bound) == 1
        assert bound[0].workflow_run_id == "WR-project"


def test_idor_file_from_other_run_not_visible(
    files_client: TestClient, run_id: str, session_factory: sessionmaker[Session]
) -> None:
    # upload into WR-T1
    file_id = _upload(files_client, f"{PREFIX}/workflow-runs/{run_id}/files").json()["data"][
        "file_id"
    ]
    # a second run
    with session_factory() as s:
        s.add(CaliberWorkflowRun(workflow_run_id="WR-T2", workflow_id="WF-1"))
        s.commit()
    # fetching WR-T1's file via WR-T2's path must 404 (no IDOR)
    assert files_client.get(f"{PREFIX}/workflow-runs/WR-T2/files/{file_id}").status_code == 404
    assert (
        files_client.get(f"{PREFIX}/workflow-runs/WR-T2/files/{file_id}/content").status_code == 404
    )


def test_list_hides_pending_rows(
    files_client: TestClient, run_id: str, session_factory: sessionmaker[Session]
) -> None:
    with session_factory() as s:
        s.add(
            CaliberWorkflowFile(
                file_id="FILE-pending1",
                workflow_run_id=run_id,
                kind="input",
                name="ghost.bin",
                relative_path="ghost.bin",
                file_ref=f"caliber://workflow-runs/{run_id}/input/ghost.bin",
                storage_backend="local",
                storage_uri="file:///x",
                object_key="k",
                status="pending_upload",
                created_by="@me",
            )
        )
        s.commit()
    items = files_client.get(f"{PREFIX}/workflow-runs/{run_id}/files").json()["data"]["items"]
    assert all(i["file_id"] != "FILE-pending1" for i in items)


def test_register_artifact(files_client: TestClient, run_id: str) -> None:
    file_id = _upload(
        files_client, f"{PREFIX}/workflow-runs/{run_id}/files", name="out.csv", kind="work"
    ).json()["data"]["file_id"]
    resp = files_client.post(
        f"{PREFIX}/workflow-runs/{run_id}/artifacts",
        json={"file_id": file_id, "artifact_type": "report", "display_name": "Out"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["kind"] == "artifact"


def test_bad_kind_rejected(files_client: TestClient, run_id: str) -> None:
    resp = files_client.post(
        f"{PREFIX}/workflow-runs/{run_id}/files",
        files={"file": ("x.bin", b"x", "application/octet-stream")},
        data={"kind": "bogus"},
    )
    assert resp.status_code == 400


def test_upload_requires_operator_scope(files_client: TestClient, run_id: str) -> None:
    # authenticated but viewer-only user -> 403
    resp = files_client.post(
        f"{PREFIX}/workflow-runs/{run_id}/files",
        files={"file": ("x.csv", b"x", "text/csv")},
        data={"kind": "input"},
        headers={"X-CALIBER-User": "@viewer-only"},
    )
    assert resp.status_code == 403


def test_upload_anonymous_rejected(files_client: TestClient, run_id: str) -> None:
    resp = files_client.post(
        f"{PREFIX}/workflow-runs/{run_id}/files",
        files={"file": ("x.csv", b"x", "text/csv")},
        data={"kind": "input"},
        headers={"X-CALIBER-User": ""},
    )
    assert resp.status_code == 401


def test_upload_to_missing_run_404(files_client: TestClient) -> None:
    resp = _upload(files_client, f"{PREFIX}/workflow-runs/WR-NOPE/files")
    assert resp.status_code == 404
