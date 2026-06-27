"""Phase 1b: execute(extra_tools=...) merge + run-scoped file-tool binding/summary."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import caliber.db.models  # noqa: F401
from caliber.config import WorkflowStorageConfig
from caliber.db.base import Base
from caliber.storage import LocalStorageBackend, WorkingDirectoryService
from caliber.workflows.compiler import build_ir
from caliber.workflows.file_tools import bind_run_read_tools
from caliber.workflows.manifest import parse_manifest
from caliber.workflows.runtime import FakeWorkflowExecutor, RuntimePlan, execute
from tests.workflow_helpers import fake_resolver, make_support_manifest


def _plan(manifest_dict: dict, **kwargs: object) -> RuntimePlan:
    resolver = fake_resolver()
    ir = build_ir(parse_manifest(manifest_dict), resolver, version="7")
    return RuntimePlan(ir=ir, resolver=resolver, **kwargs)


def _tool_result(result, tool_name: str):
    for step in result.steps:
        for call in step.tool_calls:
            if call.get("tool") == tool_name:
                return call.get("result")
    return None


def test_extra_tools_none_is_noop() -> None:
    """Without extra_tools the resolved tool runs (baseline / regression guard)."""
    result = execute(_plan(make_support_manifest()), "refund?", executor=FakeWorkflowExecutor())
    assert result.status == "completed"
    assert _tool_result(result, "lookup_policy") == {"policy": "30-day refund"}


def test_extra_tools_override_resolved_tool() -> None:
    """extra_tools shadow the resolved tool of the same local_name (storage doc §4.4)."""
    sentinel = {"overridden": True}
    result = execute(
        _plan(make_support_manifest()),
        "refund?",
        executor=FakeWorkflowExecutor(),
        extra_tools={"lookup_policy": lambda _x="": sentinel},
    )
    assert result.status == "completed"
    assert _tool_result(result, "lookup_policy") == sentinel


# ----- run-scoped file-tool binding + summary ------------------------------ #
@pytest.fixture
def storage_env(tmp_path: Path) -> Iterator[tuple[WorkingDirectoryService, Session, object]]:
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


def test_bind_run_read_tools_grounds_on_inputs(storage_env) -> None:
    svc, session, ctx = storage_env
    svc.register_upload(
        session,
        ctx,
        kind="input",
        filename="notes.txt",
        data=b"ground truth",
        media_type="text/plain",
        actor="@me",
    )
    session.flush()
    tools = bind_run_read_tools(svc, session, ctx)
    # grounding signature: invoked positionally with the run input text
    listed = tools["list_workdir_files"]("anything")
    assert any(f["name"] == "notes.txt" for f in listed["files"])
    read = tools["read_workdir_file"]("refund?")  # no ref -> reads input files
    assert read["count"] == 1
    assert read["input_files"][0]["content"] == "ground truth"


def test_run_file_summary_counts_and_artifacts(storage_env) -> None:
    svc, session, ctx = storage_env
    svc.register_upload(
        session,
        ctx,
        kind="input",
        filename="in.csv",
        data=b"a,b",
        media_type="text/csv",
        actor="@me",
    )
    svc.write_artifact(
        session,
        ctx,
        path="out.pdf",
        data=b"%PDF",
        media_type="application/pdf",
        actor="@runtime",
        node_id="reporter",
    )
    session.flush()
    summary = svc.run_file_summary(session, "WR-1")
    assert summary["file_counts"].get("input") == 1
    assert summary["file_counts"].get("artifact") == 1
    assert len(summary["artifacts"]) == 1
    assert summary["artifacts"][0]["producer_node_id"] == "reporter"
