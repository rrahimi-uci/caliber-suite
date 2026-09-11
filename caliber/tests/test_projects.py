"""Project (workspace) routes: CRUD + project-scoped file upload/list/download."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.config import WorkflowStorageConfig
from caliber.db.models import CaliberProject, CaliberWorkspaceEnvironment
from caliber.storage import LocalStorageBackend, WorkingDirectoryService

PREFIX = "/ajax-api/2.0/mlflow/caliber"


@pytest.fixture
def proj_client(client: TestClient, tmp_path: Path) -> TestClient:
    cfg = WorkflowStorageConfig(base_uri=f"file://{tmp_path}/ws")
    client.app.state.config = client.app.state.config.model_copy(update={"workflow_storage": cfg})
    client.app.state.working_dir_service = WorkingDirectoryService(
        LocalStorageBackend(cfg.base_uri), cfg
    )
    return client


def _create(client: TestClient, name: str = "Acme Support") -> str:
    resp = client.post(f"{PREFIX}/projects", json={"name": name, "description": "demo"})
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["project_id"]


def test_create_list_get_project(proj_client: TestClient) -> None:
    pid = _create(proj_client)
    assert pid.startswith("PRJ-")

    listing = proj_client.get(f"{PREFIX}/projects").json()["data"]
    assert any(
        p["project_id"] == pid and p["file_count"] == 0 and p["storage_backend"] == "local"
        for p in listing
    )

    detail = proj_client.get(f"{PREFIX}/projects/{pid}").json()["data"]
    assert detail["name"] == "Acme Support" and detail["file_count"] == 0


def test_admin_lists_only_their_own_projects(proj_client: TestClient, db_session: Session) -> None:
    """`P1-B`: `caliber.admin` is no longer an implicit workspace role, so
    `list_projects` must apply the owner/member filter to admins too --
    the regression-proving test for the bypass removal *and* its coupled
    `list_projects` fix (an admin viewing a mix of owned and non-owned
    projects used to crash with an uncaught 404 the moment the
    `project_role()` bypass alone was removed without this fix)."""
    mine = _create(proj_client, "Mine")
    db_session.add(CaliberProject(project_id="PRJ-someone-elses", name="Not mine", owner="@other"))
    db_session.commit()

    resp = proj_client.get(f"{PREFIX}/projects")
    assert resp.status_code == 200, resp.text
    ids = {p["project_id"] for p in resp.json()["data"]}
    assert mine in ids
    assert "PRJ-someone-elses" not in ids


def test_project_storage_endpoint_reports_active_backend(proj_client: TestClient) -> None:
    resp = proj_client.get(f"{PREFIX}/projects/storage")
    assert resp.status_code == 200
    payload = resp.json()["data"]
    assert payload["backend"] == "local"
    assert payload["backend_label"] == "Local file system"
    assert payload["available_backends"][0]["id"] == "local"
    assert payload["available_backends"][1]["id"] == "s3"
    assert payload["available_backends"][1]["configured"] is False
    assert payload["base_uri"].startswith("file://")


def test_project_storage_endpoint_reports_configured_minio(
    proj_client: TestClient, tmp_path: Path
) -> None:
    cfg = WorkflowStorageConfig(
        backend="local",
        base_uri=f"file://{tmp_path}/ws",
        bucket="caliber-test",
        region="us-east-1",
    )
    proj_client.app.state.config = proj_client.app.state.config.model_copy(
        update={"workflow_storage": cfg}
    )
    proj_client.app.state.working_dir_service = WorkingDirectoryService(
        LocalStorageBackend(cfg.base_uri), cfg
    )

    payload = proj_client.get(f"{PREFIX}/projects/storage").json()["data"]
    s3 = next(item for item in payload["available_backends"] if item["id"] == "s3")
    assert s3["label"] == "MinIO / S3-compatible object storage"
    assert s3["configured"] is True
    assert payload["bucket"] == "caliber-test"


def test_duplicate_name_conflicts(proj_client: TestClient) -> None:
    _create(proj_client, "Billing")
    resp = proj_client.post(f"{PREFIX}/projects", json={"name": "Billing"})
    assert resp.status_code == 409


def test_create_requires_name_and_operator(proj_client: TestClient) -> None:
    assert proj_client.post(f"{PREFIX}/projects", json={}).status_code == 400
    assert (
        proj_client.post(
            f"{PREFIX}/projects",
            json={"name": "X", "storage_backend": "minio"},
        ).status_code
        == 400
    )
    resp = proj_client.post(
        f"{PREFIX}/projects",
        json={"name": "X"},
        headers={"X-CALIBER-User": "@viewer-only"},
    )
    assert resp.status_code == 403


def test_create_project_seeds_slug_source_mode_and_environments(
    proj_client: TestClient, db_session: Session
) -> None:
    """`P1-A`: creation transactionally derives a slug, defaults the source
    mode, and seeds all four fixed environments (dev active, the rest
    disabled) -- none of this is in the response schema yet (a separate,
    later slice), so this asserts against the DB directly."""
    pid = _create(proj_client, "Mortgage Underwriting")

    project = db_session.execute(
        select(CaliberProject).where(CaliberProject.project_id == pid)
    ).scalar_one()
    assert project.slug == "mortgage-underwriting"
    assert project.source_mode == "caliber_managed"

    environments = (
        db_session.execute(
            select(CaliberWorkspaceEnvironment)
            .where(CaliberWorkspaceEnvironment.project_id == pid)
            .order_by(CaliberWorkspaceEnvironment.promotion_order)
        )
        .scalars()
        .all()
    )
    assert [(e.name, e.environment_class, e.promotion_order, e.status) for e in environments] == [
        ("dev", "development", 10, "active"),
        ("qa", "qa", 20, "disabled"),
        ("staging", "staging", 30, "disabled"),
        ("prod", "production", 40, "disabled"),
    ]


def test_create_project_slug_collision_gets_a_deterministic_suffix(
    proj_client: TestClient, db_session: Session
) -> None:
    """Two projects whose names slugify identically (``"Demo"`` and
    ``"demo!"``) must not collide -- the second gets a numeric suffix, the
    same strategy migration 0093's backfill uses for pre-existing rows."""
    first = _create(proj_client, "Demo")
    second = _create(proj_client, "demo!")

    slugs = {
        pid: db_session.execute(
            select(CaliberProject.slug).where(CaliberProject.project_id == pid)
        ).scalar_one()
        for pid in (first, second)
    }
    assert slugs[first] == "demo"
    assert slugs[second] == "demo-2"


