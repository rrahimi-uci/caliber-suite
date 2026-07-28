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
from caliber.storage import LocalStorageBackend, StoragePermissionError, WorkingDirectoryService
from caliber.workflows import file_tools
from caliber.workflows.ir import (
    IRManagedFileReference,
    IRTool,
    IRToolBinding,
    IRWorkflow,
    NodeType,
)


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
    svc, session, _ctx = env
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
    ctx = svc.create_run_workspace(project_id="PRJ-1", workflow_id="WF-1", workflow_run_id="WR-1")

    read = file_tools.read_workdir_file(svc, session, ctx, file_ref=rec.file_ref)
    assert read["content"] == "refund policy"
    meta = file_tools.get_file_metadata(svc, session, ctx, file_ref=rec.file_ref)
    assert meta["file_ref"] == rec.file_ref
    assert meta["storage_backend"] == "local"


def test_project_file_ref_is_not_readable_from_another_project(env) -> None:
    svc, session, _ctx = env
    session.add(CaliberProject(project_id="PRJ-private", name="Private", owner="@me"))
    record = svc.register_project_file(
        session,
        project_id="PRJ-private",
        kind="input",
        filename="secret.txt",
        data=b"secret",
        media_type="text/plain",
        actor="@me",
    )
    session.flush()
    other_ctx = svc.create_run_workspace(
        project_id="PRJ-other", workflow_id="WF-other", workflow_run_id="WR-other"
    )

    with pytest.raises(StoragePermissionError, match="tenant/project"):
        file_tools.read_workdir_file(
            svc,
            session,
            other_ctx,
            file_ref=record.file_ref,
        )


def test_write_requires_content(env) -> None:
    svc, session, ctx = env
    with pytest.raises(ValueError, match="content"):
        file_tools.write_workdir_file(svc, session, ctx, path="x.txt")


def _managed_snapshot(record) -> IRManagedFileReference:
    return IRManagedFileReference(
        file_id=record.file_id,
        file_ref=record.file_ref,
        sha256=record.sha256,
        name=record.name,
        size_bytes=record.size_bytes,
        media_type=record.media_type,
        object_version_id=record.object_version_id,
    )


def test_managed_file_resolver_verifies_and_extracts_without_host_path(env) -> None:
    svc, session, _ctx = env
    session.add(CaliberProject(project_id="PRJ-managed", name="Managed", owner="@me"))
    record = svc.register_project_file(
        session,
        project_id="PRJ-managed",
        kind="input",
        filename="source.md",
        data=b"# Verified source\n\nPinned bytes.",
        media_type="text/markdown",
        actor="@me",
    )
    ctx = svc.create_run_workspace(
        project_id="PRJ-managed", workflow_id="WF-1", workflow_run_id="WR-managed"
    )
    snapshot = _managed_snapshot(record)
    resolver, tools = file_tools.bind_managed_file_runtime(svc, session, ctx, [snapshot])
    assert resolver is not None
    resolver.verify_all()
    text, metadata = resolver.read_text(snapshot, "utf-8", 200_000)
    assert "Verified source" in text
    assert metadata["immutable"] is True
    extracted = tools["extract_document"](record.file_ref)
    assert extracted["text"] == "Verified source\n\nPinned bytes."
    assert extracted["source"] == "source.md"
    assert extracted["file_ref"] == record.file_ref
    assert "caliber-managed-document" not in str(extracted)


def test_managed_file_extractor_override_follows_registered_local_alias(env) -> None:
    svc, session, _ctx = env
    session.add(CaliberProject(project_id="PRJ-alias", name="Alias", owner="@me"))
    record = svc.register_project_file(
        session,
        project_id="PRJ-alias",
        kind="input",
        filename="aliased.md",
        data=b"# Aliased verified bytes",
        media_type="text/markdown",
        actor="@me",
    )
    binding = IRToolBinding(
        local_name="extractor",
        registry_ref="tool.extract_document.v1",
        version_constraint=">=1",
        requires_approval=False,
        side_effect_level="read",
        allow_in_preview=True,
        module_path="caliber.workflows.ingestion_tools",
        callable_name="extract_document",
    )
    tool_node = IRTool(node_id="extract", node_type=NodeType.TOOL, binding=binding)
    ir = IRWorkflow(
        workflow_id="WF-alias",
        version="1",
        nodes={tool_node.node_id: tool_node},
        edges=[],
        entry_node_id=tool_node.node_id,
        output_node_id=tool_node.node_id,
    )
    ctx = svc.create_run_workspace(
        project_id="PRJ-alias", workflow_id=ir.workflow_id, workflow_run_id="WR-alias"
    )
    resolver, tools = file_tools.bind_managed_file_runtime(
        svc,
        session,
        ctx,
        [_managed_snapshot(record)],
        extract_document_aliases=file_tools.managed_file_tool_aliases(ir),
    )

    assert resolver is not None
    assert set(tools) == {"extract_document", "extractor"}
    extracted = tools["extractor"](record.file_ref)
    assert extracted["text"] == "Aliased verified bytes"
    assert extracted["file_ref"] == record.file_ref


def test_managed_file_resolver_fails_closed_on_scope_and_byte_tampering(env) -> None:
    svc, session, _ctx = env
    session.add(CaliberProject(project_id="PRJ-source", name="Source", owner="@me"))
    record = svc.register_project_file(
        session,
        project_id="PRJ-source",
        kind="input",
        filename="source.txt",
        data=b"original",
        media_type="text/plain",
        actor="@me",
    )
    snapshot = _managed_snapshot(record)
    wrong_project_ctx = svc.create_run_workspace(
        project_id="PRJ-other", workflow_id="WF-1", workflow_run_id="WR-other"
    )
    resolver, _ = file_tools.bind_managed_file_runtime(svc, session, wrong_project_ctx, [snapshot])
    assert resolver is not None
    with pytest.raises(ValueError, match="outside this run's tenant/project"):
        resolver.verify_all()

    row = svc.get_row(session, record.file_id)
    assert row is not None
    svc._backend.write_bytes(
        row.object_key,
        b"tampered",
        media_type="text/plain",
        overwrite=True,
    )
    correct_ctx = svc.create_run_workspace(
        project_id="PRJ-source", workflow_id="WF-1", workflow_run_id="WR-source"
    )
    resolver, _ = file_tools.bind_managed_file_runtime(svc, session, correct_ctx, [snapshot])
    assert resolver is not None
    with pytest.raises(ValueError, match="sha256"):
        resolver.verify_all()
