"""Agent-facing file tool tests (storage doc §4.4)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import caliber.db.models  # noqa: F401
from caliber.config import WorkflowStorageConfig
from caliber.db.base import Base
from caliber.db.models import CaliberProject
from caliber.storage import LocalStorageBackend, WorkingDirectoryService
from caliber.workflows import file_tools


@pytest.fixture
def env(tmp_path: Path) -> Iterator[tuple[WorkingDirectoryService, Session, object]]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cfg = WorkflowStorageConfig(base_uri=f"file://{tmp_path}/ws")
    svc = WorkingDirectoryService(LocalStorageBackend(cfg.base_uri), cfg)
    try:
        with sessionmaker(engine)() as session:
            ctx = svc.create_run_workspace(workflow_id="WF-1", workflow_run_id="WR-1")
            yield svc, session, ctx
    finally:
        engine.dispose()


def test_write_then_read_and_list(env) -> None:
    svc, session, ctx = env
    written = file_tools.write_workdir_file(
        svc, session, ctx, path="notes/summary.txt", content="hello tools", node_id="n1"
    )
    session.flush()
    assert written["kind"] == "work"

    read = file_tools.read_workdir_file(svc, session, ctx, file_ref=written["file_ref"])
    assert read["content"] == "hello tools"
    assert read["truncated"] is False

    listing = file_tools.list_workdir_files(session, ctx)
    assert any(f["file_ref"] == written["file_ref"] for f in listing["files"])


def test_read_respects_max_bytes(env) -> None:
    svc, session, ctx = env
    rec = file_tools.write_workdir_file(svc, session, ctx, path="big.txt", content="abcdefghij")
    session.flush()
    out = file_tools.read_workdir_file(svc, session, ctx, file_ref=rec["file_ref"], max_bytes=4)
    assert out["content"] == "abcd"
    assert out["truncated"] is True


def test_create_artifact_marks_artifact(env) -> None:
    svc, session, ctx = env
    rec = file_tools.create_artifact(
        svc, session, ctx, path="report.md", content="# Report", node_id="reporter"
    )
    session.flush()
    assert rec["kind"] == "artifact"
    assert rec["status"] == "artifact"


def test_get_file_metadata(env) -> None:
    svc, session, ctx = env
    rec = file_tools.write_workdir_file(svc, session, ctx, path="m.txt", content="data")
    session.flush()
    meta = file_tools.get_file_metadata(svc, session, ctx, file_ref=rec["file_ref"])
    assert meta["size_bytes"] == 4
    assert meta["sha256"]


def test_project_file_ref_is_agent_readable(env) -> None:
    svc, session, ctx = env
    project = CaliberProject(project_id="PRJ-1", name="Directory", owner="@me")
    session.add(project)
    rec = svc.register_project_file(
        session,
        project_id="PRJ-1",
        kind="input",
        filename="policies/refund.md",
        data=b"refund policy",
        media_type="text/markdown",
        actor="@me",
    )
    session.flush()

    read = file_tools.read_workdir_file(svc, session, ctx, file_ref=rec.file_ref)
    assert read["content"] == "refund policy"
    meta = file_tools.get_file_metadata(svc, session, ctx, file_ref=rec.file_ref)
    assert meta["file_ref"] == rec.file_ref
    assert meta["storage_backend"] == "local"


def test_write_requires_content(env) -> None:
    svc, session, ctx = env
    with pytest.raises(ValueError, match="content"):
        file_tools.write_workdir_file(svc, session, ctx, path="x.txt")
