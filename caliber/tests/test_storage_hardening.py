"""Phase 5 hardening: content sniff, zip-slip/bomb, retention cleanup (storage doc §8)."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import caliber.db.models  # noqa: F401
from caliber.config import RetentionConfig, WorkflowStorageConfig
from caliber.db.base import Base
from caliber.storage import (
    LocalStorageBackend,
    StorageValidationError,
    WorkingDirectoryService,
    retention_days_for,
    safe_zip_members,
    sniff_media_type,
)


# ----- content sniffing ---------------------------------------------------- #
def test_sniff_magic_bytes_beat_extension() -> None:
    assert sniff_media_type(b"%PDF-1.7\n...", "evil.txt") == "application/pdf"
    assert sniff_media_type(b"\x89PNG\r\n\x1a\n", "x.bin") == "image/png"
    assert sniff_media_type(b"plain text", "notes.csv") == "text/csv"  # extension fallback
    assert sniff_media_type(b"\x00\x01\x02", "noext") is None


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


# ----- zip safety ---------------------------------------------------------- #
def test_safe_zip_accepts_clean_archive() -> None:
    members = safe_zip_members(_zip_bytes({"a.txt": b"1", "sub/b.txt": b"2"}))
    assert {m.name for m in members} == {"a.txt", "sub/b.txt"}


def test_zip_slip_entry_rejected() -> None:
    data = _zip_bytes({"../../escape.txt": b"x"})
    with pytest.raises(StorageValidationError, match="unsafe archive entry"):
        safe_zip_members(data)


def test_zip_entry_count_capped() -> None:
    data = _zip_bytes({f"f{i}.txt": b"x" for i in range(5)})
    with pytest.raises(StorageValidationError, match="entries"):
        safe_zip_members(data, max_entries=3)


def test_zip_bomb_total_size_capped() -> None:
    data = _zip_bytes({"big.txt": b"A" * 10_000})
    with pytest.raises(StorageValidationError, match="zip bomb"):
        safe_zip_members(data, max_total_bytes=1000)


def test_not_a_zip_rejected() -> None:
    with pytest.raises(StorageValidationError, match="not a valid zip"):
        safe_zip_members(b"definitely not a zip")


# ----- retention ----------------------------------------------------------- #
def test_retention_days_by_status() -> None:
    r = RetentionConfig()
    assert retention_days_for("completed", r) == r.default_run_days
    assert retention_days_for("error", r) == r.failed_run_days
    assert retention_days_for("completed", r, is_preview=True) == r.preview_run_days
    assert retention_days_for("completed", r, is_eval=True) == r.eval_run_days


@pytest.fixture
def service(tmp_path: Path) -> Iterator[tuple[WorkingDirectoryService, Session]]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cfg = WorkflowStorageConfig(base_uri=f"file://{tmp_path}/ws")
    svc = WorkingDirectoryService(LocalStorageBackend(cfg.base_uri), cfg)
    try:
        with sessionmaker(engine)() as s:
            yield svc, s
    finally:
        engine.dispose()


def test_cleanup_run_files_removes_and_soft_deletes(service) -> None:
    svc, s = service
    ctx = svc.create_run_workspace(workflow_id="WF-1", workflow_run_id="WR-1")
    rec = svc.register_upload(
        s,
        ctx,
        kind="work",
        filename="scratch.txt",
        data=b"tmp",
        media_type="text/plain",
        actor="@me",
    )
    s.flush()
    removed = svc.cleanup_run_files(s, "WR-1")
    s.flush()
    assert removed == 1
    row = svc.get_row(s, rec.file_id)
    assert row is not None and row.status == "deleted" and row.deleted_at is not None
    # the physical object is gone
    assert not svc._backend.exists(row.object_key)


def test_cleanup_kind_filter(service) -> None:
    svc, s = service
    ctx = svc.create_run_workspace(workflow_id="WF-1", workflow_run_id="WR-2")
    svc.register_upload(
        s, ctx, kind="input", filename="keep.txt", data=b"k", media_type=None, actor="@me"
    )
    svc.write_artifact(
        s, ctx, path="scratch.txt", data=b"t", media_type=None, actor="@me", kind="tmp"
    )
    s.flush()
    removed = svc.cleanup_run_files(s, "WR-2", kinds=("tmp",))
    assert removed == 1
    # input survives
    summary = svc.run_file_summary(s, "WR-2")
    assert summary["file_counts"].get("input") == 1
