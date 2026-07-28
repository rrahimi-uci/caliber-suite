"""WorkingDirectoryService + ref grammar tests (storage doc §3.3, §8.1, §13)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import caliber.db.models  # noqa: F401 — register tables
from caliber.config import WorkflowStorageConfig
from caliber.db.base import Base
from caliber.db.models import CaliberWorkflowFileEvent
from caliber.storage import (
    LocalStorageBackend,
    StorageNotFoundError,
    StoragePermissionError,
    StorageValidationError,
    WorkingDirectoryService,
    build_ref,
    parse_ref,
)


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with sessionmaker(engine)() as s:
            yield s
    finally:
        engine.dispose()


def _service(tmp_path: Path, **overrides: object) -> WorkingDirectoryService:
    cfg = WorkflowStorageConfig(base_uri=f"file://{tmp_path}/ws", **overrides)  # type: ignore[arg-type]
    return WorkingDirectoryService(LocalStorageBackend(cfg.base_uri), cfg)


# ----- ref grammar --------------------------------------------------------- #
def test_ref_build_and_parse_artifact_segment() -> None:
    ref = build_ref("workflow-runs", "WR-1", "artifact", "out/report.pdf")
    assert ref == "caliber://workflow-runs/WR-1/artifacts/out/report.pdf"
    parsed = parse_ref(ref)
    assert parsed.resource_type == "workflow-runs"
    assert parsed.resource_id == "WR-1"
    assert parsed.segment == "artifacts"
    assert parsed.relative_path == "out/report.pdf"


def test_dataset_ref_roundtrip() -> None:
    ref = build_ref("datasets", "ED-1", "expected", "summary.csv", example_id="EX-9")
    assert ref == "caliber://datasets/ED-1/examples/EX-9/expected/summary.csv"
    parsed = parse_ref(ref)
    assert parsed.example_id == "EX-9"
    assert parsed.segment == "expected"


@pytest.mark.parametrize(
    "bad",
    [
        "caliber://workflow-runs/WR-1/bogus/x.txt",  # bad segment
        "caliber://datasets/ED-1/examples/EX-1/work/x",  # work not a dataset kind
        "caliber://workflow-runs/WR-1/input/../../escape",  # traversal
        "http://example.com/x",  # wrong scheme
    ],
)
def test_invalid_refs_rejected(bad: str) -> None:
    with pytest.raises(StorageValidationError):
        parse_ref(bad)


# ----- service ------------------------------------------------------------- #
def test_register_upload_creates_record(session: Session, tmp_path: Path) -> None:
    svc = _service(tmp_path)
    ctx = svc.create_run_workspace(workflow_id="WF-1", workflow_run_id="WR-1")
    rec = svc.register_upload(
        session,
        ctx,
        kind="input",
        filename="data.csv",
        data=b"a,b\n",
        media_type="text/csv",
        actor="@me",
    )
    assert rec.file_id.startswith("FILE-")
    assert rec.file_ref == "caliber://workflow-runs/WR-1/input/data.csv"
    assert rec.size_bytes == 4
    assert rec.status == "uploaded"
    # event recorded
    events = session.query(CaliberWorkflowFileEvent).all()
    assert any(e.action == "upload_file" for e in events)


def test_denied_extension_rejected(session: Session, tmp_path: Path) -> None:
    svc = _service(tmp_path)
    ctx = svc.create_run_workspace(workflow_id="WF-1", workflow_run_id="WR-1")
    with pytest.raises(StorageValidationError):
        svc.register_upload(
            session,
            ctx,
            kind="input",
            filename="evil.sh",
            data=b"#!/bin/sh",
            media_type=None,
            actor="@me",
        )


def test_size_cap_enforced(session: Session, tmp_path: Path) -> None:
    svc = _service(tmp_path, max_upload_bytes=4)
    ctx = svc.create_run_workspace(workflow_id="WF-1", workflow_run_id="WR-1")
    with pytest.raises(StorageValidationError):
        svc.register_upload(
            session,
            ctx,
            kind="input",
            filename="big.txt",
            data=b"toolong",
            media_type=None,
            actor="@me",
        )


def test_per_run_file_count_quota(session: Session, tmp_path: Path) -> None:
    svc = _service(tmp_path, max_files_per_run=1)
    ctx = svc.create_run_workspace(workflow_id="WF-1", workflow_run_id="WR-1")
    svc.register_upload(
        session,
        ctx,
        kind="input",
        filename="a.txt",
        data=b"a",
        media_type=None,
        actor="@me",
    )
    session.flush()
    with pytest.raises(StorageValidationError):
        svc.register_upload(
            session,
            ctx,
            kind="input",
            filename="b.txt",
            data=b"b",
            media_type=None,
            actor="@me",
        )


def test_per_run_file_count_quota_enforced_for_playground(session: Session, tmp_path: Path) -> None:
    """Regression (#10): playground files are recorded under playground_run_id,
    not workflow_run_id, so the quota query must scope to that column. It used
    to query workflow_run_id only → count 0 → the quota never tripped."""
    svc = _service(tmp_path, max_files_per_run=1)
    ctx = svc.create_playground_workspace(playground_run_id="PG-1")
    svc.register_upload(
        session, ctx, kind="input", filename="a.txt", data=b"a", media_type=None, actor="@me"
    )
    session.flush()
    with pytest.raises(StorageValidationError):
        svc.register_upload(
            session, ctx, kind="input", filename="b.txt", data=b"b", media_type=None, actor="@me"
        )


def test_allowed_media_types_enforced(session: Session, tmp_path: Path) -> None:
    svc = _service(tmp_path, allowed_media_types=["text/csv"])
    ctx = svc.create_run_workspace(workflow_id="WF-1", workflow_run_id="WR-1")
    with pytest.raises(StorageValidationError):
        svc.register_upload(
            session,
            ctx,
            kind="input",
            filename="x.json",
            data=b"{}",
            media_type="application/json",
            actor="@me",
        )


def test_write_artifact_status(session: Session, tmp_path: Path) -> None:
    svc = _service(tmp_path)
    ctx = svc.create_run_workspace(workflow_id="WF-1", workflow_run_id="WR-1")
    rec = svc.write_artifact(
        session,
        ctx,
        path="report.pdf",
        data=b"%PDF",
        media_type="application/pdf",
        actor="@runtime",
        node_id="reporter",
    )
    assert rec.status == "artifact"
    assert rec.kind == "artifact"
    assert rec.producer_node_id == "reporter"
    assert rec.file_ref == "caliber://workflow-runs/WR-1/artifacts/report.pdf"


def test_open_for_tool_cross_run_denied(session: Session, tmp_path: Path) -> None:
    svc = _service(tmp_path)
    ctx = svc.create_run_workspace(workflow_id="WF-1", workflow_run_id="WR-1")
    rec = svc.register_upload(
        session,
        ctx,
        kind="input",
        filename="x.txt",
        data=b"data",
        media_type=None,
        actor="@me",
    )
    session.flush()
    # Same run: allowed.
    fh = svc.open_for_tool(session, rec.file_ref, run_id="WR-1", actor="@me")
    assert fh.read() == b"data"
    fh.close()
    # Different run: denied + event recorded.
    with pytest.raises(StoragePermissionError):
        svc.open_for_tool(session, rec.file_ref, run_id="WR-OTHER", actor="@evil")
    denials = [
        e for e in session.query(CaliberWorkflowFileEvent).all() if e.action == "file_access_denied"
    ]
    assert len(denials) == 1


def test_materialize_input_files_binds_staged(session: Session, tmp_path: Path) -> None:
    svc = _service(tmp_path)
    staging = svc.create_run_workspace(workflow_id="_staging", workflow_run_id="staging")
    staged = svc.register_upload(
        session,
        staging,
        kind="input",
        filename="invoice.pdf",
        data=b"%PDF",
        media_type="application/pdf",
        actor="@me",
    )
    session.flush()
    run = svc.create_run_workspace(workflow_id="WF-1", workflow_run_id="WR-9")
    bound = svc.materialize_input_files(session, run, [{"file_id": staged.file_id}], actor="@me")
    assert len(bound) == 1
    assert bound[0].file_ref == "caliber://workflow-runs/WR-9/input/invoice.pdf"
    assert bound[0].workflow_run_id == "WR-9"


def test_materialize_missing_raises(session: Session, tmp_path: Path) -> None:
    svc = _service(tmp_path)
    run = svc.create_run_workspace(workflow_id="WF-1", workflow_run_id="WR-9")
    with pytest.raises(StorageNotFoundError):
        svc.materialize_input_files(session, run, [{"file_id": "FILE-nope"}], actor="@me")


@pytest.mark.parametrize(
    ("target_tenant", "target_project"),
    [("tenant-a", "PRJ-b"), ("tenant-b", "PRJ-a")],
)
def test_materialize_input_files_rejects_cross_project_or_tenant(
    session: Session,
    tmp_path: Path,
    target_tenant: str,
    target_project: str,
) -> None:
    svc = _service(tmp_path)
    staging = svc.create_run_workspace(
        tenant_id="tenant-a",
        project_id="PRJ-a",
        workflow_id="_staging",
        workflow_run_id="staging-session",
    )
    staged = svc.register_upload(
        session,
        staging,
        kind="input",
        filename="private.txt",
        data=b"private",
        media_type="text/plain",
        actor="@owner",
    )
    row = svc.get_row(session, staged.file_id)
    assert row is not None
    row.workflow_run_id = None
    session.flush()
    target = svc.create_run_workspace(
        tenant_id=target_tenant,
        project_id=target_project,
        workflow_id="WF-target",
        workflow_run_id="WR-target",
    )

    with pytest.raises(StoragePermissionError, match="tenant/project"):
        svc.materialize_input_files(
            session,
            target,
            [{"file_id": staged.file_id}],
            actor="@attacker",
        )


def test_project_file_physical_object_is_content_addressed_and_never_overwritten(
    session: Session,
    tmp_path: Path,
) -> None:
    svc = _service(tmp_path)
    first = svc.register_project_file(
        session,
        project_id="PRJ-race",
        kind="input",
        filename="source.txt",
        data=b"first pinned bytes",
        media_type="text/plain",
        actor="@owner",
    )
    first_row = svc.get_row(session, first.file_id)
    assert first_row is not None
    first_key = first_row.object_key
    # Simulate a stale concurrent DB lookup that did not observe the winner.
    session.delete(first_row)
    session.flush()

    second = svc.register_project_file(
        session,
        project_id="PRJ-race",
        kind="input",
        filename="source.txt",
        data=b"different concurrent bytes",
        media_type="text/plain",
        actor="@other",
    )
    second_row = svc.get_row(session, second.file_id)
    assert second_row is not None

    assert first_key != second_row.object_key
    assert svc._backend.read_bytes(first_key) == b"first pinned bytes"
    assert svc.read_bytes(second_row) == b"different concurrent bytes"
