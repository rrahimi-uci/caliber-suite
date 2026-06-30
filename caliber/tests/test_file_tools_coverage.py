"""Targeted coverage for agent-facing file tools (storage doc §4.4).

Exercises the error/fallback branches not hit by ``tests/test_file_tools.py``:
base64 read fallback on non-UTF-8 bytes, ``content_base64`` writes, the
wrong-run / not-found ``get_file_metadata`` guard, ``kind``-filtered listing,
and the ``bind_run_read_tools`` callables (explicit-ref read, no-ref input
grounding with the ``remaining<=0`` break + base64 fallback, and ``_meta``).
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import caliber.db.models  # noqa: F401
from caliber.config import WorkflowStorageConfig
from caliber.db.base import Base
from caliber.storage import LocalStorageBackend, WorkingDirectoryService
from caliber.workflows import file_tools

# Bytes that are not valid UTF-8 (lone continuation byte) -> forces base64.
NON_UTF8 = b"\xff\xfe\x00\x01"


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


def test_read_falls_back_to_base64_on_non_utf8(env) -> None:
    """read_workdir_file returns base64 when the bytes are not decodable (lines 95-97)."""
    svc, session, ctx = env
    rec = file_tools.write_workdir_file(
        svc, session, ctx, path="blob.bin", content_base64=base64.b64encode(NON_UTF8).decode()
    )
    session.flush()
    out = file_tools.read_workdir_file(svc, session, ctx, file_ref=rec["file_ref"])
    assert out["encoding"] == "base64"
    assert "content" not in out
    assert base64.b64decode(out["content_base64"]) == NON_UTF8


def test_write_via_content_base64(env) -> None:
    """write_workdir_file decodes content_base64 into bytes (line 122)."""
    svc, session, ctx = env
    payload = b"round-trip via base64"
    rec = file_tools.write_workdir_file(
        svc, session, ctx, path="raw.dat", content_base64=base64.b64encode(payload).decode()
    )
    session.flush()
    assert rec["size_bytes"] == len(payload)
    out = file_tools.read_workdir_file(svc, session, ctx, file_ref=rec["file_ref"])
    assert out["content"] == "round-trip via base64"


def test_list_filtered_by_kind(env) -> None:
    """The kind filter restricts the listing to matching rows (line 60)."""
    svc, session, ctx = env
    file_tools.write_workdir_file(svc, session, ctx, path="w.txt", content="work")
    file_tools.create_artifact(svc, session, ctx, path="a.txt", content="art")
    session.flush()
    arts = file_tools.list_workdir_files(session, ctx, kind="artifact")
    assert len(arts["files"]) == 1
    assert arts["files"][0]["kind"] == "artifact"
    all_files = file_tools.list_workdir_files(session, ctx)
    assert len(all_files["files"]) == 2


def test_get_file_metadata_unknown_ref_raises(env) -> None:
    """A well-formed ref that resolves to nothing raises ValueError (line 178)."""
    svc, session, ctx = env
    with pytest.raises(ValueError, match="file not found"):
        file_tools.get_file_metadata(
            svc, session, ctx, file_ref="caliber://workflow-runs/WR-1/work/missing.txt"
        )


def test_get_file_metadata_other_run_raises(env, tmp_path: Path) -> None:
    """A ref bound to a different run is rejected for this ctx (line 178)."""
    svc, session, ctx = env
    other = svc.create_run_workspace(workflow_id="WF-9", workflow_run_id="WR-9")
    rec = file_tools.write_workdir_file(svc, session, other, path="x.txt", content="elsewhere")
    session.flush()
    with pytest.raises(ValueError, match="file not found"):
        file_tools.get_file_metadata(svc, session, ctx, file_ref=rec["file_ref"])


def test_bind_read_with_explicit_ref(env) -> None:
    """_read reads the named ref for a caliber:// arg (line 205); _list ignores its arg (200)."""
    svc, session, ctx = env
    rec = file_tools.write_workdir_file(svc, session, ctx, path="doc.txt", content="explicit")
    session.flush()
    tools = file_tools.bind_run_read_tools(svc, session, ctx)
    out = tools["read_workdir_file"](rec["file_ref"])
    assert out["content"] == "explicit"
    assert "input_files" not in out
    # The bound _list wrapper ignores its positional grounding arg (line 200).
    listed = tools["list_workdir_files"]("ignored grounding text")
    assert any(f["file_ref"] == rec["file_ref"] for f in listed["files"])


def test_bind_read_grounds_on_inputs_with_base64_fallback(env) -> None:
    """No-ref _read concatenates input files, base64-encoding non-UTF-8 ones (lines 227-228)."""
    svc, session, ctx = env
    svc.register_upload(
        session, ctx, kind="input", filename="bin.in", data=NON_UTF8, media_type=None, actor="@me"
    )
    session.flush()
    tools = file_tools.bind_run_read_tools(svc, session, ctx)
    out = tools["read_workdir_file"]("")  # no caliber:// ref -> ground on inputs
    assert out["count"] == 1
    assert base64.b64decode(out["input_files"][0]["content"]) == NON_UTF8


def test_bind_read_remaining_break_stops_after_cap(env) -> None:
    """The remaining<=0 guard stops reading once the cap is exhausted (lines 221-222)."""
    svc, session, ctx = env
    svc.register_upload(
        session,
        ctx,
        kind="input",
        filename="a.in",
        data=b"AAAA",
        media_type="text/plain",
        actor="@me",
    )
    svc.register_upload(
        session,
        ctx,
        kind="input",
        filename="b.in",
        data=b"BBBB",
        media_type="text/plain",
        actor="@me",
    )
    session.flush()
    # read_cap=4 -> first file consumes the whole budget; second file is skipped.
    tools = file_tools.bind_run_read_tools(svc, session, ctx, read_cap=4)
    out = tools["read_workdir_file"]("")
    assert out["count"] == 1
    assert out["input_files"][0]["content"] == "AAAA"


def test_bind_meta_with_and_without_ref(env) -> None:
    """_meta returns metadata for a ref, else falls back to a listing (lines 233-235)."""
    svc, session, ctx = env
    rec = file_tools.write_workdir_file(svc, session, ctx, path="m.txt", content="meta")
    session.flush()
    tools = file_tools.bind_run_read_tools(svc, session, ctx)

    with_ref = tools["get_file_metadata"](rec["file_ref"])
    assert with_ref["file_ref"] == rec["file_ref"]
    assert with_ref["size_bytes"] == 4

    without_ref = tools["get_file_metadata"]("just some text")
    assert "files" in without_ref
    assert any(f["file_ref"] == rec["file_ref"] for f in without_ref["files"])
