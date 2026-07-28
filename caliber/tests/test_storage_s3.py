"""S3/MinIO backend tests via moto in-process mock (storage doc §3.5, Phase 2)."""

from __future__ import annotations

import pytest

pytest.importorskip("moto")
pytest.importorskip("boto3")

import boto3
from moto import mock_aws
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import caliber.db.models  # noqa: F401
from caliber.config import WorkflowStorageConfig
from caliber.db.base import Base
from caliber.db.models import CaliberProject
from caliber.storage import StorageConflictError, StorageNotFoundError, build_backend
from caliber.storage.s3 import S3StorageBackend
from caliber.storage.service import WorkingDirectoryService

BUCKET = "caliber-test"


def _cfg(**overrides: object) -> WorkflowStorageConfig:
    return WorkflowStorageConfig(
        backend="s3",
        bucket=BUCKET,
        region="us-east-1",
        prefix="tenant/local",
        **overrides,  # type: ignore[arg-type]
    )


def _make_bucket() -> None:
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)


@mock_aws
def test_build_backend_returns_s3() -> None:
    _make_bucket()
    backend = build_backend(_cfg())
    assert isinstance(backend, S3StorageBackend)
    assert backend.name == "s3"


@mock_aws
def test_write_read_stat_roundtrip() -> None:
    _make_bucket()
    backend = S3StorageBackend(_cfg())
    meta = backend.write_bytes("input/a.csv", b"a,b\n1,2\n", media_type="text/csv")
    assert meta.size_bytes == 8
    assert meta.sha256 and len(meta.sha256) == 64
    assert backend.read_bytes("input/a.csv") == b"a,b\n1,2\n"
    assert backend.read_bytes("input/a.csv", byte_range=(0, 2)) == b"a,b"
    assert backend.exists("input/a.csv")
    assert not backend.exists("input/missing.csv")
    st = backend.stat("input/a.csv")
    assert st.size_bytes == 8 and st.media_type == "text/csv"


@mock_aws
def test_no_overwrite_conflict_and_missing() -> None:
    _make_bucket()
    backend = S3StorageBackend(_cfg())
    backend.write_bytes("f.txt", b"one")
    with pytest.raises(StorageConflictError):
        backend.write_bytes("f.txt", b"two", overwrite=False)
    backend.write_bytes("f.txt", b"three", overwrite=True)
    assert backend.read_bytes("f.txt") == b"three"
    with pytest.raises(StorageNotFoundError):
        backend.read_bytes("nope.txt")


@mock_aws
def test_list_copy_move_delete() -> None:
    _make_bucket()
    backend = S3StorageBackend(_cfg())
    backend.write_bytes("d/one.txt", b"1")
    backend.write_bytes("d/sub/two.txt", b"2")
    items, _cursor = backend.list("d")
    names = sorted(m.name for m in items)
    assert names == ["one.txt", "two.txt"]
    backend.copy("d/one.txt", "d/copy.txt")
    assert backend.read_bytes("d/copy.txt") == b"1"
    backend.move("d/copy.txt", "d/moved.txt")
    assert backend.exists("d/moved.txt") and not backend.exists("d/copy.txt")
    backend.delete("d/moved.txt")
    assert not backend.exists("d/moved.txt")


@mock_aws
def test_signed_urls() -> None:
    _make_bucket()
    backend = S3StorageBackend(_cfg(signed_url_max_ttl_seconds=3600))
    backend.write_bytes("artifacts/r.pdf", b"%PDF")
    get = backend.signed_url("artifacts/r.pdf", method="GET", expires_seconds=600)
    assert get.method == "GET" and get.url.startswith("http")
    put = backend.signed_url("artifacts/up.bin", method="PUT", expires_seconds=999_999)
    assert put.method == "PUT" and "http" in put.url  # TTL capped, still issued


@mock_aws
def test_working_dir_service_over_s3() -> None:
    """The full service layer works unchanged over the S3 backend."""
    _make_bucket()
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    svc = WorkingDirectoryService(S3StorageBackend(_cfg()), _cfg())
    try:
        with sessionmaker(engine)() as s:
            ctx = svc.create_run_workspace(workflow_id="WF-1", workflow_run_id="WR-1")
            rec = svc.register_upload(
                s,
                ctx,
                kind="input",
                filename="data.csv",
                data=b"x,y\n",
                media_type="text/csv",
                actor="@me",
            )
            s.flush()
            assert rec.file_ref == "caliber://workflow-runs/WR-1/input/data.csv"
            fh = svc.open_for_tool(s, rec.file_ref, run_id="WR-1", actor="@me")
            assert fh.read() == b"x,y\n"
            fh.close()
    finally:
        engine.dispose()


@mock_aws
def test_project_directory_files_over_s3_are_agent_readable() -> None:
    """Project/File Directory refs work over S3-compatible storage."""
    _make_bucket()
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cfg = _cfg()
    svc = WorkingDirectoryService(S3StorageBackend(cfg), cfg)
    try:
        with sessionmaker(engine)() as s:
            s.add(CaliberProject(project_id="PRJ-s3", name="Blob Directory", owner="@me"))
            folder = svc.create_project_folder(
                s,
                project_id="PRJ-s3",
                path="datasets/raw",
                actor="@me",
            )
            rec = svc.register_project_file(
                s,
                project_id="PRJ-s3",
                kind="input",
                filename="datasets/raw/cases.csv",
                data=b"id,text\n1,hello\n",
                media_type="text/csv",
                actor="@me",
            )
            s.flush()

            assert (
                folder.file_ref == "caliber://projects/PRJ-s3/metadata/datasets/raw/.caliber-folder"
            )
            assert rec.file_ref == "caliber://projects/PRJ-s3/input/datasets/raw/cases.csv"
            assert rec.storage_backend == "s3"
            ctx = svc.create_run_workspace(
                project_id="PRJ-s3", workflow_id="WF-1", workflow_run_id="WR-1"
            )
            fh = svc.open_for_tool(s, rec.file_ref, ctx=ctx, actor="@runtime")
            try:
                assert fh.read() == b"id,text\n1,hello\n"
            finally:
                fh.close()
    finally:
        engine.dispose()