def test_create_requires_both_operator_and_approver(proj_client: TestClient) -> None:
    """`P1-A`: `project.create` is now an AND of two scopes, not an OR --
    holding only one is not enough. The default test identity (`@test`) is
    admin-scoped, which implies both, so this reconfigures the fixture app
    with dedicated single-scope users to prove the conjunction directly."""
    proj_client.app.state.config = proj_client.app.state.config.model_copy(
        update={"operator_users": "@op-only", "approver_users": "@appr-only"}
    )

    operator_only = proj_client.post(
        f"{PREFIX}/projects",
        json={"name": "Operator Only"},
        headers={"X-CALIBER-User": "@op-only"},
    )
    assert operator_only.status_code == 403

    approver_only = proj_client.post(
        f"{PREFIX}/projects",
        json={"name": "Approver Only"},
        headers={"X-CALIBER-User": "@appr-only"},
    )
    assert approver_only.status_code == 403

    proj_client.app.state.config = proj_client.app.state.config.model_copy(
        update={"operator_users": "@both", "approver_users": "@both"}
    )
    both = proj_client.post(
        f"{PREFIX}/projects",
        json={"name": "Both Scopes"},
        headers={"X-CALIBER-User": "@both"},
    )
    assert both.status_code == 201, both.text


def test_update_project_rename_and_archive(proj_client: TestClient) -> None:
    pid = _create(proj_client, "Old Name")
    resp = proj_client.patch(
        f"{PREFIX}/projects/{pid}", json={"name": "New Name", "status": "archived"}
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["name"] == "New Name"
    # archived projects are excluded from the default list
    listing = proj_client.get(f"{PREFIX}/projects").json()["data"]
    assert all(p["project_id"] != pid for p in listing)
    # ...but visible with ?status=all
    all_listing = proj_client.get(f"{PREFIX}/projects?status=all").json()["data"]
    assert any(p["project_id"] == pid for p in all_listing)


def test_project_membership_roles_and_permissions(proj_client: TestClient) -> None:
    pid = _create(proj_client, "Access controlled project")

    detail = proj_client.get(f"{PREFIX}/projects/{pid}").json()["data"]
    assert detail["access_role"] == "owner"
    assert "project.manage_members" in detail["permissions"]

    added = proj_client.post(
        f"{PREFIX}/projects/{pid}/members",
        json={"user_id": "@reader", "role": "viewer"},
    )
    assert added.status_code == 201, added.text
    assert added.json()["data"]["role"] == "viewer"

    reader_headers = {"X-CALIBER-User": "@reader"}
    reader_detail = proj_client.get(f"{PREFIX}/projects/{pid}", headers=reader_headers)
    assert reader_detail.status_code == 200
    assert reader_detail.json()["data"]["access_role"] == "viewer"
    assert (
        proj_client.patch(
            f"{PREFIX}/projects/{pid}",
            json={"name": "should not change"},
            headers=reader_headers,
        ).status_code
        == 403
    )

    members = proj_client.get(f"{PREFIX}/projects/{pid}/members", headers=reader_headers)
    assert members.status_code == 200
    assert {row["user_id"] for row in members.json()["data"]["members"]} == {"@test", "@reader"}

    removed = proj_client.delete(f"{PREFIX}/projects/{pid}/members/@reader")
    assert removed.status_code == 200
    assert proj_client.get(f"{PREFIX}/projects/{pid}", headers=reader_headers).status_code == 404


def test_upload_list_download_project_file(proj_client: TestClient) -> None:
    pid = _create(proj_client)
    resp = proj_client.post(
        f"{PREFIX}/projects/{pid}/files",
        files={"file": ("policy.md", b"# Refund policy\n", "text/markdown")},
        data={"kind": "input", "path": "policies/refund/policy.md"},
    )
    assert resp.status_code == 201, resp.text
    rec = resp.json()["data"]
    assert rec["file_ref"] == f"caliber://projects/{pid}/input/policies/refund/policy.md"
    assert rec["relative_path"] == "policies/refund/policy.md"
    assert rec["storage_backend"] == "local"
    assert rec["project_id"] == pid
    file_id = rec["file_id"]

    items = proj_client.get(f"{PREFIX}/projects/{pid}/files").json()["data"]["items"]
    assert [i["file_id"] for i in items] == [file_id]
    directories = proj_client.get(f"{PREFIX}/projects/{pid}/files").json()["data"]["directories"]
    assert [d["path"] for d in directories] == ["policies", "policies/refund"]

    # project file_count reflects the upload
    assert proj_client.get(f"{PREFIX}/projects/{pid}").json()["data"]["file_count"] == 1

    content = proj_client.get(f"{PREFIX}/projects/{pid}/files/{file_id}/content")
    assert content.status_code == 200
    assert content.content == b"# Refund policy\n"
    assert content.headers["x-content-type-options"] == "nosniff"


def test_create_project_folder_is_idempotent_and_hidden_from_files(proj_client: TestClient) -> None:
    pid = _create(proj_client)
    resp = proj_client.post(f"{PREFIX}/projects/{pid}/folders", json={"path": "datasets/raw"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["path"] == "datasets/raw"
    assert resp.json()["data"]["file_ref"] == (
        f"caliber://projects/{pid}/metadata/datasets/raw/.caliber-folder"
    )
    again = proj_client.post(f"{PREFIX}/projects/{pid}/folders", json={"path": "datasets/raw"})
    assert again.status_code == 201

    payload = proj_client.get(f"{PREFIX}/projects/{pid}/files").json()["data"]
    assert payload["items"] == []
    assert [d["path"] for d in payload["directories"]] == ["datasets", "datasets/raw"]
    assert proj_client.get(f"{PREFIX}/projects/{pid}").json()["data"]["file_count"] == 0


def test_minio_backed_project_folder_and_file(proj_client: TestClient, tmp_path: Path) -> None:
    moto = pytest.importorskip("moto")
    boto3 = pytest.importorskip("boto3")
    bucket = "caliber-test"
    cfg = WorkflowStorageConfig(
        backend="local",
        base_uri=f"file://{tmp_path}/ws",
        bucket=bucket,
        region="us-east-1",
    )

    with moto.mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=bucket)
        proj_client.app.state.config = proj_client.app.state.config.model_copy(
            update={"workflow_storage": cfg}
        )
        proj_client.app.state.working_dir_service = WorkingDirectoryService(
            LocalStorageBackend(cfg.base_uri), cfg
        )

        project_resp = proj_client.post(
            f"{PREFIX}/projects",
            json={"name": "Blob Directory", "storage_backend": "minio"},
        )
        assert project_resp.status_code == 201, project_resp.text
        pid = project_resp.json()["data"]["project_id"]
        assert project_resp.json()["data"]["storage_backend"] == "s3"

        folder_resp = proj_client.post(
            f"{PREFIX}/projects/{pid}/folders", json={"path": "datasets/raw"}
        )
        assert folder_resp.status_code == 201, folder_resp.text
        assert folder_resp.json()["data"]["storage_backend"] == "s3"

        file_resp = proj_client.post(
            f"{PREFIX}/projects/{pid}/files",
            files={"file": ("cases.csv", b"id,text\n1,hello\n", "text/csv")},
            data={"kind": "input", "path": "datasets/raw/cases.csv"},
        )
        assert file_resp.status_code == 201, file_resp.text
        file_id = file_resp.json()["data"]["file_id"]
        assert file_resp.json()["data"]["storage_backend"] == "s3"
        assert proj_client.get(f"{PREFIX}/projects/{pid}/files/{file_id}/content").content == (
            b"id,text\n1,hello\n"
        )


def test_project_file_isolation(proj_client: TestClient) -> None:
    pid_a = _create(proj_client, "A")
    pid_b = _create(proj_client, "B")
    file_id = proj_client.post(
        f"{PREFIX}/projects/{pid_a}/files",
        files={"file": ("a.txt", b"x", "text/plain")},
        data={"kind": "input"},
    ).json()["data"]["file_id"]
    # B cannot see/download A's file
    assert proj_client.get(f"{PREFIX}/projects/{pid_b}/files/{file_id}/content").status_code == 404
    assert proj_client.get(f"{PREFIX}/projects/{pid_b}/files").json()["data"]["items"] == []


def test_delete_project_file_soft(proj_client: TestClient) -> None:
    pid = _create(proj_client)
    file_id = proj_client.post(
        f"{PREFIX}/projects/{pid}/files",
        files={"file": ("d.txt", b"x", "text/plain")},
        data={"kind": "input"},
    ).json()["data"]["file_id"]
    assert proj_client.delete(f"{PREFIX}/projects/{pid}/files/{file_id}").status_code == 200
    # gone from the list
    assert proj_client.get(f"{PREFIX}/projects/{pid}/files").json()["data"]["items"] == []


def test_upload_requires_operator(proj_client: TestClient) -> None:
    pid = _create(proj_client)
    resp = proj_client.post(
        f"{PREFIX}/projects/{pid}/files",
        files={"file": ("a.txt", b"x", "text/plain")},
        data={"kind": "input"},
        headers={"X-CALIBER-User": "@viewer-only"},
    )
    assert resp.status_code == 403


def test_missing_project_404(proj_client: TestClient) -> None:
    assert proj_client.get(f"{PREFIX}/projects/PRJ-nope").status_code == 404
    assert proj_client.get(f"{PREFIX}/projects/PRJ-nope/files").status_code == 404
