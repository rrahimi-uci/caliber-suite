"""Runtime interpreter tests (plan §19.16)."""

from __future__ import annotations

import contextlib
import json
import sys
import textwrap
import threading
import time
import types
from copy import deepcopy

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import caliber.workflows.runtime as workflow_runtime
from caliber.db import Base
from caliber.db.models import CaliberWorkflow
from caliber.mcp_gateway import McpGatewayError
from caliber.workflows.compiler import build_ir
from caliber.workflows.ir import (
    IRAgent,
    IREdge,
    IRExecutionPolicy,
    IRExternalApp,
    IRGuardrail,
    IRGuardrailCheck,
    IRHumanApproval,
    IRNode,
    IRRouter,
    IRRouterBranch,
    IRTool,
    IRToolBinding,
    IRType,
    IRWorkflow,
    NodeType,
    PromptRef,
)
from caliber.workflows.manifest import parse_manifest
from caliber.workflows.runtime import (
    AgentTurnResult,
    CaliberRunContext,
    FakeWorkflowExecutor,
    RuntimePlan,
    RuntimeResumeCheckpoint,
    ToolExecutionError,
    _condition_matches,
    _execute_model_tool_call,
    _find_recent_tool_calls,
    _first_str,
    _invoke_external_app_callable,
    _path_from_inputs,
    _publish_declared_outputs,
    _read_folder_input_node,
    _read_text_file_node,
    _resilient_callable,
    _resolve_external_app_entrypoint,
    _route,
    _run_external_app_entrypoint,
    _run_node,
    _select_input,
    _set_mlflow_tags,
    current_run_context,
    execute,
    run_tags,
    run_with_caliber_context,
    workflow_model,
)
from caliber.workflows.session_memory import (
    InMemoryWorkflowSessionMemoryStore,
    SqlWorkflowSessionMemoryStore,
)
from caliber.workflows.template_catalog import build_workflow_template_catalog
from caliber.workflows.tools import InMemoryToolResolver
from tests.workflow_helpers import fake_resolver, make_manifest, make_support_manifest


def _plan(manifest_dict, **plan_kwargs) -> RuntimePlan:
    resolver = fake_resolver()
    ir = build_ir(parse_manifest(manifest_dict), resolver, version="7")
    # ``external_app`` entrypoints are allowlisted and fail closed by default (C8).
    # These tests exercise node behaviour, so permit the test modules by prefix; the
    # allowlist itself is covered by its own tests.
    plan_kwargs.setdefault("external_app_entrypoint_allowlist", "workflow_external_*")
    return RuntimePlan(ir=ir, resolver=resolver, **plan_kwargs)


def _starter_manifest(kind: str) -> dict[str, object]:
    catalog = build_workflow_template_catalog()
    for template in catalog["templates"]:
        if template["kind"] == kind:
            return deepcopy(template["manifest_template"])
    raise AssertionError(f"unknown workflow starter template {kind!r}")


class _NoSelectionHandoffExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.input_calls: list[str] = []
        self.history_calls: list[list[dict[str, str]]] = []

    def run_agent(
        self,
        agent: IRAgent,
        input_text: str,
        *,
        history: list[dict[str, str]] | None = None,
        tool_callables,
        preview,
    ) -> AgentTurnResult:
        del tool_callables, preview
        self.calls.append(agent.node_id)
        self.input_calls.append(input_text)
        self.history_calls.append([dict(item) for item in (history or [])])
        return AgentTurnResult(
            final_output=f"[{agent.node_id}] processed: {input_text}",
            tokens=len(input_text.split()) + 1,
        )


class _ExecutorManagedHandoffExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.handoff_graphs: list[list[str]] = []

    def run_agent(
        self,
        agent: IRAgent,
        input_text: str,
        *,
        history: list[dict[str, str]] | None = None,
        handoff_agents=None,
        tool_callables=None,
        preview=False,
    ) -> AgentTurnResult:
        del history, tool_callables, preview
        self.calls.append(agent.node_id)
        self.handoff_graphs.append(sorted((handoff_agents or {}).keys()))
        return AgentTurnResult(
            final_output=f"[{agent.node_id}] internally delegated: {input_text}",
            tokens=len(input_text.split()) + 2,
            handoffs_resolved_in_executor=True,
        )


def test_single_agent_runs() -> None:
    result = execute(_plan(make_manifest()), "hello", executor=FakeWorkflowExecutor())
    assert result.status == "completed"
    assert result.output


def test_start_to_output_passthrough_runs_without_agent() -> None:
    manifest = make_manifest("passthrough-runtime-wf")
    del manifest["nodes"]["agent"]
    manifest["edges"] = [
        {"id": "e_start_final", "from": "start", "to": "final", "map": {"msg": "response"}}
    ]

    result = execute(_plan(manifest), "hello", executor=FakeWorkflowExecutor())

    assert result.status == "completed"
    assert result.output == "hello"
    assert [step.node_id for step in result.steps] == ["start", "final"]
    assert result.steps[0].node_type == "start"
    assert result.steps[1].node_type == "output"


def test_execute_records_step_port_snapshots() -> None:
    result = execute(_plan(make_manifest()), "hello", executor=FakeWorkflowExecutor())

    assert result.status == "completed"
    start_step = next(step for step in result.steps if step.node_id == "start")
    agent_step = next(step for step in result.steps if step.node_id == "agent")
    final_step = next(step for step in result.steps if step.node_id == "final")

    assert start_step.input_by_port == {}
    assert start_step.output_by_port == {"msg": "hello"}
    assert agent_step.input_by_port == {"input": "hello"}
    assert agent_step.output_by_port == {"final_output": agent_step.output}
    assert final_step.input_by_port == {"response": agent_step.output}
    assert final_step.output_by_port == {}


def test_subworkflow_nodes_publish_child_workflow_diagnostics() -> None:
    manifest = make_manifest("subworkflow-runtime-wf")
    manifest["nodes"] = {
        "start": {
            "id": "start",
            "type": "start",
            "outputs": {"msg": {"type": "string"}},
        },
        "child_workflow": {
            "id": "child_workflow",
            "type": "subworkflow",
            "workflow_id": "WF-child",
            "alias": "prod",
            "inputs": {"input": {"type": "string"}},
            "outputs": {
                "output": {"type": "string"},
                "result": {"type": "structured"},
            },
        },
        "final": {
            "id": "final",
            "type": "output",
            "inputs": {"response": {"type": "string"}},
        },
    }
    manifest["edges"] = [
        {"id": "e1", "from": "start", "to": "child_workflow", "map": {"msg": "input"}},
        {
            "id": "e2",
            "from": "child_workflow",
            "to": "final",
            "map": {"output": "response"},
        },
    ]

    calls: list[tuple[str, str, str, float, int, bool]] = []

    def runner(
        workflow_id: str,
        alias: str,
        input_text: str,
        timeout_seconds: float,
        depth: int,
        executor,
        preview: bool,
    ) -> dict[str, object]:
        del executor
        calls.append((workflow_id, alias, input_text, timeout_seconds, depth, preview))
        return {
            "status": "completed",
            "output": "Escalated to the governed child workflow.",
            "error": None,
            "tokens": 17,
            "steps": ["child_start", "child_review", "child_final"],
        }

    result = execute(
        _plan(manifest, subworkflow_runner=runner),
        "Escalate the refund exception.",
        executor=FakeWorkflowExecutor(),
    )

    assert result.status == "completed"
    assert result.output == "Escalated to the governed child workflow."
    assert result.tokens == 17
    assert calls == [
        (
            "WF-child",
            "prod",
            "Escalate the refund exception.",
            120.0,
            1,
            False,
        )
    ]

    subflow_step = next(step for step in result.steps if step.node_id == "child_workflow")
    assert subflow_step.input_by_port == {"input": "Escalate the refund exception."}
    assert subflow_step.output_by_port is not None
    assert subflow_step.output_by_port["output"] == "Escalated to the governed child workflow."
    payload = subflow_step.output_by_port["result"]
    assert payload["workflow_id"] == "WF-child"
    assert payload["alias"] == "prod"
    assert payload["steps"] == ["child_start", "child_review", "child_final"]


def test_subworkflow_node_reports_missing_runner_at_workflow_runtime() -> None:
    manifest = make_manifest("subworkflow-runtime-missing-runner")
    manifest["nodes"] = {
        "start": {
            "id": "start",
            "type": "start",
            "outputs": {"msg": {"type": "string"}},
        },
        "child_workflow": {
            "id": "child_workflow",
            "type": "subworkflow",
            "workflow_id": "WF-child",
            "alias": "prod",
            "inputs": {"input": {"type": "string"}},
            "outputs": {
                "output": {"type": "string"},
                "result": {"type": "structured"},
            },
        },
        "final": {
            "id": "final",
            "type": "output",
            "inputs": {"response": {"type": "string"}},
        },
    }
    manifest["edges"] = [
        {"id": "e1", "from": "start", "to": "child_workflow", "map": {"msg": "input"}},
        {
            "id": "e2",
            "from": "child_workflow",
            "to": "final",
            "map": {"output": "response"},
        },
    ]

    result = execute(
        _plan(manifest),
        "Escalate the refund exception.",
        executor=FakeWorkflowExecutor(),
    )

    assert result.status == "error"
    assert result.error is not None
    assert (
        "ToolExecutionError: subworkflow runner is not configured for this runtime plan"
        in result.error
    )


def test_subworkflow_node_reports_child_failure_at_workflow_runtime() -> None:
    manifest = make_manifest("subworkflow-runtime-child-failure")
    manifest["nodes"] = {
        "start": {
            "id": "start",
            "type": "start",
            "outputs": {"msg": {"type": "string"}},
        },
        "child_workflow": {
            "id": "child_workflow",
            "type": "subworkflow",
            "workflow_id": "WF-child",
            "alias": "prod",
            "inputs": {"input": {"type": "string"}},
            "outputs": {
                "output": {"type": "string"},
                "result": {"type": "structured"},
            },
        },
        "final": {
            "id": "final",
            "type": "output",
            "inputs": {"response": {"type": "string"}},
        },
    }
    manifest["edges"] = [
        {"id": "e1", "from": "start", "to": "child_workflow", "map": {"msg": "input"}},
        {
            "id": "e2",
            "from": "child_workflow",
            "to": "final",
            "map": {"output": "response"},
        },
    ]

    def runner(
        workflow_id: str,
        alias: str,
        input_text: str,
        timeout_seconds: float,
        depth: int,
        executor,
        preview: bool,
    ) -> dict[str, object]:
        del input_text, timeout_seconds, depth, executor, preview
        return {
            "workflow_id": workflow_id,
            "alias": alias,
            "status": "error",
            "error": "child workflow checkpoint replay failed",
        }

    result = execute(
        _plan(manifest, subworkflow_runner=runner),
        "Escalate the refund exception.",
        executor=FakeWorkflowExecutor(),
    )

    assert result.status == "error"
    assert result.error is not None
    assert "ToolExecutionError: subworkflow 'WF-child'@'prod' failed" in result.error
    assert "child workflow checkpoint replay failed" in result.error


def test_build_ir_threads_workflow_session_mode() -> None:
    manifest = make_manifest(
        "session-mode-wf",
        runtime={
            "sdk": "openai-agents-python",
            "sdk_version_policy": "runtime-pinned",
            "compiler_version": "caliber-workflow-compiler-v1",
            "default_model_ref": "CALIBER_WORKFLOW_DEFAULT_MODEL",
            "session": {"type": "persistent"},
        },
    )
    ir = build_ir(parse_manifest(manifest), fake_resolver(), version="7")
    assert ir.session_mode == "persistent"


def test_agent_history_input_still_works_without_automatic_session_memory() -> None:
    data = make_manifest("manual-history-wf")
    data["nodes"]["agent"]["inputs"]["history"] = {"type": "structured"}
    data["nodes"]["agent"]["outputs"]["history"] = {"type": "structured"}
    plan = _plan(data)
    executor = FakeWorkflowExecutor()
    port_values: dict[tuple[str, str], object] = {}
    seeded_history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Hi there."},
    ]

    _, step = _run_node(
        plan.ir.nodes["agent"],
        plan.ir,
        plan,
        executor=executor,
        preview=False,
        inputs={"input": "follow up", "history": seeded_history},
        run_input="follow up",
        port_values=port_values,
        guardrail_results=[],
    )

    assert executor.history_calls[-1] == seeded_history
    assert step.output == "[test-agent] processed: follow up"
    assert port_values[("agent", "history")] == [
        *seeded_history,
        {"role": "user", "content": "follow up"},
        {"role": "assistant", "content": step.output},
    ]


def test_agent_structured_output_publishes_declared_port() -> None:
    data = make_manifest("structured-output-wf")
    data["nodes"]["agent"]["output_type"] = {
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "grounded": {"type": "boolean"},
        },
        "required": ["answer", "grounded"],
    }
    data["nodes"]["agent"]["outputs"] = {
        "final_output": {"type": "string"},
        "structured_output": {"type": "structured"},
    }

    result = execute(_plan(data), "hello", executor=FakeWorkflowExecutor())

    agent_step = next(step for step in result.steps if step.node_id == "agent")
    assert isinstance(agent_step.output_by_port, dict)
    assert isinstance(agent_step.output_by_port["structured_output"], dict)
    assert (
        agent_step.output_by_port["structured_output"]["answer"] == "[test-agent] processed: hello"
    )
    assert agent_step.output_by_port["structured_output"]["grounded"] is False


def test_workflow_session_memory_persists_in_memory_and_persistent_store() -> None:
    manifest = make_manifest(
        "session-memory-wf",
        runtime={
            "sdk": "openai-agents-python",
            "sdk_version_policy": "runtime-pinned",
            "compiler_version": "caliber-workflow-compiler-v1",
            "default_model_ref": "CALIBER_WORKFLOW_DEFAULT_MODEL",
            "session": {"type": "persistent"},
        },
    )
    resolver = fake_resolver()
    ir = build_ir(parse_manifest(manifest), resolver, version="7")

    engine = create_engine("sqlite:///:memory:", future=True)
    try:
        Base.metadata.create_all(engine)
        sql_factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
        with sql_factory() as session:
            session.add(
                CaliberWorkflow(
                    workflow_id="session-memory-wf",
                    name="Session Memory",
                    owner="@test",
                )
            )
            session.commit()

        executor = FakeWorkflowExecutor()
        persistent_plan = RuntimePlan(
            ir=ir,
            resolver=resolver,
            session_memory_store=SqlWorkflowSessionMemoryStore(session_factory=sql_factory),
        )
        first = execute(
            persistent_plan,
            "hello",
            executor=executor,
            session_id="SESSION-persistent",
        )
        second = execute(
            RuntimePlan(
                ir=ir,
                resolver=resolver,
                session_memory_store=SqlWorkflowSessionMemoryStore(session_factory=sql_factory),
            ),
            "again",
            executor=executor,
            session_id="SESSION-persistent",
        )

        assert first.status == "completed"
        assert second.status == "completed"
        assert executor.history_calls[0] == []
        assert executor.history_calls[1] == [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": first.output},
        ]

        memory_executor = FakeWorkflowExecutor()
        in_memory_first = execute(
            RuntimePlan(
                ir=ir,
                resolver=resolver,
                session_memory_store=InMemoryWorkflowSessionMemoryStore(),
            ),
            "hello",
            executor=memory_executor,
            session_id="SESSION-in-memory",
        )
        execute(
            RuntimePlan(
                ir=ir,
                resolver=resolver,
                session_memory_store=InMemoryWorkflowSessionMemoryStore(),
            ),
            "again",
            executor=memory_executor,
            session_id="SESSION-in-memory",
        )

        assert memory_executor.history_calls[0] == []
        assert memory_executor.history_calls[1] == [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": in_memory_first.output},
        ]
    finally:
        engine.dispose()


def test_file_input_node_reads_text(tmp_path) -> None:
    source = tmp_path / "input.txt"
    source.write_text("hello from a file", encoding="utf-8")
    data = make_manifest()
    data["nodes"]["file_input"] = {
        "id": "file_input",
        "type": "file_input",
        "inputs": {"path": {"type": "string"}},
        "outputs": {
            "text": {"type": "string"},
            "path": {"type": "string"},
            "metadata": {"type": "structured"},
        },
    }
    data["edges"] = [
        {"id": "e_start_file", "from": "start", "to": "file_input", "map": {"msg": "path"}},
        {"id": "e_file_agent", "from": "file_input", "to": "agent", "map": {"text": "input"}},
        {
            "id": "e_agent_final",
            "from": "agent",
            "to": "final",
            "map": {"final_output": "response"},
        },
    ]

    result = execute(_plan(data), str(source), executor=FakeWorkflowExecutor())

    assert result.status == "completed"
    file_step = next(s for s in result.steps if s.node_id == "file_input")
    assert file_step.output == "hello from a file"
    assert "hello from a file" in result.output


def test_managed_file_input_uses_scoped_resolver_and_publishes_lineage() -> None:
    managed_ref = {
        "file_id": "FILE-managed",
        "file_ref": "caliber://projects/PRJ-1/input/source.md",
        "sha256": "c" * 64,
        "name": "source.md",
        "size_bytes": 19,
        "media_type": "text/markdown",
        "object_version_id": "v3",
    }
    data = make_manifest()
    data["nodes"]["file_input"] = {
        "id": "file_input",
        "type": "file_input",
        "file_ref": managed_ref,
    }
    data["edges"] = [
        {
            "id": "e_start_file",
            "from": "start",
            "to": "file_input",
            "map": {"msg": "path"},
        },
        {"id": "e_file_agent", "from": "file_input", "to": "agent", "map": {"text": "input"}},
        {
            "id": "e_agent_final",
            "from": "agent",
            "to": "final",
            "map": {"final_output": "response"},
        },
    ]
    seen = []

    def resolve(snapshot, encoding, max_bytes):
        seen.append((snapshot, encoding, max_bytes))
        return "managed document", {
            "bytes": 16,
            "sha256": snapshot.sha256,
            "immutable": True,
        }

    result = execute(
        _plan(data),
        "ignored",
        executor=FakeWorkflowExecutor(),
        managed_file_resolver=resolve,
    )

    assert result.status == "completed"
    assert len(seen) == 1
    file_step = next(step for step in result.steps if step.node_id == "file_input")
    assert file_step.output_by_port["path"] == managed_ref["file_ref"]
    assert file_step.output_by_port["file_ref"] == managed_ref
    assert file_step.output_by_port["metadata"]["immutable"] is True


def test_managed_file_input_without_runtime_resolver_fails_clearly() -> None:
    data = make_manifest()
    data["nodes"]["file_input"] = {
        "id": "file_input",
        "type": "file_input",
        "file_ref": {
            "file_id": "FILE-managed",
            "file_ref": "caliber://projects/PRJ-1/input/source.md",
            "sha256": "c" * 64,
            "name": "source.md",
            "size_bytes": 19,
        },
    }
    data["edges"] = [
        {
            "id": "e_start_file",
            "from": "start",
            "to": "file_input",
            "map": {"msg": "path"},
        },
        {"id": "e_file_agent", "from": "file_input", "to": "agent", "map": {"text": "input"}},
        {
            "id": "e_agent_final",
            "from": "agent",
            "to": "final",
            "map": {"final_output": "response"},
        },
    ]
    result = execute(_plan(data), "ignored", executor=FakeWorkflowExecutor())
    assert result.status == "error"
    assert "scoped runtime file resolver" in (result.error or "")


def test_file_input_node_reports_missing_file_at_workflow_runtime(tmp_path) -> None:
    missing = tmp_path / "missing.txt"
    data = make_manifest()
    data["nodes"]["file_input"] = {
        "id": "file_input",
        "type": "file_input",
        "inputs": {"path": {"type": "string"}},
        "outputs": {
            "text": {"type": "string"},
            "path": {"type": "string"},
            "metadata": {"type": "structured"},
        },
    }
    data["edges"] = [
        {"id": "e_start_file", "from": "start", "to": "file_input", "map": {"msg": "path"}},
        {"id": "e_file_agent", "from": "file_input", "to": "agent", "map": {"text": "input"}},
        {
            "id": "e_agent_final",
            "from": "agent",
            "to": "final",
            "map": {"final_output": "response"},
        },
    ]

    result = execute(_plan(data), str(missing), executor=FakeWorkflowExecutor())

    assert result.status == "error"
    assert result.error is not None
    assert "FileNotFoundError: file input path does not exist" in result.error
    assert str(missing) in result.error


def test_folder_input_node_reads_matching_files(tmp_path) -> None:
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    (tmp_path / "b.log").write_text("beta", encoding="utf-8")
    data = make_manifest()
    data["nodes"]["folder_input"] = {
        "id": "folder_input",
        "type": "folder_input",
        "pattern": "*.txt",
        "recursive": False,
        "max_files": 5,
        "inputs": {"path": {"type": "string"}},
        "outputs": {
            "text": {"type": "string"},
            "files": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    data["edges"] = [
        {"id": "e_start_folder", "from": "start", "to": "folder_input", "map": {"msg": "path"}},
        {"id": "e_folder_agent", "from": "folder_input", "to": "agent", "map": {"text": "input"}},
        {
            "id": "e_agent_final",
            "from": "agent",
            "to": "final",
            "map": {"final_output": "response"},
        },
    ]

    result = execute(_plan(data), str(tmp_path), executor=FakeWorkflowExecutor())

    assert result.status == "completed"
    folder_step = next(s for s in result.steps if s.node_id == "folder_input")
    assert "--- a.txt ---" in folder_step.output
    assert "alpha" in result.output
    assert "beta" not in folder_step.output


def test_folder_input_node_reports_missing_folder_at_workflow_runtime(tmp_path) -> None:
    missing = tmp_path / "missing-folder"
    data = make_manifest()
    data["nodes"]["folder_input"] = {
        "id": "folder_input",
        "type": "folder_input",
        "pattern": "*.txt",
        "recursive": False,
        "max_files": 5,
        "inputs": {"path": {"type": "string"}},
        "outputs": {
            "text": {"type": "string"},
            "files": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    data["edges"] = [
        {"id": "e_start_folder", "from": "start", "to": "folder_input", "map": {"msg": "path"}},
        {"id": "e_folder_agent", "from": "folder_input", "to": "agent", "map": {"text": "input"}},
        {
            "id": "e_agent_final",
            "from": "agent",
            "to": "final",
            "map": {"final_output": "response"},
        },
    ]

    result = execute(_plan(data), str(missing), executor=FakeWorkflowExecutor())

    assert result.status == "error"
    assert result.error is not None
    assert "FileNotFoundError: folder input path does not exist" in result.error
    assert str(missing) in result.error


def test_file_input_helpers_cover_errors_and_fallback_outputs(tmp_path) -> None:
    file_path = tmp_path / "data.txt"
    file_path.write_text("abcdef", encoding="utf-8")
    folder = tmp_path / "folder"
    folder.mkdir()

    assert (
        _path_from_inputs({"path": ""}, configured_path=str(file_path), run_input="") == file_path
    )
    with pytest.raises(ValueError):
        _path_from_inputs({"path": ""}, configured_path="", run_input="")
    with pytest.raises(ValueError):
        _path_from_inputs({"path": ""}, configured_path="", run_input=str(file_path))
    with pytest.raises(FileNotFoundError):
        _read_text_file_node(tmp_path / "missing.txt", encoding="utf-8", max_bytes=10)
    with pytest.raises(IsADirectoryError):
        _read_text_file_node(folder, encoding="utf-8", max_bytes=10)

    text, metadata = _read_text_file_node(file_path, encoding="utf-8", max_bytes=3)
    assert text == "abc"
    assert metadata["truncated"] is True

    node = IRNode(
        node_id="file_input",
        node_type=NodeType.FILE_INPUT,
        outputs={"custom": IRType("string"), "details": IRType("structured")},
    )
    port_values: dict[tuple[str, str], object] = {}
    _publish_declared_outputs(node, port_values, {"metadata": {"ok": True}}, fallback="fallback")
    assert port_values[("file_input", "custom")] == "fallback"
    assert port_values[("file_input", "details")] == {"ok": True}


def test_folder_input_helper_error_and_limit_paths(tmp_path) -> None:
    file_path = tmp_path / "not_a_folder.txt"
    file_path.write_text("x", encoding="utf-8")
    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "a.txt").write_text("alpha", encoding="utf-8")
    (folder / "b.txt").write_text("beta", encoding="utf-8")

    with pytest.raises(FileNotFoundError):
        _read_folder_input_node(
            tmp_path / "missing",
            pattern="*.txt",
            recursive=False,
            max_files=1,
            max_bytes_per_file=10,
            encoding="utf-8",
        )
    with pytest.raises(NotADirectoryError):
        _read_folder_input_node(
            file_path,
            pattern="*.txt",
            recursive=False,
            max_files=1,
            max_bytes_per_file=10,
            encoding="utf-8",
        )

    text, files, metadata = _read_folder_input_node(
        folder,
        pattern="**/*.txt",
        recursive=False,
        max_files=1,
        max_bytes_per_file=10,
        encoding="utf-8",
    )
    assert "alpha" in text
    assert len(files) == 1
    assert metadata["truncated_file_list"] is True


def test_tool_call_binds_to_registry() -> None:
    result = execute(_plan(make_support_manifest()), "refund?", executor=FakeWorkflowExecutor())
    assert result.status == "completed"
    support_step = next(s for s in result.steps if s.node_id == "support_agent")
    called = {c["tool"] for c in support_step.tool_calls}
    assert "lookup_policy" in called


def test_mcp_tool_binding_executes_in_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    data = make_manifest()
    data["nodes"]["agent"]["tools"] = ["lookup_docs"]
    data["tools"] = {
        "lookup_docs": {
            "type": "mcp_tool",
            "server_id": "MCP-DOCS",
            "tool_name": "web_search",
            "side_effect_level": "read",
        }
    }

    def _fake_invoke(
        *,
        server_id: str,
        tool_name: str,
        arguments: dict[str, object],
        timeout_seconds: float = 45.0,
    ) -> dict[str, object]:
        return {
            "server_id": server_id,
            "tool_name": tool_name,
            "arguments": arguments,
            "result": {"ok": True},
        }

    monkeypatch.setattr("caliber.workflows.runtime.invoke_tool_by_server_id_sync", _fake_invoke)
    result = execute(_plan(data), "refund policy", executor=FakeWorkflowExecutor())
    assert result.status == "completed"
    support_step = next(s for s in result.steps if s.node_id == "agent")
    assert support_step.tool_calls
    assert support_step.tool_calls[0]["tool"] == "lookup_docs"
    assert support_step.tool_calls[0]["result"]["tool_name"] == "web_search"


def test_mcp_resource_node_executes_in_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    data = make_manifest()
    data["nodes"]["mcp_lookup"] = {
        "id": "mcp_lookup",
        "type": "mcp_resource",
        "server_id": "MCP-DOCS",
        "tool_name": "search_docs",
        "timeout_seconds": 30,
        "inputs": {"input": {"type": "string"}},
        "outputs": {
            "text": {"type": "string"},
            "result": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    data["edges"] = [
        {"id": "e_start_agent", "from": "start", "to": "agent", "map": {"msg": "input"}},
        {
            "id": "e_agent_mcp",
            "from": "agent",
            "to": "mcp_lookup",
            "map": {"final_output": "input"},
        },
        {"id": "e_mcp_final", "from": "mcp_lookup", "to": "final", "map": {"text": "response"}},
    ]

    captured: dict[str, object] = {}

    def _fake_invoke(
        *,
        server_id: str,
        tool_name: str,
        arguments: dict[str, object],
        timeout_seconds: float = 45.0,
    ) -> dict[str, object]:
        captured["server_id"] = server_id
        captured["tool_name"] = tool_name
        captured["arguments"] = arguments
        captured["timeout_seconds"] = timeout_seconds
        return {"text": "Refund policy found"}

    monkeypatch.setattr("caliber.workflows.runtime.invoke_tool_by_server_id_sync", _fake_invoke)
    result = execute(_plan(data), "refund policy", executor=FakeWorkflowExecutor())
    assert result.status == "completed"
    mcp_step = next(s for s in result.steps if s.node_id == "mcp_lookup")
    assert mcp_step.output == "Refund policy found"
    assert mcp_step.tool_calls[0]["tool_name"] == "search_docs"
    assert captured["server_id"] == "MCP-DOCS"
    assert captured["timeout_seconds"] == 30


def test_mcp_resource_node_reports_gateway_failure_at_workflow_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = make_manifest()
    data["nodes"]["mcp_lookup"] = {
        "id": "mcp_lookup",
        "type": "mcp_resource",
        "server_id": "MCP-DOCS",
        "tool_name": "search_docs",
        "timeout_seconds": 30,
        "inputs": {"input": {"type": "string"}},
        "outputs": {
            "text": {"type": "string"},
            "result": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    data["edges"] = [
        {"id": "e_start_agent", "from": "start", "to": "agent", "map": {"msg": "input"}},
        {
            "id": "e_agent_mcp",
            "from": "agent",
            "to": "mcp_lookup",
            "map": {"final_output": "input"},
        },
        {"id": "e_mcp_final", "from": "mcp_lookup", "to": "final", "map": {"text": "response"}},
    ]

    def _boom_invoke(
        *,
        server_id: str,
        tool_name: str,
        arguments: dict[str, object],
        timeout_seconds: float = 45.0,
    ) -> dict[str, object]:
        del arguments, timeout_seconds
        raise McpGatewayError(f"{server_id}/{tool_name} unavailable")

    monkeypatch.setattr("caliber.workflows.runtime.invoke_tool_by_server_id_sync", _boom_invoke)
    result = execute(_plan(data), "refund policy", executor=FakeWorkflowExecutor())

    assert result.status == "error"
    assert result.error is not None
    assert "ToolExecutionError: MCP node 'mcp_lookup' failed" in result.error
    assert "MCP-DOCS" in result.error
    assert "search_docs" in result.error


def test_tool_node_executes_registered_binding_in_runtime() -> None:
    data = make_manifest()
    data["nodes"]["tool_lookup"] = {
        "id": "tool_lookup",
        "type": "tool",
        "tool_name": "lookup_policy",
        "inputs": {
            "input": {"type": "string"},
            "arguments": {"type": "structured"},
        },
        "outputs": {
            "text": {"type": "string"},
            "result": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    data["tools"] = {
        "lookup_policy": {
            "registry_ref": "tool.lookup_policy.v1",
            "version_constraint": ">=1.0,<2.0",
        }
    }
    data["edges"] = [
        {"id": "e_start_tool", "from": "start", "to": "tool_lookup", "map": {"msg": "input"}},
        {"id": "e_tool_final", "from": "tool_lookup", "to": "final", "map": {"text": "response"}},
    ]

    result = execute(_plan(data), "refund policy", executor=FakeWorkflowExecutor())

    assert result.status == "completed"
    tool_step = next(s for s in result.steps if s.node_id == "tool_lookup")
    assert "30-day refund" in tool_step.output
    assert tool_step.tool_calls[0]["tool"] == "lookup_policy"
    assert tool_step.tool_calls[0]["registry_ref"] == "tool.lookup_policy.v1"


def test_tool_first_workflow_runs_without_agent() -> None:
    data = make_manifest()
    del data["nodes"]["agent"]
    data["nodes"]["tool_lookup"] = {
        "id": "tool_lookup",
        "type": "tool",
        "tool_name": "lookup_policy",
        "inputs": {
            "input": {"type": "string"},
            "arguments": {"type": "structured"},
        },
        "outputs": {
            "text": {"type": "string"},
            "result": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    data["tools"] = {
        "lookup_policy": {
            "registry_ref": "tool.lookup_policy.v1",
            "version_constraint": ">=1.0,<2.0",
        }
    }
    data["edges"] = [
        {"id": "e_start_tool", "from": "start", "to": "tool_lookup", "map": {"msg": "input"}},
        {"id": "e_tool_final", "from": "tool_lookup", "to": "final", "map": {"text": "response"}},
    ]

    result = execute(_plan(data), "refund policy", executor=FakeWorkflowExecutor())

    assert result.status == "completed"
    assert result.output == '{"policy": "30-day refund"}'
    assert [step.node_id for step in result.steps] == ["start", "tool_lookup", "final"]
    tool_step = next(step for step in result.steps if step.node_id == "tool_lookup")
    assert tool_step.node_type == "tool"
    assert tool_step.input_by_port == {"input": "refund policy"}
    assert tool_step.output_by_port is not None
    assert tool_step.output_by_port.get("text") == '{"policy": "30-day refund"}'
    assert tool_step.output_by_port.get("result") == {"policy": "30-day refund"}
    final_step = next(step for step in result.steps if step.node_id == "final")
    assert final_step.output == '{"policy": "30-day refund"}'


def test_tool_node_reports_bound_callable_failure_at_workflow_runtime() -> None:
    data = make_manifest()
    data["nodes"]["tool_lookup"] = {
        "id": "tool_lookup",
        "type": "tool",
        "tool_name": "failing_tool",
        "inputs": {
            "input": {"type": "string"},
            "arguments": {"type": "structured"},
        },
        "outputs": {
            "text": {"type": "string"},
            "result": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    data["tools"] = {
        "failing_tool": {
            "registry_ref": "tool.failing_tool.v1",
            "version_constraint": ">=1.0,<2.0",
        }
    }
    data["edges"] = [
        {"id": "e_start_tool", "from": "start", "to": "tool_lookup", "map": {"msg": "input"}},
        {"id": "e_tool_final", "from": "tool_lookup", "to": "final", "map": {"text": "response"}},
    ]

    def failing_tool(_input: str = "") -> dict[str, str]:
        raise RuntimeError(f"boom: {_input}")

    resolver = InMemoryToolResolver.from_callables({"tool.failing_tool.v1": failing_tool})
    plan = RuntimePlan(ir=build_ir(parse_manifest(data), resolver, version="7"), resolver=resolver)
    result = execute(plan, "refund policy", executor=FakeWorkflowExecutor())

    assert result.status == "error"
    assert result.error is not None
    assert "ToolExecutionError: tool 'failing_tool' failed after 1 attempt(s)" in result.error
    assert "boom: refund policy" in result.error


def test_tool_node_waits_for_runtime_approval_before_executing() -> None:
    string = IRType("string")
    called = False

    def forbidden_tool(_input: str = "") -> dict[str, str]:
        nonlocal called
        called = True
        raise AssertionError("approval-required tool node should not execute before approval")

    binding = IRToolBinding(
        local_name="delete_record",
        registry_ref="tool.delete_record.v1",
        version_constraint=">=1",
        requires_approval=True,
        side_effect_level="write",
        allow_in_preview=True,
        module_path="<in-memory>",
        callable_name="delete_record",
    )
    plan = RuntimePlan(
        ir=IRWorkflow(
            workflow_id="wf-tool-approval-blocked",
            version="1",
            nodes={
                "start": IRNode("start", NodeType.START, outputs={"msg": string}),
                "tool_lookup": IRTool(
                    node_id="tool_lookup",
                    node_type=NodeType.TOOL,
                    binding=binding,
                    inputs={"input": string},
                    outputs={
                        "text": string,
                        "result": IRType("structured"),
                        "metadata": IRType("structured"),
                    },
                ),
                "final": IRNode("final", NodeType.OUTPUT, inputs={"response": string}),
            },
            edges=[
                IREdge("e1", "start", "msg", "tool_lookup", "input", string),
                IREdge("e2", "tool_lookup", "text", "final", "response", string),
            ],
            entry_node_id="start",
            output_node_id="final",
        ),
        resolver=InMemoryToolResolver.from_callables({"tool.delete_record.v1": forbidden_tool}),
    )

    result = execute(
        plan,
        "delete ticket T-100",
        executor=FakeWorkflowExecutor(),
        runtime_approvals_enabled=True,
    )

    assert called is False
    assert result.status == "blocked"
    assert result.error == "waiting_approval:tool_lookup"
    tool_step = next(step for step in result.steps if step.node_id == "tool_lookup")
    assert tool_step.status == "blocked"
    assert tool_step.output == "delete ticket T-100"
    assert tool_step.input_by_port == {"input": "delete ticket T-100"}
    assert tool_step.output_by_port == {}


def test_tool_node_executes_after_runtime_approval_is_granted() -> None:
    string = IRType("string")
    calls: list[str] = []

    def approved_tool(_input: str = "") -> dict[str, str]:
        calls.append(_input)
        return {"message": f"deleted {_input}"}

    binding = IRToolBinding(
        local_name="delete_record",
        registry_ref="tool.delete_record.v1",
        version_constraint=">=1",
        requires_approval=True,
        side_effect_level="write",
        allow_in_preview=True,
        module_path="<in-memory>",
        callable_name="delete_record",
    )
    plan = RuntimePlan(
        ir=IRWorkflow(
            workflow_id="wf-tool-approval-approved",
            version="1",
            nodes={
                "start": IRNode("start", NodeType.START, outputs={"msg": string}),
                "tool_lookup": IRTool(
                    node_id="tool_lookup",
                    node_type=NodeType.TOOL,
                    binding=binding,
                    inputs={"input": string},
                    outputs={
                        "text": string,
                        "result": IRType("structured"),
                        "metadata": IRType("structured"),
                    },
                ),
                "final": IRNode("final", NodeType.OUTPUT, inputs={"response": string}),
            },
            edges=[
                IREdge("e1", "start", "msg", "tool_lookup", "input", string),
                IREdge("e2", "tool_lookup", "text", "final", "response", string),
            ],
            entry_node_id="start",
            output_node_id="final",
        ),
        resolver=InMemoryToolResolver.from_callables({"tool.delete_record.v1": approved_tool}),
    )

    result = execute(
        plan,
        "delete ticket T-200",
        executor=FakeWorkflowExecutor(),
        runtime_approvals_enabled=True,
        approved_human_approval_nodes={"tool_lookup"},
    )

    assert calls == ["delete ticket T-200"]
    assert result.status == "completed"
    assert result.output == "deleted delete ticket T-200"
    tool_step = next(step for step in result.steps if step.node_id == "tool_lookup")
    assert tool_step.status == "ok"
    assert tool_step.tool_calls[0]["tool"] == "delete_record"
    assert tool_step.output_by_port == {
        "metadata": {
            "arguments": {},
            "binding_type": "registered_function",
            "callable_name": "delete_record",
            "module_path": "<in-memory>",
            "registry_ref": "tool.delete_record.v1",
            "requires_approval": True,
            "side_effect_level": "write",
            "tool_name": "delete_record",
        },
        "result": {"message": "deleted delete ticket T-200"},
        "text": "deleted delete ticket T-200",
    }


def test_knowledge_query_node_executes_with_age_graph_payload() -> None:
    data = make_manifest()
    data["nodes"]["knowledge"] = {
        "id": "knowledge",
        "type": "knowledge_query",
        "knowledge_base_id": "KB-1",
        "version_ids": ["KBV-dyn", "KBV-dyn", "KBV-alt"],
        "retrieval_modes": ["dense", "age_graph"],
        "top_k": 4,
        "chat_model": "gpt-4.1-mini",
        "graph_overrides": {
            "age_seed_mode": "query_text_only",
            "age_traversal_hops": 1,
            "strict_age_retrieval": True,
            "age_candidate_pool_size": 40,
        },
    }
    data["edges"] = [
        {"id": "e_start_knowledge", "from": "start", "to": "knowledge", "map": {"msg": "question"}},
        {
            "id": "e_knowledge_final",
            "from": "knowledge",
            "to": "final",
            "map": {"answer": "response"},
        },
    ]

    captured: dict[str, object] = {}

    def _fake_knowledge_query(payload: dict[str, object]) -> dict[str, object]:
        captured.update(payload)
        return {
            "question": str(payload.get("question") or ""),
            "versions": [
                {
                    "knowledge_base_version_id": "KBV-dyn",
                    "retrieval_mode": "age_graph",
                    "answer": "AGE-backed answer",
                    "citations": [{"chunk_id": "CH-1", "label": "Policy excerpt"}],
                    "retrieved_chunks": [
                        {
                            "chunk_id": "CH-1",
                            "content": "Refunds are allowed for 30 days.",
                            "source_key": "docs/policy.md",
                        }
                    ],
                    "graph_context": {
                        "age_graph_name": "knowledge_graph",
                        "age_seed_strategy": "query_text",
                    },
                }
            ],
        }

    result = execute(
        _plan(data, knowledge_query_runner=_fake_knowledge_query),
        "refund policy",
        executor=FakeWorkflowExecutor(),
    )

    assert result.status == "completed"
    assert result.output == "AGE-backed answer"
    knowledge_step = next(s for s in result.steps if s.node_id == "knowledge")
    assert knowledge_step.output == "AGE-backed answer"
    assert "via age_graph" in knowledge_step.detail
    assert "1 citation" in knowledge_step.detail
    assert "1 chunk" in knowledge_step.detail
    assert "seeded from question text" in knowledge_step.detail
    assert captured["knowledge_base_id"] == "KB-1"
    assert captured["question"] == "refund policy"
    assert captured["version_ids"] == ["KBV-dyn", "KBV-alt"]
    assert captured["history"] == []
    assert captured["top_k"] == 4
    assert captured["chat_model"] == "gpt-4.1-mini"
    assert captured["retrieval_modes"] == ["dense", "age_graph"]
    assert captured["graph_overrides"] == {
        "age_seed_mode": "query_text_only",
        "age_traversal_hops": 1,
        "strict_age_retrieval": True,
        "age_candidate_pool_size": 40,
    }


def test_knowledge_query_node_preserves_empty_retrieval_modes_for_kb_default() -> None:
    data = make_manifest()
    data["nodes"]["knowledge"] = {
        "id": "knowledge",
        "type": "knowledge_query",
        "knowledge_base_id": "KB-1",
        "version_ids": [],
        "retrieval_modes": [],
        "top_k": 4,
    }
    data["edges"] = [
        {"id": "e_start_knowledge", "from": "start", "to": "knowledge", "map": {"msg": "question"}},
        {
            "id": "e_knowledge_final",
            "from": "knowledge",
            "to": "final",
            "map": {"answer": "response"},
        },
    ]

    captured: dict[str, object] = {}

    def _fake_knowledge_query(payload: dict[str, object]) -> dict[str, object]:
        captured.update(payload)
        return {
            "question": str(payload.get("question") or ""),
            "versions": [
                {
                    "knowledge_base_version_id": "KBV-dyn",
                    "retrieval_mode": "graph_hybrid",
                    "answer": "KB default answer",
                    "citations": [],
                    "retrieved_chunks": [],
                    "graph_context": {},
                }
            ],
        }

    result = execute(
        _plan(data, knowledge_query_runner=_fake_knowledge_query),
        "refund policy",
        executor=FakeWorkflowExecutor(),
    )

    assert result.status == "completed"
    assert result.output == "KB default answer"
    assert captured["retrieval_modes"] == []


def test_knowledge_query_node_accepts_runtime_retrieval_mode_override() -> None:
    data = make_manifest()
    data["nodes"]["selector"] = {
        "id": "selector",
        "type": "python_code",
        "code": 'return ["age_graph"]',
        "outputs": {"result": {"type": "structured"}},
    }
    data["nodes"]["knowledge"] = {
        "id": "knowledge",
        "type": "knowledge_query",
        "knowledge_base_id": "KB-1",
        "version_ids": [],
        "retrieval_modes": ["dense"],
        "top_k": 4,
        "inputs": {
            "question": {"type": "string"},
            "retrieval_modes": {"type": "structured"},
        },
        "outputs": {
            "text": {"type": "string"},
            "answer": {"type": "string"},
            "result": {"type": "structured"},
            "citations": {"type": "structured"},
            "chunks": {"type": "structured"},
            "graph_context": {"type": "structured"},
        },
    }
    data["edges"] = [
        {"id": "e_start_selector", "from": "start", "to": "selector", "map": {"msg": "input"}},
        {"id": "e_start_knowledge", "from": "start", "to": "knowledge", "map": {"msg": "question"}},
        {
            "id": "e_selector_knowledge",
            "from": "selector",
            "to": "knowledge",
            "map": {"result": "retrieval_modes"},
        },
        {
            "id": "e_knowledge_final",
            "from": "knowledge",
            "to": "final",
            "map": {"answer": "response"},
        },
    ]

    captured: dict[str, object] = {}

    def _fake_knowledge_query(payload: dict[str, object]) -> dict[str, object]:
        captured.update(payload)
        return {
            "question": str(payload.get("question") or ""),
            "versions": [
                {
                    "knowledge_base_version_id": "KBV-age",
                    "retrieval_mode": "age_graph",
                    "answer": "Runtime AGE answer",
                    "citations": [],
                    "retrieved_chunks": [],
                    "graph_context": {},
                }
            ],
        }

    result = execute(
        _plan(data, knowledge_query_runner=_fake_knowledge_query),
        "refund policy",
        executor=FakeWorkflowExecutor(),
    )

    assert result.status == "completed"
    assert result.output == "Runtime AGE answer"
    assert captured["retrieval_modes"] == ["age_graph"]


def test_graph_hybrid_rag_template_executes_with_query_runner() -> None:
    data = _starter_manifest("graph_hybrid_rag")
    nodes = data["nodes"]
    assert isinstance(nodes, dict)
    knowledge = nodes["knowledge"]
    assert isinstance(knowledge, dict)
    knowledge["knowledge_base_id"] = "KB-BAKEOFF"

    captured: dict[str, object] = {}

    def _fake_knowledge_query(payload: dict[str, object]) -> dict[str, object]:
        captured.update(payload)
        return {
            "question": str(payload.get("question") or ""),
            "versions": [
                {
                    "knowledge_base_version_id": "KBV-HYBRID-1",
                    "retrieval_mode": "graph_hybrid",
                    "answer": "Graph hybrid answer",
                    "citations": [{"chunk_id": "CH-H1", "label": "Hybrid evidence"}],
                    "retrieved_chunks": [
                        {
                            "chunk_id": "CH-H1",
                            "content": "Hybrid retrieval joined graph neighbors with chunk similarity.",
                            "source_key": "docs/hybrid.md",
                        }
                    ],
                    "graph_context": {
                        "retrieval_mode": "graph_hybrid",
                        "matched_entities": ["policy", "refund"],
                    },
                }
            ],
        }

    result = execute(
        _plan(data, knowledge_query_runner=_fake_knowledge_query),
        "refund policy",
        executor=FakeWorkflowExecutor(),
    )

    assert result.status == "completed"
    assert result.output == "Graph hybrid answer"
    knowledge_step = next(step for step in result.steps if step.node_id == "knowledge")
    assert knowledge_step.output == "Graph hybrid answer"
    assert "via graph_hybrid" in knowledge_step.detail
    assert "1 citation" in knowledge_step.detail
    assert captured["knowledge_base_id"] == "KB-BAKEOFF"
    assert captured["retrieval_modes"] == ["graph_hybrid"]
    assert captured["top_k"] == 6


def test_knowledge_age_build_template_executes_with_age_graph_defaults() -> None:
    data = _starter_manifest("knowledge_age_build")
    nodes = data["nodes"]
    assert isinstance(nodes, dict)
    build_graph = nodes["build_graph"]
    assert isinstance(build_graph, dict)
    build_graph["knowledge_base_id"] = "KB-AGE"

    captured: dict[str, object] = {}

    def _fake_knowledge_build(payload: dict[str, object]) -> dict[str, object]:
        captured.update(payload)
        return {
            "summary": "AGE knowledge build queued for KB-AGE with recursive chunks.",
            "status": "queued",
            "knowledge_base": {"knowledge_base_id": "KB-AGE"},
            "version": {
                "knowledge_base_version_id": "KBV-AGE-1",
                "version_number": 1,
                "status": "queued",
                "chunking_strategy": "recursive",
                "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
            },
            "run": {"knowledge_base_run_id": "KBR-AGE-1", "status": "queued"},
            "await_completion": {"requested": True, "status": "pending"},
            "activation": {"requested": True, "status": "scheduled"},
        }

    result = execute(
        _plan(data, knowledge_build_runner=_fake_knowledge_build),
        "refresh the graph profile",
        executor=FakeWorkflowExecutor(),
    )

    assert result.status == "completed"
    assert result.output == "AGE knowledge build queued for KB-AGE with recursive chunks."
    build_step = next(step for step in result.steps if step.node_id == "build_graph")
    assert build_step.output == result.output
    assert "queued" in build_step.detail
    assert captured["knowledge_base_id"] == "KB-AGE"
    assert captured["chunking_strategy"] == "recursive"
    assert captured["embedding_model"] == "sentence-transformers/all-MiniLM-L6-v2"
    assert captured["wait_for_completion"] is True
    assert captured["activate_when_complete"] is True
    assert captured["graph_config"] == {
        "extractor_backend": "heuristic",
        "max_entities_per_chunk": 12,
        "entity_types": [],
        "minimum_entity_mentions": 1,
        "minimum_relationship_weight": 1.0,
        "default_retrieval_mode": "age_graph",
        "retrieval_strength": "balanced",
        "output_target": "object_store_and_age",
        "age_seed_mode": "entity_then_text",
        "age_traversal_hops": 1,
        "age_candidate_pool_size": 24,
        "age_dense_rerank_weight": 0.35,
        "strict_age_retrieval_default": False,
    }


def test_knowledge_query_node_reports_missing_runner_at_workflow_runtime() -> None:
    data = make_manifest()
    data["nodes"]["knowledge"] = {
        "id": "knowledge",
        "type": "knowledge_query",
        "knowledge_base_id": "KB-1",
        "version_ids": [],
        "retrieval_modes": ["dense"],
        "top_k": 4,
    }
    data["edges"] = [
        {"id": "e_start_knowledge", "from": "start", "to": "knowledge", "map": {"msg": "question"}},
        {
            "id": "e_knowledge_final",
            "from": "knowledge",
            "to": "final",
            "map": {"answer": "response"},
        },
    ]

    result = execute(_plan(data), "refund policy", executor=FakeWorkflowExecutor())

    assert result.status == "error"
    assert result.error is not None
    assert "ToolExecutionError: knowledge query runner is not configured" in result.error


def test_knowledge_query_node_reports_query_failure_at_workflow_runtime() -> None:
    data = make_manifest()
    data["nodes"]["knowledge"] = {
        "id": "knowledge",
        "type": "knowledge_query",
        "knowledge_base_id": "KB-1",
        "version_ids": [],
        "retrieval_modes": ["dense"],
        "top_k": 4,
    }
    data["edges"] = [
        {"id": "e_start_knowledge", "from": "start", "to": "knowledge", "map": {"msg": "question"}},
        {
            "id": "e_knowledge_final",
            "from": "knowledge",
            "to": "final",
            "map": {"answer": "response"},
        },
    ]

    def _boom_knowledge_query(payload: dict[str, object]) -> dict[str, object]:
        del payload
        raise RuntimeError("knowledge query service unavailable")

    result = execute(
        _plan(data, knowledge_query_runner=_boom_knowledge_query),
        "refund policy",
        executor=FakeWorkflowExecutor(),
    )

    assert result.status == "error"
    assert result.error is not None
    assert "RuntimeError: knowledge query service unavailable" in result.error


def test_knowledge_build_node_launches_and_publishes_result() -> None:
    data = make_manifest()
    data["nodes"]["knowledge_build"] = {
        "id": "knowledge_build",
        "type": "knowledge_build",
        "knowledge_base_id": "KB-1",
        "chunking_strategy": "recursive",
        "embedding_model": "BAAI/bge-m3",
        "wait_for_completion": False,
        "activate_when_complete": False,
    }
    data["edges"] = [
        {"id": "e_start_build", "from": "start", "to": "knowledge_build", "map": {"msg": "input"}},
        {
            "id": "e_build_final",
            "from": "knowledge_build",
            "to": "final",
            "map": {"text": "response"},
        },
    ]

    captured: dict[str, object] = {}

    def _fake_knowledge_build(payload: dict[str, object]) -> dict[str, object]:
        captured.update(payload)
        return {
            "summary": "Knowledge build queued for v2 using recursive / BAAI/bge-m3.",
            "status": "queued",
            "knowledge_base": {"knowledge_base_id": "KB-1"},
            "version": {
                "knowledge_base_version_id": "KBV-2",
                "version_number": 2,
                "status": "queued",
                "chunking_strategy": "recursive",
                "embedding_model": "BAAI/bge-m3",
            },
            "run": {"knowledge_base_run_id": "KBR-2", "status": "queued"},
            "await_completion": {"requested": False, "status": "not_requested"},
            "activation": {"requested": False, "status": "skipped"},
        }

    result = execute(
        _plan(data, knowledge_build_runner=_fake_knowledge_build),
        "refresh",
        executor=FakeWorkflowExecutor(),
    )

    assert result.status == "completed"
    assert result.output == "Knowledge build queued for v2 using recursive / BAAI/bge-m3."
    step = next(s for s in result.steps if s.node_id == "knowledge_build")
    assert step.output == result.output
    assert "v2" in step.detail
    assert "queued" in step.detail
    assert captured["knowledge_base_id"] == "KB-1"
    assert captured["chunking_strategy"] == "recursive"
    assert captured["embedding_model"] == "BAAI/bge-m3"
    assert captured["wait_for_completion"] is False
    assert captured["activate_when_complete"] is False


def test_knowledge_build_node_reports_missing_runner_at_workflow_runtime() -> None:
    data = make_manifest()
    data["nodes"]["knowledge_build"] = {
        "id": "knowledge_build",
        "type": "knowledge_build",
        "knowledge_base_id": "KB-1",
        "chunking_strategy": "recursive",
        "embedding_model": "BAAI/bge-m3",
    }
    data["edges"] = [
        {"id": "e_start_build", "from": "start", "to": "knowledge_build", "map": {"msg": "input"}},
        {
            "id": "e_build_final",
            "from": "knowledge_build",
            "to": "final",
            "map": {"text": "response"},
        },
    ]

    result = execute(_plan(data), "refresh", executor=FakeWorkflowExecutor())

    assert result.status == "error"
    assert result.error is not None
    assert "ToolExecutionError: knowledge build runner is not configured" in result.error


def test_knowledge_build_node_reports_runner_failure_at_workflow_runtime() -> None:
    data = make_manifest()
    data["nodes"]["knowledge_build"] = {
        "id": "knowledge_build",
        "type": "knowledge_build",
        "knowledge_base_id": "KB-1",
        "chunking_strategy": "recursive",
        "embedding_model": "BAAI/bge-m3",
    }
    data["edges"] = [
        {"id": "e_start_build", "from": "start", "to": "knowledge_build", "map": {"msg": "input"}},
        {
            "id": "e_build_final",
            "from": "knowledge_build",
            "to": "final",
            "map": {"text": "response"},
        },
    ]

    def _boom_knowledge_build(payload: dict[str, object]) -> dict[str, object]:
        del payload
        raise RuntimeError("knowledge build service unavailable")

    result = execute(
        _plan(data, knowledge_build_runner=_boom_knowledge_build),
        "refresh",
        executor=FakeWorkflowExecutor(),
    )

    assert result.status == "error"
    assert result.error is not None
    assert "RuntimeError: knowledge build service unavailable" in result.error


def test_knowledge_build_node_accepts_runtime_field_overrides() -> None:
    data = make_manifest()
    data["nodes"]["selector"] = {
        "id": "selector",
        "type": "python_code",
        "code": 'return {"chunking": "semantic", "embedding": "intfloat/e5-large-v2"}',
        "outputs": {
            "chunking": {"type": "string"},
            "embedding": {"type": "string"},
        },
    }
    data["nodes"]["knowledge_build"] = {
        "id": "knowledge_build",
        "type": "knowledge_build",
        "knowledge_base_id": "KB-1",
        "chunking_strategy": "recursive",
        "embedding_model": "BAAI/bge-m3",
        "inputs": {
            "input": {"type": "string"},
            "chunking_strategy": {"type": "string"},
            "embedding_model": {"type": "string"},
        },
    }
    data["edges"] = [
        {"id": "e_start_selector", "from": "start", "to": "selector", "map": {"msg": "input"}},
        {"id": "e_start_build", "from": "start", "to": "knowledge_build", "map": {"msg": "input"}},
        {
            "id": "e_selector_build_chunking",
            "from": "selector",
            "to": "knowledge_build",
            "map": {"chunking": "chunking_strategy"},
        },
        {
            "id": "e_selector_build_embedding",
            "from": "selector",
            "to": "knowledge_build",
            "map": {"embedding": "embedding_model"},
        },
        {
            "id": "e_build_final",
            "from": "knowledge_build",
            "to": "final",
            "map": {"text": "response"},
        },
    ]

    captured: dict[str, object] = {}

    def _fake_knowledge_build(payload: dict[str, object]) -> dict[str, object]:
        captured.update(payload)
        return {
            "summary": "Knowledge build queued for v3 using semantic / intfloat/e5-large-v2.",
            "status": "queued",
            "knowledge_base": {"knowledge_base_id": "KB-1"},
            "version": {
                "knowledge_base_version_id": "KBV-3",
                "version_number": 3,
                "status": "queued",
                "chunking_strategy": "semantic",
                "embedding_model": "intfloat/e5-large-v2",
            },
            "run": {"knowledge_base_run_id": "KBR-3", "status": "queued"},
            "await_completion": {"requested": False, "status": "not_requested"},
            "activation": {"requested": False, "status": "skipped"},
        }

    result = execute(
        _plan(data, knowledge_build_runner=_fake_knowledge_build),
        "refresh",
        executor=FakeWorkflowExecutor(),
    )

    assert result.status == "completed", result.error
    assert captured["chunking_strategy"] == "semantic"
    assert captured["embedding_model"] == "intfloat/e5-large-v2"


def test_knowledge_build_node_skips_launch_in_preview_mode() -> None:
    data = make_manifest()
    data["nodes"]["knowledge_build"] = {
        "id": "knowledge_build",
        "type": "knowledge_build",
        "knowledge_base_id": "KB-1",
        "chunking_strategy": "recursive",
        "embedding_model": "BAAI/bge-m3",
    }
    data["edges"] = [
        {"id": "e_start_build", "from": "start", "to": "knowledge_build", "map": {"msg": "input"}},
        {
            "id": "e_build_final",
            "from": "knowledge_build",
            "to": "final",
            "map": {"text": "response"},
        },
    ]

    result = execute(
        _plan(data),
        "refresh",
        executor=FakeWorkflowExecutor(),
        preview=True,
    )

    assert result.status == "completed"
    assert result.output == "Preview skipped the knowledge build launch."
    step = next(s for s in result.steps if s.node_id == "knowledge_build")
    assert step.status == "skipped"
    assert "preview skipped" in step.detail


def test_python_code_nodes_go_through_the_configured_sandbox_backend(monkeypatch) -> None:
    """The pluggable backend must cover the path that runs *arbitrary user code*.

    ``CALIBER_TOOL_SANDBOX_BACKEND`` was honoured for registered tools but not here: the
    ``python_code`` node constructed ``LocalSubprocessToolSandbox`` directly. So an operator
    who deployed a container-backed sandbox for isolation did not get it on the one path
    where a workflow author's own code executes. Found by an independent review.

    Asserted by spying on the factory the node must consult, then running a real node — not
    by inspecting the source, which would pass for a node that never runs.
    """
    import caliber.workflows.runtime as runtime_module

    calls: list[object] = []
    real = runtime_module.sandbox_from_optional_config

    def _spy(config: object, **kwargs: object) -> object:
        calls.append(config)
        return real(config, **kwargs)

    monkeypatch.setattr(runtime_module, "sandbox_from_optional_config", _spy)

    data = make_manifest()
    data["nodes"]["python"] = {
        "id": "python",
        "type": "python_code",
        "code": "return str(input).upper()",
        "timeout_seconds": 5,
        "inputs": {"input": {"type": "string"}, "context": {"type": "structured"}},
        "outputs": {
            "text": {"type": "string"},
            "result": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    data["edges"] = [
        {"id": "e_start_python", "from": "start", "to": "python", "map": {"msg": "input"}},
        {"id": "e_python_final", "from": "python", "to": "final", "map": {"text": "response"}},
    ]

    result = execute(_plan(data), "refund", executor=FakeWorkflowExecutor())

    assert result.status == "completed", result.error
    assert calls, "the python_code node must resolve its sandbox through the configured factory"


def test_python_code_node_executes_in_runtime() -> None:
    data = make_manifest()
    data["nodes"]["python"] = {
        "id": "python",
        "type": "python_code",
        "code": 'return {"text": (input or "").upper(), "result": {"chars": len(input or "")}}',
        "timeout_seconds": 5,
        "inputs": {
            "input": {"type": "string"},
            "context": {"type": "structured"},
        },
        "outputs": {
            "text": {"type": "string"},
            "result": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    data["edges"] = [
        {"id": "e_start_python", "from": "start", "to": "python", "map": {"msg": "input"}},
        {"id": "e_python_final", "from": "python", "to": "final", "map": {"text": "response"}},
    ]

    result = execute(_plan(data), "refund", executor=FakeWorkflowExecutor())

    assert result.status == "completed", result.error
    python_step = next(s for s in result.steps if s.node_id == "python")
    assert python_step.output == "REFUND"
    assert result.output == "REFUND"


def test_template_node_renders_text_and_metadata() -> None:
    data = make_manifest()
    data["nodes"]["template"] = {
        "id": "template",
        "type": "template",
        "template": "Hello {{variables.customer.name}} :: {{input}} :: {{variables.order_id}}",
        "output_format": "text",
        "missing_variable_mode": "preserve",
        "inputs": {
            "input": {"type": "string"},
            "variables": {"type": "structured"},
        },
        "outputs": {
            "text": {"type": "string"},
            "result": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    plan = _plan(data)
    port_values: dict[tuple[str, str], object] = {}

    _, step = _run_node(
        plan.ir.nodes["template"],
        plan.ir,
        plan,
        executor=FakeWorkflowExecutor(),
        preview=False,
        inputs={
            "input": "refund request",
            "variables": {"customer": {"name": "Ada"}},
        },
        run_input="refund request",
        port_values=port_values,
        guardrail_results=[],
    )

    assert step.output == "Hello Ada :: refund request :: {{variables.order_id}}"
    assert step.detail == "rendered text template · 2 variables · 1 missing"
    assert port_values[("template", "text")] == step.output
    assert port_values[("template", "result")] == {"rendered": step.output}
    assert port_values[("template", "metadata")] == {
        "output_format": "text",
        "missing_variable_mode": "preserve",
        "used_variables": ["variables.customer.name", "input"],
        "missing_variables": ["variables.order_id"],
        "rendered_bytes": len(step.output.encode("utf-8")),
    }


def test_template_node_renders_json_payload() -> None:
    data = make_manifest()
    data["nodes"]["template"] = {
        "id": "template",
        "type": "template",
        "template": (
            '{"ticket_id":"{{variables.ticket.id}}","summary":"{{input}}",'
            '"labels":{{variables.labels}},"first_label":"{{variables.labels[0]}}",'
            '"attempt":{{variables.attempt}}}'
        ),
        "output_format": "json",
        "missing_variable_mode": "preserve",
        "inputs": {
            "input": {"type": "string"},
            "variables": {"type": "structured"},
        },
        "outputs": {
            "text": {"type": "string"},
            "result": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    plan = _plan(data)
    port_values: dict[tuple[str, str], object] = {}

    _, step = _run_node(
        plan.ir.nodes["template"],
        plan.ir,
        plan,
        executor=FakeWorkflowExecutor(),
        preview=False,
        inputs={
            "input": "refund request",
            "variables": {
                "ticket": {"id": "T-17"},
                "labels": ["billing", "urgent"],
                "attempt": 2,
            },
        },
        run_input="refund request",
        port_values=port_values,
        guardrail_results=[],
    )

    result_payload = {
        "ticket_id": "T-17",
        "summary": "refund request",
        "labels": ["billing", "urgent"],
        "first_label": "billing",
        "attempt": 2,
    }
    assert step.output == json.dumps(result_payload, ensure_ascii=False)
    assert step.detail == "rendered json template · 5 variables"
    assert json.loads(str(port_values[("template", "text")])) == result_payload
    assert port_values[("template", "result")] == result_payload
    assert port_values[("template", "metadata")] == {
        "output_format": "json",
        "missing_variable_mode": "preserve",
        "used_variables": [
            "variables.ticket.id",
            "input",
            "variables.labels",
            "variables.labels[0]",
            "variables.attempt",
        ],
        "missing_variables": [],
        "rendered_bytes": len(step.output.encode("utf-8")),
    }


def test_template_node_missing_variable_error_mode_raises() -> None:
    data = make_manifest()
    data["nodes"]["template"] = {
        "id": "template",
        "type": "template",
        "template": "Hello {{variables.customer.name}}",
        "output_format": "text",
        "missing_variable_mode": "error",
        "inputs": {
            "input": {"type": "string"},
            "variables": {"type": "structured"},
        },
        "outputs": {
            "text": {"type": "string"},
            "result": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    plan = _plan(data)

    with pytest.raises(ToolExecutionError, match="missing variable"):
        _run_node(
            plan.ir.nodes["template"],
            plan.ir,
            plan,
            executor=FakeWorkflowExecutor(),
            preview=False,
            inputs={"input": "refund request", "variables": {}},
            run_input="refund request",
            port_values={},
            guardrail_results=[],
        )


def test_template_node_missing_variable_error_mode_is_normalized_at_workflow_runtime() -> None:
    data = make_manifest()
    data["nodes"]["template"] = {
        "id": "template",
        "type": "template",
        "template": "Hello {{variables.customer.name}}",
        "output_format": "text",
        "missing_variable_mode": "error",
        "inputs": {
            "input": {"type": "string"},
            "variables": {"type": "structured"},
        },
        "outputs": {
            "text": {"type": "string"},
            "result": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    data["edges"] = [
        {"id": "e_start_template", "from": "start", "to": "template", "map": {"msg": "input"}},
        {"id": "e_template_final", "from": "template", "to": "final", "map": {"text": "response"}},
    ]

    result = execute(_plan(data), "refund request", executor=FakeWorkflowExecutor())

    assert result.status == "error"
    assert result.error is not None
    assert "ToolExecutionError: template references missing variable" in result.error


def test_python_code_node_runtime_failure_is_reported() -> None:
    data = make_manifest()
    data["nodes"]["python"] = {
        "id": "python",
        "type": "python_code",
        "code": 'raise ValueError("boom")',
        "timeout_seconds": 5,
        "inputs": {"input": {"type": "string"}},
        "outputs": {"text": {"type": "string"}},
    }
    data["edges"] = [
        {"id": "e_start_python", "from": "start", "to": "python", "map": {"msg": "input"}},
        {"id": "e_python_final", "from": "python", "to": "final", "map": {"text": "response"}},
    ]

    result = execute(_plan(data), "refund", executor=FakeWorkflowExecutor())

    assert result.status == "error", result.error
    assert "python_code node 'python' failed" in (result.error or "")


def _big_python_code_manifest(n_chars: int) -> dict:
    data = make_manifest()
    data["nodes"]["python"] = {
        "id": "python",
        "type": "python_code",
        "code": f'return {{"text": "x" * {n_chars}}}',
        "timeout_seconds": 10,
        "inputs": {"input": {"type": "string"}},
        "outputs": {"text": {"type": "string"}},
    }
    data["edges"] = [
        {"id": "e_start_python", "from": "start", "to": "python", "map": {"msg": "input"}},
        {"id": "e_python_final", "from": "python", "to": "final", "map": {"text": "response"}},
    ]
    return data


def test_python_code_node_honors_raised_max_output_bytes() -> None:
    """A python_code node emitting >64 KB succeeds when the plan carries a
    raised sandbox cap. ``build_plan`` sources this from
    ``config.tool_sandbox_max_output_bytes`` (1 MB default / 16 MB in the suite
    env); without it the 64 KB sandbox class default would truncate the
    runner's JSON and fail the node (see the companion test below)."""
    big = 100_000
    data = _big_python_code_manifest(big)

    result = execute(
        _plan(data, max_output_bytes=1_048_576),
        "go",
        executor=FakeWorkflowExecutor(),
    )

    assert result.status == "completed"
    python_step = next(s for s in result.steps if s.node_id == "python")
    assert len(python_step.output) == big


def test_python_code_node_falls_back_to_default_cap_without_config() -> None:
    """With no cap threaded onto the plan (preview / eval-replay paths), the sandbox
    applies its class default.

    This previously asserted that the node *failed* on a >64 KB output — because the
    oversized value truncated the runner's JSON envelope. That was the C8 bounding
    defect, not intended behaviour: the child now bounds its own capture, so the
    envelope always parses and the node reports a clipped result instead of an error.
    The property still worth guarding is that the cap is applied at all, which the
    raised-cap test above pins from the other direction.
    """
    data = _big_python_code_manifest(100_000)

    result = execute(_plan(data), "go", executor=FakeWorkflowExecutor())

    # The node's *return value* is not what ``max_output_bytes`` bounds — that setting
    # caps captured stdout/stderr. The old assertion only passed because an oversized
    # return value overflowed the clip applied to the JSON envelope, which is the
    # transport, and produced a parse error. With the envelope bounded in the child and
    # parsed unclipped, a large return value now arrives intact.
    assert result.status == "completed"
    assert len(result.output) == 100_000


def test_wait_until_node_blocks_until_resume_boundary() -> None:
    data = make_manifest()
    data["nodes"]["wait_until"] = {
        "id": "wait_until",
        "type": "wait_until",
        "wait_until": "2099-01-01T00:00:00Z",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"output": {"type": "string"}},
    }
    data["edges"] = [
        {"id": "e_start_wait", "from": "start", "to": "wait_until", "map": {"msg": "input"}},
        {"id": "e_wait_final", "from": "wait_until", "to": "final", "map": {"output": "response"}},
    ]
    result = execute(_plan(data), "hello", executor=FakeWorkflowExecutor())
    assert result.status == "blocked"
    assert (result.error or "").startswith("waiting_event:wait_until")
    wait_step = next(step for step in result.steps if step.node_id == "wait_until")
    assert wait_step.checkpoint_state is not None
    assert wait_step.checkpoint_state["input_by_port"] == {"input": "hello"}
    assert str(wait_step.checkpoint_state["resume_at"]).startswith("2099-01-01T00:00:00")


def test_wait_until_node_invalid_timestamp_is_reported() -> None:
    data = make_manifest()
    data["nodes"]["wait_until"] = {
        "id": "wait_until",
        "type": "wait_until",
        "wait_until": "not-a-timestamp",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"output": {"type": "string"}},
    }
    data["edges"] = [
        {"id": "e_start_wait", "from": "start", "to": "wait_until", "map": {"msg": "input"}},
        {"id": "e_wait_final", "from": "wait_until", "to": "final", "map": {"output": "response"}},
    ]

    result = execute(_plan(data), "hello", executor=FakeWorkflowExecutor())

    assert result.status == "error"
    assert "wait_until node 'wait_until' has invalid wait_until" in (result.error or "")


def test_wait_until_resume_checkpoint_manual_override_bypasses_deadline() -> None:
    data = make_manifest()
    data["nodes"]["wait_until"] = {
        "id": "wait_until",
        "type": "wait_until",
        "wait_until": "2099-01-01T00:00:00Z",
        "timezone": "UTC",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"output": {"type": "string"}},
    }
    data["edges"] = [
        {"id": "e_start_wait", "from": "start", "to": "wait_until", "map": {"msg": "input"}},
        {"id": "e_wait_final", "from": "wait_until", "to": "final", "map": {"output": "response"}},
    ]

    result = execute(
        _plan(data),
        "hello",
        executor=FakeWorkflowExecutor(),
        resume_checkpoint=RuntimeResumeCheckpoint(
            node_id="wait_until",
            input_by_port={"input": "hello"},
            injected_inputs={"resume_event": {"manual_resume": True}},
            replay_output=False,
        ),
    )

    assert result.status == "completed"
    assert result.output == "hello"
    wait_step = next(step for step in result.steps if step.node_id == "wait_until")
    assert wait_step.status == "ok"
    assert wait_step.output == "hello"


def test_wait_for_event_node_blocks_without_event_payload() -> None:
    data = make_manifest()
    data["nodes"]["wait_event"] = {
        "id": "wait_event",
        "type": "wait_for_event",
        "event_name": "resume_event",
        "inputs": {"input": {"type": "string"}},
        "outputs": {
            "output": {"type": "string"},
            "event_payload": {"type": "structured"},
            "event_name": {"type": "string"},
        },
    }
    data["edges"] = [
        {"id": "e_start_wait", "from": "start", "to": "wait_event", "map": {"msg": "input"}},
        {"id": "e_wait_final", "from": "wait_event", "to": "final", "map": {"output": "response"}},
    ]
    result = execute(_plan(data), "hello", executor=FakeWorkflowExecutor())
    assert result.status == "blocked"
    assert (result.error or "").startswith("waiting_event:wait_event")
    wait_step = next(step for step in result.steps if step.node_id == "wait_event")
    assert wait_step.checkpoint_state == {
        "input_by_port": {"input": "hello"},
        "expected_event_name": "resume_event",
    }


def test_wait_for_event_node_captures_correlation_and_timeout_metadata() -> None:
    data = make_manifest()
    data["nodes"]["wait_event"] = {
        "id": "wait_event",
        "type": "wait_for_event",
        "event_name": "ticket.approved",
        "correlation_key": "ticket_id",
        "timeout_seconds": 300,
        "inputs": {"input": {"type": "structured"}},
        "outputs": {
            "output": {"type": "string"},
            "event_payload": {"type": "structured"},
            "event_name": {"type": "string"},
        },
    }
    data["edges"] = [
        {"id": "e_start_wait", "from": "start", "to": "wait_event", "map": {"msg": "input"}},
        {"id": "e_wait_final", "from": "wait_event", "to": "final", "map": {"output": "response"}},
    ]

    result = execute(
        _plan(data),
        '{"ticket_id":"T-42","approved":false}',
        executor=FakeWorkflowExecutor(),
    )

    assert result.status == "blocked"
    wait_step = next(step for step in result.steps if step.node_id == "wait_event")
    assert wait_step.checkpoint_state == {
        "input_by_port": {"input": '{"ticket_id":"T-42","approved":false}'},
        "expected_event_name": "ticket.approved",
        "correlation_key": "ticket_id",
        "correlation_value": "T-42",
        "timeout_seconds": 300,
    }


def test_wait_for_event_resume_checkpoint_injects_event_payload() -> None:
    data = make_manifest()
    data["nodes"]["wait_event"] = {
        "id": "wait_event",
        "type": "wait_for_event",
        "event_name": "ticket.approved",
        "inputs": {"input": {"type": "string"}},
        "outputs": {
            "output": {"type": "string"},
            "event_payload": {"type": "structured"},
            "event_name": {"type": "string"},
        },
    }
    data["nodes"]["python"] = {
        "id": "python",
        "type": "python_code",
        "code": (
            'payload = inputs.get("payload") or {}\n'
            'event_name = inputs.get("event_name") or ""\n'
            "return {\"text\": f\"{event_name}::{payload.get('ticket_id')}::{payload.get('approved')}\","
            ' "result": {"payload": payload}}'
        ),
        "inputs": {
            "payload": {"type": "structured"},
            "event_name": {"type": "string"},
        },
        "outputs": {
            "text": {"type": "string"},
            "result": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    data["edges"] = [
        {"id": "e_start_wait", "from": "start", "to": "wait_event", "map": {"msg": "input"}},
        {
            "id": "e_wait_payload",
            "from": "wait_event",
            "to": "python",
            "map": {"event_payload": "payload"},
        },
        {
            "id": "e_wait_name",
            "from": "wait_event",
            "to": "python",
            "map": {"event_name": "event_name"},
        },
        {"id": "e_python_final", "from": "python", "to": "final", "map": {"text": "response"}},
    ]

    result = execute(
        _plan(data),
        "hello",
        executor=FakeWorkflowExecutor(),
        resume_checkpoint=RuntimeResumeCheckpoint(
            node_id="wait_event",
            input_by_port={"input": "hello"},
            injected_inputs={
                "resume_event": {"ticket_id": "T-42", "approved": True},
                "event": {"ticket_id": "T-42", "approved": True},
                "event_payload": {"ticket_id": "T-42", "approved": True},
                "event_name": "ticket.approved",
                "ticket.approved": {"ticket_id": "T-42", "approved": True},
            },
            replay_output=False,
        ),
    )

    assert result.status == "completed"
    assert result.output == "ticket.approved::T-42::True"
    wait_step = next(step for step in result.steps if step.node_id == "wait_event")
    assert wait_step.status == "ok"


def test_wait_for_event_resume_checkpoint_rejects_missing_event_payload() -> None:
    data = make_manifest()
    data["nodes"]["wait_event"] = {
        "id": "wait_event",
        "type": "wait_for_event",
        "event_name": "ticket.approved",
        "inputs": {"input": {"type": "string"}},
        "outputs": {
            "output": {"type": "string"},
            "event_payload": {"type": "structured"},
            "event_name": {"type": "string"},
        },
    }
    data["edges"] = [
        {"id": "e_start_wait", "from": "start", "to": "wait_event", "map": {"msg": "input"}},
        {"id": "e_wait_final", "from": "wait_event", "to": "final", "map": {"output": "response"}},
    ]

    result = execute(
        _plan(data),
        "hello",
        executor=FakeWorkflowExecutor(),
        resume_checkpoint=RuntimeResumeCheckpoint(
            node_id="wait_event",
            input_by_port={"input": "hello"},
            injected_inputs={"event_name": "ticket.approved"},
            replay_output=False,
        ),
    )

    assert result.status == "error"
    assert result.error is not None
    assert (
        "resume checkpoint for wait_for_event node 'wait_event' requires a resumed event payload"
        in result.error
    )


def test_wait_for_event_resume_checkpoint_rejects_mismatched_event_name() -> None:
    data = make_manifest()
    data["nodes"]["wait_event"] = {
        "id": "wait_event",
        "type": "wait_for_event",
        "event_name": "ticket.approved",
        "inputs": {"input": {"type": "string"}},
        "outputs": {
            "output": {"type": "string"},
            "event_payload": {"type": "structured"},
            "event_name": {"type": "string"},
        },
    }
    data["edges"] = [
        {"id": "e_start_wait", "from": "start", "to": "wait_event", "map": {"msg": "input"}},
        {"id": "e_wait_final", "from": "wait_event", "to": "final", "map": {"output": "response"}},
    ]

    result = execute(
        _plan(data),
        "hello",
        executor=FakeWorkflowExecutor(),
        resume_checkpoint=RuntimeResumeCheckpoint(
            node_id="wait_event",
            input_by_port={"input": "hello"},
            injected_inputs={
                "event_payload": {"ticket_id": "T-42"},
                "event_name": "ticket.denied",
            },
            replay_output=False,
        ),
    )

    assert result.status == "error"
    assert result.error is not None
    assert (
        "resume checkpoint for wait_for_event node 'wait_event' expected event 'ticket.approved' but received 'ticket.denied'"
        in result.error
    )


def test_event_resume_template_blocks_with_correlation_and_resumes() -> None:
    data = _starter_manifest("event_resume")
    run_input = '{"document_id":"DOC-7","request":"summarize the release"}'

    blocked = execute(_plan(data), run_input, executor=FakeWorkflowExecutor())

    assert blocked.status == "blocked"
    wait_step = next(step for step in blocked.steps if step.node_id == "wait_gate")
    assert wait_step.checkpoint_state == {
        "input_by_port": {"input": run_input},
        "expected_event_name": "documents.ready",
        "correlation_key": "document_id",
        "correlation_value": "DOC-7",
        "timeout_seconds": 3600,
    }

    resumed = execute(
        _plan(data),
        run_input,
        executor=FakeWorkflowExecutor(),
        resume_checkpoint=RuntimeResumeCheckpoint(
            node_id="wait_gate",
            input_by_port={"input": run_input},
            injected_inputs={
                "resume_event": {"document_id": "DOC-7", "status": "ready"},
                "event": {"document_id": "DOC-7", "status": "ready"},
                "event_payload": {"document_id": "DOC-7", "status": "ready"},
                "event_name": "documents.ready",
                "documents.ready": {"document_id": "DOC-7", "status": "ready"},
            },
            replay_output=False,
        ),
    )

    assert resumed.status == "completed"
    assert [step.node_id for step in resumed.steps] == ["wait_gate", "agent", "final"]
    agent_step = next(step for step in resumed.steps if step.node_id == "agent")
    assert agent_step.input_by_port == {"input": run_input}
    assert resumed.output == agent_step.output


def test_resume_checkpoint_replays_agent_output_without_rerunning_agent() -> None:
    class _ExplodingExecutor:
        def run_agent(self, *args, **kwargs):
            raise AssertionError("agent should not be re-executed during replay")

    result = execute(
        _plan(make_manifest("resume-agent-replay")),
        "ignored",
        executor=_ExplodingExecutor(),
        resume_checkpoint=RuntimeResumeCheckpoint(
            node_id="agent",
            output="checkpoint answer",
            replay_output=True,
        ),
    )

    assert result.status == "completed"
    assert result.output == "checkpoint answer"
    assert [step.node_id for step in result.steps] == ["final"]


def test_resume_checkpoint_replays_output_by_port_for_downstream_nodes() -> None:
    data = make_manifest("resume-port-replay")
    data["nodes"]["producer"] = {
        "id": "producer",
        "type": "python_code",
        "code": 'raise RuntimeError("producer should not run during replay")',
        "inputs": {"input": {"type": "string"}},
        "outputs": {
            "text": {"type": "string"},
            "result": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    data["nodes"]["consumer"] = {
        "id": "consumer",
        "type": "python_code",
        "code": (
            'payload = inputs.get("payload")\n'
            'label = inputs.get("label") or ""\n'
            'value = payload.get("value") if isinstance(payload, dict) else f"bad:{payload}"\n'
            'return {"text": f"{label}::{value}", "result": {"value": value}, "metadata": {}}'
        ),
        "inputs": {
            "payload": {"type": "structured"},
            "label": {"type": "string"},
        },
        "outputs": {
            "text": {"type": "string"},
            "result": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    data["edges"] = [
        {"id": "e_start_producer", "from": "start", "to": "producer", "map": {"msg": "input"}},
        {"id": "e_producer_label", "from": "producer", "to": "consumer", "map": {"text": "label"}},
        {
            "id": "e_producer_payload",
            "from": "producer",
            "to": "consumer",
            "map": {"result": "payload"},
        },
        {"id": "e_consumer_final", "from": "consumer", "to": "final", "map": {"text": "response"}},
    ]

    result = execute(
        _plan(data),
        "ignored",
        executor=FakeWorkflowExecutor(),
        resume_checkpoint=RuntimeResumeCheckpoint(
            node_id="producer",
            output="WRONG",
            output_by_port={
                "text": "checkpoint",
                "result": {"value": 7},
                "metadata": {"source": "resume"},
            },
            replay_output=True,
        ),
    )

    assert result.status == "completed"
    assert result.output == "checkpoint::7"
    assert [step.node_id for step in result.steps] == ["consumer", "final"]


@pytest.mark.parametrize(
    ("node_id", "node_payload"),
    [
        (
            "wait_until",
            {
                "id": "wait_until",
                "type": "wait_until",
                "wait_until": "2099-01-01T00:00:00Z",
                "timezone": "UTC",
                "inputs": {"input": {"type": "string"}},
                "outputs": {"output": {"type": "string"}},
            },
        ),
        (
            "wait_event",
            {
                "id": "wait_event",
                "type": "wait_for_event",
                "event_name": "ticket.approved",
                "inputs": {"input": {"type": "string"}},
                "outputs": {
                    "output": {"type": "string"},
                    "event_payload": {"type": "structured"},
                    "event_name": {"type": "string"},
                },
            },
        ),
        (
            "human_gate",
            {
                "id": "human_gate",
                "type": "human_approval",
                "inputs": {"request": {"type": "string"}},
                "outputs": {"request": {"type": "string"}},
            },
        ),
    ],
)
def test_resume_checkpoint_rejects_output_replay_for_gate_nodes(
    node_id: str,
    node_payload: dict[str, object],
) -> None:
    data = make_manifest(f"resume-gate-replay-{node_id}")
    data["nodes"][node_id] = node_payload
    if node_id == "human_gate":
        data["edges"] = [
            {"id": "e_start_agent", "from": "start", "to": "agent", "map": {"msg": "input"}},
            {
                "id": "e_agent_gate",
                "from": "agent",
                "to": "human_gate",
                "map": {"final_output": "request"},
            },
            {
                "id": "e_gate_final",
                "from": "human_gate",
                "to": "final",
                "map": {"request": "response"},
            },
        ]
    else:
        data["edges"] = [
            {"id": "e_start_gate", "from": "start", "to": node_id, "map": {"msg": "input"}},
            {"id": "e_gate_final", "from": node_id, "to": "final", "map": {"output": "response"}},
        ]

    result = execute(
        _plan(data),
        "ignored",
        executor=FakeWorkflowExecutor(),
        resume_checkpoint=RuntimeResumeCheckpoint(
            node_id=node_id,
            output="stale output",
            replay_output=True,
        ),
    )

    assert result.status == "error"
    assert result.error is not None
    assert (
        f"resume checkpoint for gated node '{node_id}' cannot replay output past the gate"
        in result.error
    )


def test_resume_checkpoint_rejects_output_replay_for_approval_required_tool_nodes() -> None:
    string = IRType("string")
    binding = IRToolBinding(
        local_name="delete_record",
        registry_ref="tool.delete_record.v1",
        version_constraint=">=1",
        requires_approval=True,
        side_effect_level="write",
        allow_in_preview=True,
        module_path="<in-memory>",
        callable_name="delete_record",
    )
    plan = RuntimePlan(
        ir=IRWorkflow(
            workflow_id="wf-tool-approval-replay-rejected",
            version="1",
            nodes={
                "start": IRNode("start", NodeType.START, outputs={"msg": string}),
                "tool_lookup": IRTool(
                    node_id="tool_lookup",
                    node_type=NodeType.TOOL,
                    binding=binding,
                    inputs={"input": string},
                    outputs={
                        "text": string,
                        "result": IRType("structured"),
                        "metadata": IRType("structured"),
                    },
                ),
                "final": IRNode("final", NodeType.OUTPUT, inputs={"response": string}),
            },
            edges=[
                IREdge("e1", "start", "msg", "tool_lookup", "input", string),
                IREdge("e2", "tool_lookup", "text", "final", "response", string),
            ],
            entry_node_id="start",
            output_node_id="final",
        ),
        resolver=InMemoryToolResolver.from_callables(
            {"tool.delete_record.v1": lambda _input="": {"ok": True}}
        ),
    )

    result = execute(
        plan,
        "delete ticket T-900",
        executor=FakeWorkflowExecutor(),
        resume_checkpoint=RuntimeResumeCheckpoint(
            node_id="tool_lookup",
            output="executed delete ticket T-900",
            replay_output=True,
        ),
    )

    assert result.status == "error"
    assert result.error is not None
    assert (
        "resume checkpoint for gated node 'tool_lookup' cannot replay output past the gate"
        in result.error
    )


@pytest.mark.parametrize("replay_output", [False, True])
def test_resume_checkpoint_rejects_missing_node_in_current_plan(replay_output: bool) -> None:
    result = execute(
        _plan(make_manifest("resume-missing-node")),
        "ignored",
        executor=FakeWorkflowExecutor(),
        resume_checkpoint=RuntimeResumeCheckpoint(
            node_id="deleted_gate",
            output="stale output",
            input_by_port={"input": "retry input"},
            replay_output=replay_output,
        ),
    )

    assert result.status == "error"
    assert result.error is not None
    assert (
        "resume checkpoint references missing node 'deleted_gate' in the current workflow plan"
        in result.error
    )


def test_resume_checkpoint_rejects_checkpoint_kind_drift_for_existing_node() -> None:
    data = make_manifest("resume-kind-drift")
    data["nodes"]["human_gate"] = {
        "id": "human_gate",
        "type": "python_code",
        "code": 'return {"output": payload["input"]}',
        "inputs": {"input": {"type": "string"}},
        "outputs": {"output": {"type": "string"}},
    }
    data["edges"] = [
        {"id": "e_start_agent", "from": "start", "to": "agent", "map": {"msg": "input"}},
        {
            "id": "e_agent_gate",
            "from": "agent",
            "to": "human_gate",
            "map": {"final_output": "input"},
        },
        {"id": "e_gate_final", "from": "human_gate", "to": "final", "map": {"output": "response"}},
    ]

    result = execute(
        _plan(data),
        "ignored",
        executor=FakeWorkflowExecutor(),
        resume_checkpoint=RuntimeResumeCheckpoint(
            node_id="human_gate",
            checkpoint_kind="human_approval",
            input_by_port={"request": "approved output"},
            replay_output=False,
        ),
    )

    assert result.status == "error"
    assert result.error is not None
    assert (
        "resume checkpoint kind 'human_approval' does not match current node 'human_gate' type 'python_code'"
        in result.error
    )


def test_for_each_node_invokes_target_agent() -> None:
    data = make_manifest()
    data["nodes"]["for_each"] = {
        "id": "for_each",
        "type": "for_each",
        "target_node_id": "agent",
        "inputs": {"items": {"type": "structured"}},
        "outputs": {"text": {"type": "string"}, "results": {"type": "structured"}},
    }
    data["edges"] = [
        {"id": "e_start_loop", "from": "start", "to": "for_each", "map": {"msg": "items"}},
        {"id": "e_loop_final", "from": "for_each", "to": "final", "map": {"text": "response"}},
    ]
    result = execute(
        _plan(data),
        '["refund","shipping"]',
        executor=FakeWorkflowExecutor(),
    )
    assert result.status == "completed"
    loop_step = next(s for s in result.steps if s.node_id == "for_each")
    assert "processed 2 item(s)" in loop_step.detail


def _for_each_manifest() -> dict:
    data = make_manifest()
    data["nodes"]["for_each"] = {
        "id": "for_each",
        "type": "for_each",
        "target_node_id": "agent",
        "inputs": {"items": {"type": "structured"}},
        "outputs": {"text": {"type": "string"}, "results": {"type": "structured"}},
    }
    data["edges"] = [
        {"id": "e_start_loop", "from": "start", "to": "for_each", "map": {"msg": "items"}},
        {"id": "e_loop_final", "from": "for_each", "to": "final", "map": {"text": "response"}},
    ]
    return data


class _RecordingExecutor:
    """Test executor that echoes its input and records observed concurrency.

    With ``barrier_parties`` set, every agent call blocks on a shared barrier so
    the run only completes if that many calls are in flight simultaneously — a
    deterministic proof that the ForEach node truly parallelizes (a sequential
    loop would never gather enough parties and would time out).
    """

    def __init__(
        self,
        *,
        barrier_parties: int | None = None,
        barrier_timeout: float = 10.0,
        tokens: int = 7,
        fail_on: str | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.calls: list[str] = []
        self._barrier = threading.Barrier(barrier_parties) if barrier_parties else None
        self._barrier_timeout = barrier_timeout
        self._tokens = tokens
        self._fail_on = fail_on

    def run_agent(self, agent, input_text, *, tool_callables, preview):
        if self._fail_on is not None and input_text == self._fail_on:
            raise RuntimeError(f"boom:{input_text}")
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append(input_text)
        try:
            if self._barrier is not None:
                self._barrier.wait(timeout=self._barrier_timeout)
            else:
                time.sleep(0.01)
        finally:
            with self._lock:
                self.active -= 1
        return AgentTurnResult(final_output=input_text, tokens=self._tokens)


def test_for_each_parallel_matches_sequential_order_and_tokens() -> None:
    payload = '["i0","i1","i2","i3","i4","i5"]'
    seq = execute(
        _plan(_for_each_manifest(), foreach_max_workers=1), payload, executor=_RecordingExecutor()
    )
    par = execute(
        _plan(_for_each_manifest(), foreach_max_workers=4), payload, executor=_RecordingExecutor()
    )
    assert seq.status == par.status == "completed"
    seq_loop = next(s for s in seq.steps if s.node_id == "for_each")
    par_loop = next(s for s in par.steps if s.node_id == "for_each")
    # Output order preserved: the parallel result is byte-identical to sequential.
    assert par_loop.output == seq_loop.output == "i0\ni1\ni2\ni3\ni4\ni5"
    assert "processed 6 item(s)" in par_loop.detail
    # Token total is identical regardless of fan-out → summation is correct.
    assert par.tokens == seq.tokens


def test_for_each_parallel_runs_items_concurrently() -> None:
    # Barrier(4) only releases if all four agent calls run at once.
    ex = _RecordingExecutor(barrier_parties=4)
    result = execute(
        _plan(_for_each_manifest(), foreach_max_workers=4), '["a","b","c","d"]', executor=ex
    )
    assert result.status == "completed"
    assert ex.max_active == 4


def test_for_each_sequential_never_overlaps() -> None:
    ex = _RecordingExecutor()
    result = execute(
        _plan(_for_each_manifest(), foreach_max_workers=1), '["a","b","c","d"]', executor=ex
    )
    assert result.status == "completed"
    assert ex.max_active == 1  # workers=1 → strictly sequential


def test_for_each_parallel_tolerates_worker_exception() -> None:
    ex = _RecordingExecutor(fail_on="c")
    result = execute(
        _plan(_for_each_manifest(), foreach_max_workers=4), '["a","b","c","d"]', executor=ex
    )
    # One item's failure must not abort the fan-out: the run completes, the failed
    # item is recorded with an error + empty output, and the others still produce.
    assert result.status == "completed"
    loop = next(s for s in result.steps if s.node_id == "for_each")
    assert "1 failed" in loop.detail
    # the 3 healthy items remain in order; the failed one contributes empty output
    assert loop.output == "a\nb\n\nd"


def test_for_each_node_runs_python_target() -> None:
    data = make_manifest()
    data["nodes"]["python"] = {
        "id": "python",
        "type": "python_code",
        "code": textwrap.dedent(
            """
            return {
                "text": str(input or run_input).upper(),
                "result": {"seen": input},
            }
            """
        ).strip(),
        "inputs": {"input": {"type": "string"}},
        "outputs": {"text": {"type": "string"}, "result": {"type": "structured"}},
    }
    data["nodes"]["for_each"] = {
        "id": "for_each",
        "type": "for_each",
        "target_node_id": "python",
        "inputs": {"items": {"type": "structured"}},
        "outputs": {
            "text": {"type": "string"},
            "results": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    data["edges"] = [
        {"id": "e_start_loop", "from": "start", "to": "for_each", "map": {"msg": "items"}},
        {"id": "e_loop_final", "from": "for_each", "to": "final", "map": {"text": "response"}},
    ]

    result = execute(
        _plan(data),
        '["alpha","beta"]',
        executor=FakeWorkflowExecutor(),
    )

    assert result.status == "completed"
    assert result.output == "ALPHA\nBETA"
    loop_step = next(s for s in result.steps if s.node_id == "for_each")
    assert "processed 2 item(s) via python_code" in loop_step.detail
    assert isinstance(loop_step.output_by_port, dict)
    results = loop_step.output_by_port["results"]
    assert results[0]["node_type"] == "python_code"
    assert results[0]["outputs"]["result"]["result"]["seen"] == "alpha"
    assert loop_step.output_by_port["metadata"]["target_node_type"] == "python_code"


def test_for_each_node_reports_invalid_inline_target_at_workflow_runtime() -> None:
    data = make_manifest()
    data["nodes"]["wait_event"] = {
        "id": "wait_event",
        "type": "wait_for_event",
        "event_name": "resume_event",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"output": {"type": "string"}},
    }
    data["nodes"]["for_each"] = {
        "id": "for_each",
        "type": "for_each",
        "target_node_id": "wait_event",
        "inputs": {"items": {"type": "structured"}},
        "outputs": {
            "results": {"type": "structured"},
            "text": {"type": "string"},
            "metadata": {"type": "structured"},
        },
    }
    data["edges"] = [
        {"id": "e_start_for_each", "from": "start", "to": "for_each", "map": {"msg": "items"}},
        {"id": "e_for_each_final", "from": "for_each", "to": "final", "map": {"text": "response"}},
    ]

    result = execute(_plan(data), ["alpha", "beta"], executor=FakeWorkflowExecutor())

    assert result.status == "error"
    assert result.error is not None
    assert (
        "ToolExecutionError: for_each node 'for_each' target 'wait_event' must be an executable node"
        in result.error
    )


def test_loop_node_repeats_until_stop_condition() -> None:
    data = make_manifest()
    data["nodes"]["counter"] = {
        "id": "counter",
        "type": "python_code",
        "code": textwrap.dedent(
            """
            current = inputs.get("count")
            if current is None:
                current = int(str(run_input or "0") or "0")
            count = int(current) + 1
            next_state = {"count": count, "done": count >= 3}
            return {"text": str(count), "result": next_state}
            """
        ).strip(),
        "inputs": {"count": {"type": "structured"}, "done": {"type": "boolean"}},
        "outputs": {"text": {"type": "string"}, "result": {"type": "structured"}},
    }
    data["nodes"]["loop"] = {
        "id": "loop",
        "type": "loop",
        "target_node_id": "counter",
        "max_iterations": 5,
        "stop_condition": "state.done",
        "inputs": {
            "input": {"type": "string"},
            "state": {"type": "structured"},
        },
        "outputs": {
            "output": {"type": "string"},
            "result": {"type": "structured"},
            "iterations": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    data["edges"] = [
        {"id": "e_start_loop", "from": "start", "to": "loop", "map": {"msg": "input"}},
        {"id": "e_loop_final", "from": "loop", "to": "final", "map": {"output": "response"}},
    ]

    result = execute(_plan(data), "0", executor=FakeWorkflowExecutor())

    assert result.status == "completed"
    assert result.output == "3"
    loop_step = next(s for s in result.steps if s.node_id == "loop")
    assert "iterated 3 time(s) via python_code until stop condition" in loop_step.detail
    assert isinstance(loop_step.output_by_port, dict)
    assert loop_step.output_by_port["result"]["count"] == 3
    assert loop_step.output_by_port["metadata"]["stop_reason"] == "stop_condition"
    assert len(loop_step.output_by_port["iterations"]) == 3


def test_loop_node_stops_at_max_iterations_when_condition_never_matches() -> None:
    data = make_manifest()
    data["nodes"]["counter"] = {
        "id": "counter",
        "type": "python_code",
        "code": textwrap.dedent(
            """
            current = inputs.get("count")
            if current is None:
                current = int(str(run_input or "0") or "0")
            count = int(current) + 1
            return {"text": str(count), "result": {"count": count}}
            """
        ).strip(),
        "inputs": {"count": {"type": "structured"}, "done": {"type": "boolean"}},
        "outputs": {"text": {"type": "string"}, "result": {"type": "structured"}},
    }
    data["nodes"]["loop"] = {
        "id": "loop",
        "type": "loop",
        "target_node_id": "counter",
        "max_iterations": 2,
        "stop_condition": "",
        "inputs": {
            "input": {"type": "string"},
            "state": {"type": "structured"},
        },
        "outputs": {
            "output": {"type": "string"},
            "result": {"type": "structured"},
            "iterations": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    data["edges"] = [
        {"id": "e_start_loop", "from": "start", "to": "loop", "map": {"msg": "input"}},
        {"id": "e_loop_final", "from": "loop", "to": "final", "map": {"output": "response"}},
    ]

    result = execute(_plan(data), "0", executor=FakeWorkflowExecutor())

    assert result.status == "completed"
    assert result.output == "2"
    loop_step = next(s for s in result.steps if s.node_id == "loop")
    assert "iterated 2 time(s) via python_code (max reached)" in loop_step.detail
    assert isinstance(loop_step.output_by_port, dict)
    assert loop_step.output_by_port["result"]["count"] == 2
    assert loop_step.output_by_port["metadata"]["stop_reason"] == "max_iterations_reached"


def test_loop_node_reports_invalid_stop_condition_at_workflow_runtime() -> None:
    data = make_manifest()
    data["nodes"]["counter"] = {
        "id": "counter",
        "type": "python_code",
        "code": 'return {"text": "1", "result": {"count": 1}}',
        "inputs": {"input": {"type": "string"}},
        "outputs": {"text": {"type": "string"}, "result": {"type": "structured"}},
    }
    data["nodes"]["loop"] = {
        "id": "loop",
        "type": "loop",
        "target_node_id": "counter",
        "max_iterations": 2,
        "stop_condition": "state[",
        "inputs": {
            "input": {"type": "string"},
            "state": {"type": "structured"},
        },
        "outputs": {
            "output": {"type": "string"},
            "result": {"type": "structured"},
            "iterations": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    data["edges"] = [
        {"id": "e_start_loop", "from": "start", "to": "loop", "map": {"msg": "input"}},
        {"id": "e_loop_final", "from": "loop", "to": "final", "map": {"output": "response"}},
    ]

    result = execute(_plan(data), "0", executor=FakeWorkflowExecutor())

    assert result.status == "error"
    assert result.error is not None
    assert "ToolExecutionError: loop node 'loop' stop_condition is invalid" in result.error


def test_error_boundary_handles_non_agent_target_and_python_compensation() -> None:
    data = make_manifest()
    data["nodes"]["knowledge"] = {
        "id": "knowledge",
        "type": "knowledge_query",
        "knowledge_base_id": "KB-1",
        "retrieval_modes": ["dense"],
        "top_k": 3,
        "inputs": {"question": {"type": "string"}},
        "outputs": {"text": {"type": "string"}, "result": {"type": "structured"}},
    }
    data["nodes"]["python"] = {
        "id": "python",
        "type": "python_code",
        "code": 'return {"text": f"recovered:{input or run_input}", "result": {"ok": True}}',
        "inputs": {"input": {"type": "string"}},
        "outputs": {"text": {"type": "string"}, "result": {"type": "structured"}},
    }
    data["nodes"]["boundary"] = {
        "id": "boundary",
        "type": "error_boundary",
        "target_node_id": "knowledge",
        "compensate_with": "python",
        "fallback_text": "fallback",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"output": {"type": "string"}, "error": {"type": "structured"}},
    }
    data["edges"] = [
        {"id": "e_start_boundary", "from": "start", "to": "boundary", "map": {"msg": "input"}},
        {
            "id": "e_boundary_final",
            "from": "boundary",
            "to": "final",
            "map": {"output": "response"},
        },
    ]

    result = execute(_plan(data), "hello", executor=FakeWorkflowExecutor())

    assert result.status == "completed"
    assert result.output == "recovered:hello"
    boundary_step = next(s for s in result.steps if s.node_id == "boundary")
    assert "handled error" in boundary_step.detail
    assert isinstance(boundary_step.output_by_port, dict)
    assert boundary_step.output_by_port["error"]["target_node_type"] == "knowledge_query"
    assert boundary_step.output_by_port["error"]["compensation_node_type"] == "python_code"
    assert (
        boundary_step.output_by_port["error"]["compensation_outputs"]["result"]["result"]["ok"]
        is True
    )


def test_error_boundary_handles_agent_target_errors_and_python_compensation() -> None:
    data = make_manifest()
    data["nodes"]["agent_fail"] = {
        "id": "agent_fail",
        "type": "agent",
        "name": "failing-agent",
        "model": "inherit",
        "instructions": {"type": "inline", "text": "You are helpful."},
        "tools": [],
        "inputs": {"input": {"type": "string"}},
        "outputs": {"final_output": {"type": "string"}},
    }
    data["nodes"]["python"] = {
        "id": "python",
        "type": "python_code",
        "code": 'return {"text": f"recovered:{input or run_input}", "result": {"ok": True}}',
        "inputs": {"input": {"type": "string"}},
        "outputs": {"text": {"type": "string"}, "result": {"type": "structured"}},
    }
    data["nodes"]["boundary"] = {
        "id": "boundary",
        "type": "error_boundary",
        "target_node_id": "agent_fail",
        "compensate_with": "python",
        "fallback_text": "fallback",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"output": {"type": "string"}, "error": {"type": "structured"}},
    }
    data["edges"] = [
        {"id": "e_start_boundary", "from": "start", "to": "boundary", "map": {"msg": "input"}},
        {
            "id": "e_boundary_final",
            "from": "boundary",
            "to": "final",
            "map": {"output": "response"},
        },
    ]

    class _BoomExecutor(FakeWorkflowExecutor):
        def run_agent(self, agent, input_text, *, history=None, tool_callables, preview):  # type: ignore[override]
            del agent, history, tool_callables, preview
            raise RuntimeError(f"boom:{input_text}")

    result = execute(_plan(data), "hello", executor=_BoomExecutor())

    assert result.status == "completed"
    assert result.output == "recovered:hello"
    boundary_step = next(s for s in result.steps if s.node_id == "boundary")
    assert "handled error" in boundary_step.detail
    assert isinstance(boundary_step.output_by_port, dict)
    assert boundary_step.output_by_port["error"]["target_node_type"] == "agent"
    assert boundary_step.output_by_port["error"]["compensation_node_type"] == "python_code"
    assert (
        boundary_step.output_by_port["error"]["compensation_outputs"]["result"]["result"]["ok"]
        is True
    )


def test_error_boundary_handles_template_target_errors_and_python_compensation() -> None:
    data = make_manifest()
    data["nodes"]["template_fail"] = {
        "id": "template_fail",
        "type": "template",
        "template": "Hello {{variables.customer.name}}",
        "output_format": "text",
        "missing_variable_mode": "error",
        "inputs": {
            "input": {"type": "string"},
            "variables": {"type": "structured"},
        },
        "outputs": {
            "text": {"type": "string"},
            "result": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    data["nodes"]["python"] = {
        "id": "python",
        "type": "python_code",
        "code": 'return {"text": f"recovered:{input or run_input}", "result": {"ok": True}}',
        "inputs": {"input": {"type": "string"}},
        "outputs": {"text": {"type": "string"}, "result": {"type": "structured"}},
    }
    data["nodes"]["boundary"] = {
        "id": "boundary",
        "type": "error_boundary",
        "target_node_id": "template_fail",
        "compensate_with": "python",
        "fallback_text": "fallback",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"output": {"type": "string"}, "error": {"type": "structured"}},
    }
    data["edges"] = [
        {"id": "e_start_boundary", "from": "start", "to": "boundary", "map": {"msg": "input"}},
        {
            "id": "e_boundary_final",
            "from": "boundary",
            "to": "final",
            "map": {"output": "response"},
        },
    ]

    result = execute(_plan(data), "hello", executor=FakeWorkflowExecutor())

    assert result.status == "completed"
    assert result.output == "recovered:hello"
    boundary_step = next(s for s in result.steps if s.node_id == "boundary")
    assert "handled error" in boundary_step.detail
    assert isinstance(boundary_step.output_by_port, dict)
    assert boundary_step.output_by_port["error"]["target_node_type"] == "template"
    assert boundary_step.output_by_port["error"]["compensation_node_type"] == "python_code"
    assert (
        boundary_step.output_by_port["error"]["compensation_outputs"]["result"]["result"]["ok"]
        is True
    )


def test_error_boundary_reports_compensation_failure_at_workflow_runtime() -> None:
    data = make_manifest()
    data["nodes"]["template_fail"] = {
        "id": "template_fail",
        "type": "template",
        "template": "Hello {{variables.customer.name}}",
        "output_format": "text",
        "missing_variable_mode": "error",
        "inputs": {
            "input": {"type": "string"},
            "variables": {"type": "structured"},
        },
        "outputs": {
            "text": {"type": "string"},
            "result": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    data["nodes"]["python"] = {
        "id": "python",
        "type": "python_code",
        "code": 'raise ValueError(f"compensation boom:{input or run_input}")',
        "inputs": {"input": {"type": "string"}},
        "outputs": {"text": {"type": "string"}, "result": {"type": "structured"}},
    }
    data["nodes"]["boundary"] = {
        "id": "boundary",
        "type": "error_boundary",
        "target_node_id": "template_fail",
        "compensate_with": "python",
        "fallback_text": "fallback",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"output": {"type": "string"}, "error": {"type": "structured"}},
    }
    data["edges"] = [
        {"id": "e_start_boundary", "from": "start", "to": "boundary", "map": {"msg": "input"}},
        {
            "id": "e_boundary_final",
            "from": "boundary",
            "to": "final",
            "map": {"output": "response"},
        },
    ]

    result = execute(_plan(data), "hello", executor=FakeWorkflowExecutor())

    assert result.status == "error"
    assert result.error is not None
    assert "error_boundary node 'boundary' compensation target 'python' failed" in result.error
    assert "handling target 'template_fail'" in result.error
    assert "original error: template references missing variable" in result.error
    assert "compensation error: python_code node 'python' failed" in result.error
    assert "compensation boom:hello" in result.error


def test_error_boundary_reports_invalid_compensation_target_at_workflow_runtime() -> None:
    data = make_manifest()
    data["nodes"]["knowledge"] = {
        "id": "knowledge",
        "type": "knowledge_query",
        "knowledge_base_id": "KB-1",
        "retrieval_modes": ["dense"],
        "top_k": 3,
        "inputs": {"question": {"type": "string"}},
        "outputs": {"text": {"type": "string"}, "result": {"type": "structured"}},
    }
    data["nodes"]["parallel"] = {
        "id": "parallel",
        "type": "parallel",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"output": {"type": "string"}},
    }
    data["nodes"]["boundary"] = {
        "id": "boundary",
        "type": "error_boundary",
        "target_node_id": "knowledge",
        "compensate_with": "parallel",
        "fallback_text": "fallback",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"output": {"type": "string"}, "error": {"type": "structured"}},
    }
    data["edges"] = [
        {"id": "e_start_boundary", "from": "start", "to": "boundary", "map": {"msg": "input"}},
        {
            "id": "e_boundary_final",
            "from": "boundary",
            "to": "final",
            "map": {"output": "response"},
        },
    ]

    result = execute(_plan(data), "hello", executor=FakeWorkflowExecutor())

    assert result.status == "error"
    assert result.error is not None
    assert (
        "ToolExecutionError: error_boundary node 'boundary' compensation target 'parallel' must be an executable node"
        in result.error
    )


def test_join_any_continues_when_parallel_branch_waits() -> None:
    data = make_manifest()
    data["nodes"]["parallel"] = {
        "id": "parallel",
        "type": "parallel",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"output": {"type": "string"}},
    }
    data["nodes"]["wait_event"] = {
        "id": "wait_event",
        "type": "wait_for_event",
        "event_name": "resume_event",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"output": {"type": "string"}},
    }
    data["nodes"]["join_any"] = {
        "id": "join_any",
        "type": "join",
        "mode": "any",
        "inputs": {"left": {"type": "string"}, "right": {"type": "string"}},
        "outputs": {"output": {"type": "string"}},
    }
    data["edges"] = [
        {"id": "e_start_parallel", "from": "start", "to": "parallel", "map": {"msg": "input"}},
        {
            "id": "e_parallel_wait",
            "from": "parallel",
            "to": "wait_event",
            "map": {"output": "input"},
        },
        {"id": "e_parallel_agent", "from": "parallel", "to": "agent", "map": {"output": "input"}},
        {"id": "e_wait_join", "from": "wait_event", "to": "join_any", "map": {"output": "left"}},
        {"id": "e_agent_join", "from": "agent", "to": "join_any", "map": {"final_output": "right"}},
        {"id": "e_join_final", "from": "join_any", "to": "final", "map": {"output": "response"}},
    ]

    result = execute(_plan(data), "hello", executor=FakeWorkflowExecutor())
    assert result.status == "completed"
    assert any(step.node_id == "wait_event" and step.status == "blocked" for step in result.steps)
    assert any(step.node_id == "join_any" and step.status == "ok" for step in result.steps)
    assert any(step.node_id == "final" and step.status == "ok" for step in result.steps)


def test_join_all_waits_for_every_parallel_branch() -> None:
    data = make_manifest()
    data["nodes"]["parallel"] = {
        "id": "parallel",
        "type": "parallel",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"output": {"type": "string"}},
    }
    data["nodes"]["wait_event"] = {
        "id": "wait_event",
        "type": "wait_for_event",
        "event_name": "resume_event",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"output": {"type": "string"}},
    }
    data["nodes"]["join_all"] = {
        "id": "join_all",
        "type": "join",
        "mode": "all",
        "inputs": {"left": {"type": "string"}, "right": {"type": "string"}},
        "outputs": {"output": {"type": "string"}, "merged": {"type": "structured"}},
    }
    data["edges"] = [
        {"id": "e_start_parallel", "from": "start", "to": "parallel", "map": {"msg": "input"}},
        {
            "id": "e_parallel_wait",
            "from": "parallel",
            "to": "wait_event",
            "map": {"output": "input"},
        },
        {"id": "e_parallel_agent", "from": "parallel", "to": "agent", "map": {"output": "input"}},
        {"id": "e_wait_join", "from": "wait_event", "to": "join_all", "map": {"output": "left"}},
        {"id": "e_agent_join", "from": "agent", "to": "join_all", "map": {"final_output": "right"}},
        {"id": "e_join_final", "from": "join_all", "to": "final", "map": {"output": "response"}},
    ]

    result = execute(_plan(data), "hello", executor=FakeWorkflowExecutor())

    assert result.status == "blocked"
    assert result.error == "waiting_event:wait_event"
    assert any(step.node_id == "wait_event" and step.status == "blocked" for step in result.steps)
    assert not any(step.node_id == "join_all" for step in result.steps)
    assert not any(step.node_id == "final" for step in result.steps)


def test_parallel_direct_branches_run_concurrently() -> None:
    data = make_manifest()
    data["nodes"]["parallel"] = {
        "id": "parallel",
        "type": "parallel",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"output": {"type": "string"}},
    }
    data["nodes"]["agent_two"] = {
        "id": "agent_two",
        "type": "agent",
        "name": "test-agent-two",
        "model": "inherit",
        "instructions": {"type": "inline", "text": "You are helpful too."},
        "tools": [],
        "inputs": {"input": {"type": "string"}},
        "outputs": {"final_output": {"type": "string"}},
    }
    data["nodes"]["join_all"] = {
        "id": "join_all",
        "type": "join",
        "mode": "all",
        "inputs": {"left": {"type": "string"}, "right": {"type": "string"}},
        "outputs": {"output": {"type": "string"}, "merged": {"type": "structured"}},
    }
    data["edges"] = [
        {"id": "e_start_parallel", "from": "start", "to": "parallel", "map": {"msg": "input"}},
        {"id": "e_parallel_agent", "from": "parallel", "to": "agent", "map": {"output": "input"}},
        {
            "id": "e_parallel_agent_two",
            "from": "parallel",
            "to": "agent_two",
            "map": {"output": "input"},
        },
        {"id": "e_agent_join", "from": "agent", "to": "join_all", "map": {"final_output": "left"}},
        {
            "id": "e_agent_two_join",
            "from": "agent_two",
            "to": "join_all",
            "map": {"final_output": "right"},
        },
        {"id": "e_join_final", "from": "join_all", "to": "final", "map": {"output": "response"}},
    ]

    executor = _RecordingExecutor(barrier_parties=2, barrier_timeout=1.0)
    result = execute(_plan(data), "hello", executor=executor)

    assert result.status == "completed"
    assert result.output == "hello"
    assert executor.max_active == 2
    assert [step.node_id for step in result.steps] == [
        "start",
        "parallel",
        "agent",
        "agent_two",
        "join_all",
        "final",
    ]
    join_step = next(step for step in result.steps if step.node_id == "join_all")
    assert join_step.status == "ok"
    assert join_step.input_by_port == {"left": "hello", "right": "hello"}
    assert join_step.output_by_port == {
        "output": "hello",
        "merged": {"left": "hello", "right": "hello"},
    }


def test_parallel_join_any_fails_closed_when_a_parallel_sibling_errors() -> None:
    data = make_manifest()
    data["nodes"]["parallel"] = {
        "id": "parallel",
        "type": "parallel",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"output": {"type": "string"}},
    }
    data["nodes"]["good_python"] = {
        "id": "good_python",
        "type": "python_code",
        "code": 'return {"text": inputs.get("input", "")}',
        "timeout_seconds": 5,
        "inputs": {"input": {"type": "string"}},
        "outputs": {"text": {"type": "string"}},
    }
    data["nodes"]["bad_python"] = {
        "id": "bad_python",
        "type": "python_code",
        "code": 'raise ValueError("boom")',
        "timeout_seconds": 5,
        "inputs": {"input": {"type": "string"}},
        "outputs": {"text": {"type": "string"}},
    }
    data["nodes"]["join_any"] = {
        "id": "join_any",
        "type": "join",
        "mode": "any",
        "inputs": {"left": {"type": "string"}, "right": {"type": "string"}},
        "outputs": {"output": {"type": "string"}},
    }
    data["edges"] = [
        {"id": "e_start_parallel", "from": "start", "to": "parallel", "map": {"msg": "input"}},
        {
            "id": "e_parallel_good",
            "from": "parallel",
            "to": "good_python",
            "map": {"output": "input"},
        },
        {
            "id": "e_parallel_bad",
            "from": "parallel",
            "to": "bad_python",
            "map": {"output": "input"},
        },
        {
            "id": "e_good_join",
            "from": "good_python",
            "to": "join_any",
            "map": {"text": "left"},
        },
        {
            "id": "e_bad_join",
            "from": "bad_python",
            "to": "join_any",
            "map": {"text": "right"},
        },
        {"id": "e_join_final", "from": "join_any", "to": "final", "map": {"output": "response"}},
    ]

    result = execute(_plan(data), "hello", executor=FakeWorkflowExecutor())

    assert result.status == "error"
    assert "python_code node 'bad_python' failed" in (result.error or "")
    assert [step.node_id for step in result.steps] == ["start", "parallel"]
    assert not any(step.node_id == "join_any" for step in result.steps)
    assert not any(step.node_id == "final" for step in result.steps)


def test_parallel_join_all_fails_closed_when_a_parallel_sibling_errors() -> None:
    data = make_manifest()
    data["nodes"]["parallel"] = {
        "id": "parallel",
        "type": "parallel",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"output": {"type": "string"}},
    }
    data["nodes"]["good_python"] = {
        "id": "good_python",
        "type": "python_code",
        "code": 'return {"text": inputs.get("input", "")}',
        "timeout_seconds": 5,
        "inputs": {"input": {"type": "string"}},
        "outputs": {"text": {"type": "string"}},
    }
    data["nodes"]["bad_python"] = {
        "id": "bad_python",
        "type": "python_code",
        "code": 'raise ValueError("boom")',
        "timeout_seconds": 5,
        "inputs": {"input": {"type": "string"}},
        "outputs": {"text": {"type": "string"}},
    }
    data["nodes"]["join_all"] = {
        "id": "join_all",
        "type": "join",
        "mode": "all",
        "inputs": {"left": {"type": "string"}, "right": {"type": "string"}},
        "outputs": {"output": {"type": "string"}, "merged": {"type": "structured"}},
    }
    data["edges"] = [
        {"id": "e_start_parallel", "from": "start", "to": "parallel", "map": {"msg": "input"}},
        {
            "id": "e_parallel_good",
            "from": "parallel",
            "to": "good_python",
            "map": {"output": "input"},
        },
        {
            "id": "e_parallel_bad",
            "from": "parallel",
            "to": "bad_python",
            "map": {"output": "input"},
        },
        {
            "id": "e_good_join",
            "from": "good_python",
            "to": "join_all",
            "map": {"text": "left"},
        },
        {
            "id": "e_bad_join",
            "from": "bad_python",
            "to": "join_all",
            "map": {"text": "right"},
        },
        {"id": "e_join_final", "from": "join_all", "to": "final", "map": {"output": "response"}},
    ]

    result = execute(_plan(data), "hello", executor=FakeWorkflowExecutor())

    assert result.status == "error"
    assert "python_code node 'bad_python' failed" in (result.error or "")
    assert [step.node_id for step in result.steps] == ["start", "parallel"]
    assert not any(step.node_id == "join_all" for step in result.steps)
    assert not any(step.node_id == "final" for step in result.steps)


def test_handoff_executes() -> None:
    data = make_manifest()
    data["nodes"]["agent"]["handoffs"] = [{"target": "billing"}]
    data["nodes"]["billing"] = {
        "id": "billing",
        "type": "agent",
        "name": "billing-agent",
        "model": "inherit",
        "instructions": {"type": "inline", "text": "b"},
        "inputs": {"input": {"type": "string"}},
        "outputs": {"final_output": {"type": "string"}},
    }
    result = execute(_plan(data), "hi", executor=FakeWorkflowExecutor())
    assert "billing-agent" in result.output


def test_simple_handoff_executes_without_executor_selected_target() -> None:
    data = make_manifest()
    data["nodes"]["agent"]["handoffs"] = [{"target": "billing"}]
    data["nodes"]["billing"] = {
        "id": "billing",
        "type": "agent",
        "name": "billing-agent",
        "model": "inherit",
        "instructions": {"type": "inline", "text": "b"},
        "inputs": {"input": {"type": "string"}},
        "outputs": {"final_output": {"type": "string"}},
    }
    executor = _NoSelectionHandoffExecutor()

    result = execute(_plan(data), "hi", executor=executor)

    assert result.output == "[billing] processed: hi"
    assert executor.calls == ["agent", "billing"]
    assert executor.input_calls == ["hi", "hi"]
    agent_step = next(step for step in result.steps if step.node_id == "agent")
    assert agent_step.handoff_target == "billing"


def test_handoff_passes_upstream_history_to_target_agent() -> None:
    data = make_manifest()
    data["nodes"]["agent"]["handoffs"] = [{"target": "billing"}]
    data["nodes"]["billing"] = {
        "id": "billing",
        "type": "agent",
        "name": "billing-agent",
        "model": "inherit",
        "instructions": {"type": "inline", "text": "b"},
        "inputs": {
            "input": {"type": "string"},
            "history": {"type": "structured"},
        },
        "outputs": {
            "final_output": {"type": "string"},
            "history": {"type": "structured"},
        },
    }
    executor = _NoSelectionHandoffExecutor()

    execute(_plan(data), "hi", executor=executor)

    assert executor.history_calls == [
        [],
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "[agent] processed: hi"},
        ],
    ]


def test_multi_hop_handoff_executes_until_terminal_agent() -> None:
    data = make_manifest()
    data["nodes"]["agent"]["handoffs"] = [{"target": "billing"}]
    data["nodes"]["billing"] = {
        "id": "billing",
        "type": "agent",
        "name": "billing-agent",
        "model": "inherit",
        "instructions": {"type": "inline", "text": "b"},
        "handoffs": [{"target": "approvals"}],
        "inputs": {"input": {"type": "string"}},
        "outputs": {"final_output": {"type": "string"}},
    }
    data["nodes"]["approvals"] = {
        "id": "approvals",
        "type": "agent",
        "name": "approvals-agent",
        "model": "inherit",
        "instructions": {"type": "inline", "text": "c"},
        "inputs": {"input": {"type": "string"}},
        "outputs": {"final_output": {"type": "string"}},
    }
    executor = _NoSelectionHandoffExecutor()

    result = execute(_plan(data), "hi", executor=executor)

    assert result.output == "[approvals] processed: hi"
    assert executor.calls == ["agent", "billing", "approvals"]
    agent_step = next(step for step in result.steps if step.node_id == "agent")
    assert agent_step.handoff_target == "billing"


def test_handoff_cycle_is_bounded_by_runtime_hop_cap() -> None:
    data = make_manifest()
    data["nodes"]["agent"]["handoffs"] = [{"target": "billing"}]
    data["nodes"]["billing"] = {
        "id": "billing",
        "type": "agent",
        "name": "billing-agent",
        "model": "inherit",
        "instructions": {"type": "inline", "text": "b"},
        "handoffs": [{"target": "agent"}],
        "inputs": {"input": {"type": "string"}},
        "outputs": {"final_output": {"type": "string"}},
    }
    executor = _NoSelectionHandoffExecutor()

    result = execute(_plan(data), "hi", executor=executor)

    assert len(executor.calls) == 1 + workflow_runtime.MAX_AGENT_HANDOFF_HOPS
    assert executor.calls[:3] == ["agent", "billing", "agent"]
    assert result.output == f"[{executor.calls[-1]}] processed: hi"
    agent_step = next(step for step in result.steps if step.node_id == "agent")
    assert agent_step.handoff_target == "billing"


def test_executor_managed_handoffs_do_not_trigger_runtime_simple_fallback() -> None:
    data = make_manifest()
    data["nodes"]["agent"]["handoffs"] = [{"target": "billing"}]
    data["nodes"]["billing"] = {
        "id": "billing",
        "type": "agent",
        "name": "billing-agent",
        "model": "inherit",
        "instructions": {"type": "inline", "text": "b"},
        "inputs": {"input": {"type": "string"}},
        "outputs": {"final_output": {"type": "string"}},
    }
    executor = _ExecutorManagedHandoffExecutor()

    result = execute(_plan(data), "hi", executor=executor)

    assert result.output == "[agent] internally delegated: hi"
    assert executor.calls == ["agent"]
    assert executor.handoff_graphs == [["agent", "billing"]]
    agent_step = next(step for step in result.steps if step.node_id == "agent")
    assert agent_step.handoff_target is None


def test_complex_handoff_does_not_auto_execute_without_executor_target() -> None:
    data = make_manifest()
    data["nodes"]["agent"]["handoffs"] = [{"target": "billing", "condition": "billing_only"}]
    data["nodes"]["billing"] = {
        "id": "billing",
        "type": "agent",
        "name": "billing-agent",
        "model": "inherit",
        "instructions": {"type": "inline", "text": "b"},
        "inputs": {"input": {"type": "string"}},
        "outputs": {"final_output": {"type": "string"}},
    }
    executor = _NoSelectionHandoffExecutor()

    result = execute(_plan(data), "hi", executor=executor)

    assert result.output == "[agent] processed: hi"
    assert executor.calls == ["agent"]
    agent_step = next(step for step in result.steps if step.node_id == "agent")
    assert agent_step.handoff_target is None


def test_conditional_handoff_executes_without_executor_selected_target_when_enabled() -> None:
    data = make_manifest()
    data["nodes"]["agent"]["handoffs"] = [{"target": "billing", "condition": "input == 'hi'"}]
    data["nodes"]["billing"] = {
        "id": "billing",
        "type": "agent",
        "name": "billing-agent",
        "model": "inherit",
        "instructions": {"type": "inline", "text": "b"},
        "inputs": {"input": {"type": "string"}},
        "outputs": {"final_output": {"type": "string"}},
    }
    executor = _NoSelectionHandoffExecutor()

    result = execute(_plan(data), "hi", executor=executor)

    assert result.output == "[billing] processed: hi"
    assert executor.calls == ["agent", "billing"]
    assert executor.input_calls == ["hi", "hi"]
    agent_step = next(step for step in result.steps if step.node_id == "agent")
    assert agent_step.handoff_target == "billing"


def test_explicit_handoff_target_is_ignored_when_condition_is_disabled() -> None:
    data = make_manifest()
    data["nodes"]["agent"]["handoffs"] = [{"target": "billing", "condition": "input == 'refund'"}]
    data["nodes"]["billing"] = {
        "id": "billing",
        "type": "agent",
        "name": "billing-agent",
        "model": "inherit",
        "instructions": {"type": "inline", "text": "b"},
        "inputs": {"input": {"type": "string"}},
        "outputs": {"final_output": {"type": "string"}},
    }

    result = execute(_plan(data), "hi", executor=FakeWorkflowExecutor())

    assert result.output == "[test-agent] processed: hi"
    agent_step = next(step for step in result.steps if step.node_id == "agent")
    assert agent_step.handoff_target is None


def test_handoff_input_filter_rewrites_target_input_without_executor_selected_target() -> None:
    data = make_manifest()
    data["nodes"]["agent"]["handoffs"] = [
        {
            "target": "billing",
            "input_filter": "Billing summary for {{input}}\nAgent said: {{final_output}}",
        }
    ]
    data["nodes"]["billing"] = {
        "id": "billing",
        "type": "agent",
        "name": "billing-agent",
        "model": "inherit",
        "instructions": {"type": "inline", "text": "b"},
        "inputs": {"input": {"type": "string"}},
        "outputs": {"final_output": {"type": "string"}},
    }
    executor = _NoSelectionHandoffExecutor()

    result = execute(_plan(data), "refund", executor=executor)

    assert (
        result.output
        == "[billing] processed: Billing summary for refund\nAgent said: [agent] processed: refund"
    )
    assert executor.calls == ["agent", "billing"]
    assert executor.input_calls == [
        "refund",
        "Billing summary for refund\nAgent said: [agent] processed: refund",
    ]
    assert executor.history_calls == [[], []]


def test_guardrail_passes_when_tool_called() -> None:
    result = execute(_plan(make_support_manifest()), "refund?", executor=FakeWorkflowExecutor())
    assert result.status == "completed"
    assert all(g["passed"] for g in result.guardrail_results)


def test_guardrail_blocks_when_tool_skipped() -> None:
    # An un-grounded agent (skip_tools) makes a refund claim -> guardrail blocks.
    result = execute(
        _plan(make_support_manifest()),
        "what is your refund policy?",
        executor=FakeWorkflowExecutor(skip_tools=True),
    )
    assert result.status == "blocked"
    assert any(not g["passed"] for g in result.guardrail_results)


def test_mlflow_tags_present() -> None:
    plan = _plan(
        make_support_manifest(),
        workflow_version_id="wfv_1",
        workflow_alias="prod",
        compiler_version="0.1.0",
    )
    result = execute(plan, "refund?", executor=FakeWorkflowExecutor())
    assert result.tags["caliber.workflow_id"] == "support_wf"
    assert result.tags["caliber.workflow_version_id"] == "wfv_1"
    assert result.tags["caliber.manifest_hash"]
    assert result.tags["caliber.workflow_version"] == "7"


def test_session_id_recorded_in_tags() -> None:
    result = execute(
        _plan(make_manifest()), "hi", executor=FakeWorkflowExecutor(), session_id="sess-9"
    )
    assert result.tags["caliber.session_id"] == "sess-9"


def test_preview_mode_tag() -> None:
    result = execute(_plan(make_manifest()), "hi", executor=FakeWorkflowExecutor(), preview=True)
    assert result.tags.get("caliber.preview") == "true"


def test_runtime_error_is_normalized() -> None:
    class Boom(FakeWorkflowExecutor):
        def run_agent(self, *a, **k):  # type: ignore[override]
            raise RuntimeError("kaboom")

    result = execute(_plan(make_manifest()), "hi", executor=Boom())
    assert result.status == "error"
    assert "kaboom" in (result.error or "")


def test_context_tags_model_resolution_and_mlflow_tagging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}
    mlflow = types.ModuleType("mlflow")
    mlflow.active_run = lambda: object()  # type: ignore[attr-defined]
    mlflow.set_tags = lambda tags: captured.update(tags)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlflow", mlflow)

    with run_with_caliber_context(
        workflow_id="wf",
        workflow_version="3",
        entry_node_id="start",
        workflow_alias="prod",
        workflow_version_id="wfv_1",
        compiler_version="compiler",
        manifest_hash="hash",
        default_model_ref="gpt-test",
        session_id="sess",
        preview=True,
        extra_tags={"team": "ops"},
    ) as ctx:
        assert current_run_context() is ctx
        assert workflow_model("agent") == "gpt-test"
        assert workflow_model("agent", overrides={"agent": "gpt-override"}) == "gpt-override"
        assert run_tags(ctx, node_id="agent")["caliber.node_id"] == "agent"

    assert current_run_context() is None
    assert captured["caliber.workflow_alias"] == "prod"
    assert captured["caliber.preview"] == "true"
    assert captured["team"] == "ops"
    assert workflow_model("agent") == "CALIBER_WORKFLOW_DEFAULT_MODEL"

    mlflow.active_run = lambda: (_ for _ in ()).throw(RuntimeError("no run"))  # type: ignore[attr-defined]
    _set_mlflow_tags({"caliber.workflow_id": "wf"})


def test_execute_propagates_mlflow_manifest_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _FakeTracer:
        def trace_run(self, name: str, *, tags=None, experiment=None):
            captured["trace_run_name"] = name
            captured["trace_run_tags"] = dict(tags or {})
            captured["experiment"] = experiment

            @contextlib.contextmanager
            def _cm():
                yield types.SimpleNamespace(run_id="mlflow-run-1")

            return _cm()

        def span(self, name: str, *, span_type: str, attributes=None):
            captured["span_name"] = name
            captured["span_type"] = span_type
            captured.setdefault("span_attributes", dict(attributes or {}))

            @contextlib.contextmanager
            def _cm():
                yield types.SimpleNamespace(mlflow_trace_id="trace-1")

            return _cm()

    monkeypatch.setattr(workflow_runtime, "get_tracer", lambda: _FakeTracer())

    result = execute(
        _plan(
            make_support_manifest(
                mlflow={
                    "experiment_name": "caliber/support",
                    "trace_group_tags": {
                        "team": "ops",
                        "service": "support",
                    },
                },
            ),
        ),
        "refund?",
        executor=FakeWorkflowExecutor(),
    )

    assert captured["experiment"] == "caliber/support"
    assert captured["trace_run_tags"] == {
        "team": "ops",
        "service": "support",
        "caliber.workflow_id": "support_wf",
        "caliber.workflow_version": "7",
        "caliber.entry_node_id": "support_agent",
        "caliber.manifest_hash": result.tags["caliber.manifest_hash"],
    }
    assert captured["span_attributes"] == captured["trace_run_tags"]
    assert result.tags["team"] == "ops"
    assert result.tags["service"] == "support"
    assert result.mlflow_run_id == "mlflow-run-1"
    assert result.mlflow_trace_id == "trace-1"


def test_fake_executor_skips_missing_tool_and_retries_zero_arg_tool() -> None:
    agent = IRAgent(
        node_id="agent",
        node_type=NodeType.AGENT,
        name="agent",
        instructions=PromptRef(kind="mlflow_prompt", registry_name="agent"),
        tools=[
            IRToolBinding(
                local_name="missing",
                registry_ref="tool.missing.v1",
                version_constraint=">=1",
                requires_approval=False,
                side_effect_level="read",
                allow_in_preview=True,
                module_path="<in-memory>",
                callable_name="missing",
            ),
            IRToolBinding(
                local_name="zero",
                registry_ref="tool.zero.v1",
                version_constraint=">=1",
                requires_approval=False,
                side_effect_level="read",
                allow_in_preview=True,
                module_path="<in-memory>",
                callable_name="zero",
            ),
        ],
    )

    result = FakeWorkflowExecutor().run_agent(
        agent,
        "input",
        tool_callables={"zero": lambda: "called-without-args"},
        preview=False,
    )

    assert result.prompt_version == "resolved"
    assert result.tool_calls == [{"tool": "zero", "result": "called-without-args"}]


def test_guardrail_block_retry_records_retry_attempt() -> None:
    data = make_support_manifest()
    data["nodes"]["policy_guardrail"]["on_failure"] = "block_retry"
    data["nodes"]["policy_guardrail"]["max_retries"] = 1

    result = execute(
        _plan(data),
        "what is your refund policy?",
        executor=FakeWorkflowExecutor(skip_tools=True),
    )

    assert result.status == "blocked"
    assert [s.node_id for s in result.steps].count("policy_guardrail") == 2
    assert [s.node_id for s in result.steps].count("support_agent") == 2


def test_external_app_node_invokes_async_entrypoint_and_publishes_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = "workflow_external_app_test"
    module = types.ModuleType(module_name)

    async def handle(
        input=None,
        context=None,
        inputs=None,
        run_input="",
        session_id=None,
        preview=None,
        **_kwargs,
    ):
        return {
            "text": f"handled {input}",
            "ticket_id": (context or {}).get("ticket_id", "missing"),
            "echo": run_input,
            "metadata": {
                "handled_by": "async-handle",
                "preview": preview,
                "session_id": session_id,
                "input_count": len(inputs or {}),
            },
        }

    module.handle = handle
    monkeypatch.setitem(sys.modules, module_name, module)

    string = IRType("string")
    structured = IRType("structured")
    node = IRExternalApp(
        node_id="external",
        node_type=NodeType.EXTERNAL_APP,
        inputs={"input": string, "context": structured},
        outputs={
            "text": string,
            "ticket_id": string,
            "result": structured,
            "metadata": structured,
        },
        entrypoint=f"{module_name}:handle",
    )
    ir = IRWorkflow(
        workflow_id="wf-external",
        version="1",
        nodes={"external": node},
        edges=[],
        entry_node_id="external",
        output_node_id="external",
    )
    port_values: dict[tuple[str, str], object] = {}

    with run_with_caliber_context(
        workflow_id="wf-external",
        workflow_version="1",
        entry_node_id="external",
        session_id="SESSION-1",
        preview=True,
    ):
        _, step = _run_node(
            node,
            ir,
            RuntimePlan(
                ir=ir,
                resolver=InMemoryToolResolver([]),
                external_app_entrypoint_allowlist="workflow_external_*",
            ),
            executor=FakeWorkflowExecutor(),
            preview=True,
            inputs={"input": "approve", "context": {"ticket_id": "T-100"}},
            run_input="fallback input",
            port_values=port_values,
            guardrail_results=[],
        )

    assert step.status == "ok"
    assert step.output == "handled approve"
    assert "workflow_external_app_test:handle" in step.detail
    assert port_values[("external", "text")] == "handled approve"
    assert port_values[("external", "ticket_id")] == "T-100"
    assert port_values[("external", "result")] == {
        "text": "handled approve",
        "ticket_id": "T-100",
        "echo": "fallback input",
        "metadata": {
            "handled_by": "async-handle",
            "preview": True,
            "session_id": "SESSION-1",
            "input_count": 2,
        },
    }
    metadata = port_values[("external", "metadata")]
    assert isinstance(metadata, dict)
    assert metadata["handled_by"] == "async-handle"
    assert metadata["preview"] is True
    assert metadata["session_id"] == "SESSION-1"
    assert metadata["input_count"] == 2
    assert metadata["entrypoint"] == "workflow_external_app_test:handle"
    assert isinstance(metadata["duration_ms"], float)
    assert metadata["duration_ms"] >= 0


def test_runtime_human_passthrough_then_external_app_executes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = "workflow_external_router_app"
    module = types.ModuleType(module_name)

    def handle(input=None, **_kwargs):
        return {"result": f"ticketing handled: {input}"}

    module.handle = handle
    monkeypatch.setitem(sys.modules, module_name, module)

    string = IRType("string")
    nodes = {
        "start": IRNode(
            node_id="start",
            node_type=NodeType.START,
            outputs={"msg": string},
        ),
        "approval": IRHumanApproval(
            node_id="approval",
            node_type=NodeType.HUMAN_APPROVAL,
            inputs={"input": string},
            outputs={"approved": string},
        ),
        "external": IRExternalApp(
            node_id="external",
            node_type=NodeType.EXTERNAL_APP,
            inputs={"input": string},
            outputs={"result": string},
            entrypoint=f"{module_name}:handle",
        ),
        "router": IRRouter(
            node_id="router",
            node_type=NodeType.ROUTER,
            inputs={"input": string},
            outputs={"route": string},
            branches=[
                IRRouterBranch(condition={"op": "contains", "value": "ticketing"}, to="final_a"),
                IRRouterBranch(condition=None, to="final_b"),
            ],
        ),
        "final_a": IRNode(
            node_id="final_a",
            node_type=NodeType.OUTPUT,
            inputs={"response": string},
        ),
        "final_b": IRNode(
            node_id="final_b",
            node_type=NodeType.OUTPUT,
            inputs={"response": string},
        ),
    }
    edges = [
        IREdge("e1", "start", "msg", "approval", "input", string),
        IREdge("e2", "approval", "approved", "external", "input", string),
        IREdge("e3", "external", "result", "router", "input", string),
        IREdge("e4", "router", "route", "final_a", "response", string),
        IREdge("e5", "router", "route", "final_b", "response", string),
    ]
    ir = IRWorkflow(
        workflow_id="wf",
        version="1",
        nodes=nodes,
        edges=edges,
        entry_node_id="start",
        output_node_id="final_a",
    )

    result = execute(
        RuntimePlan(
            ir=ir,
            resolver=InMemoryToolResolver([]),
            external_app_entrypoint_allowlist="workflow_external_*",
        ),
        "approve",
        executor=FakeWorkflowExecutor(),
    )

    assert result.status == "completed"
    assert result.output == "ticketing handled: approve"
    step_ids = {step.node_id for step in result.steps}
    assert "approval" in step_ids  # approval ran before the external node
    assert "router" in step_ids
    external_step = next(step for step in result.steps if step.node_id == "external")
    assert f"{module_name}:handle" in external_step.detail


def test_router_without_branches_fails_closed() -> None:
    data = make_manifest()
    data["nodes"]["router"] = {
        "id": "router",
        "type": "router",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"route": {"type": "string"}},
        "branches": [],
    }
    data["edges"] = [
        {"id": "e_start_router", "from": "start", "to": "router", "map": {"msg": "input"}},
        {"id": "e_router_final", "from": "router", "to": "final", "map": {"route": "response"}},
    ]

    result = execute(_plan(data), "refund", executor=FakeWorkflowExecutor())

    assert result.status == "error"
    assert "requires at least one branch" in (result.error or "")


def test_router_routes_to_matching_branch_at_workflow_runtime() -> None:
    string = IRType("string")
    nodes = {
        "start": IRNode(
            node_id="start",
            node_type=NodeType.START,
            outputs={"msg": string},
        ),
        "router": IRRouter(
            node_id="router",
            node_type=NodeType.ROUTER,
            inputs={"input": string},
            outputs={"route": string},
            branches=[
                IRRouterBranch(condition={"op": "contains", "value": "refund"}, to="final_a"),
                IRRouterBranch(condition=None, to="final_b"),
            ],
        ),
        "final_a": IRNode(
            node_id="final_a",
            node_type=NodeType.OUTPUT,
            inputs={"response": string},
        ),
        "final_b": IRNode(
            node_id="final_b",
            node_type=NodeType.OUTPUT,
            inputs={"response": string},
        ),
    }
    edges = [
        IREdge("e1", "start", "msg", "router", "input", string),
        IREdge("e2", "router", "route", "final_a", "response", string),
        IREdge("e3", "router", "route", "final_b", "response", string),
    ]
    ir = IRWorkflow(
        workflow_id="wf-router-branch",
        version="1",
        nodes=nodes,
        edges=edges,
        entry_node_id="start",
        output_node_id="final_a",
    )

    result = execute(
        RuntimePlan(ir=ir, resolver=InMemoryToolResolver([])),
        "refund request",
        executor=FakeWorkflowExecutor(),
    )

    assert result.status == "completed"
    assert result.output == "refund request"
    step_ids = [step.node_id for step in result.steps]
    assert "router" in step_ids
    assert "final_a" in step_ids
    assert "final_b" not in step_ids


def test_router_routes_to_fallback_branch_at_workflow_runtime() -> None:
    data = make_manifest()
    data["nodes"]["router"] = {
        "id": "router",
        "type": "router",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"route": {"type": "string"}},
        "branches": [
            {"condition": {"op": "contains", "value": "refund"}, "to": "agent"},
            {"to": "final"},
        ],
    }
    data["edges"] = [
        {"id": "e_start_router", "from": "start", "to": "router", "map": {"msg": "input"}},
        {"id": "e_router_agent", "from": "router", "to": "agent", "map": {"route": "input"}},
        {"id": "e_router_final", "from": "router", "to": "final", "map": {"route": "response"}},
        {
            "id": "e_agent_final",
            "from": "agent",
            "to": "final",
            "map": {"final_output": "response"},
        },
    ]
    result = execute(_plan(data), "shipping update", executor=FakeWorkflowExecutor())

    assert result.status == "completed"
    assert result.output == "shipping update"
    step_ids = [step.node_id for step in result.steps]
    assert "router" in step_ids
    assert "final" in step_ids
    assert "agent" not in step_ids


def test_external_app_node_reports_invalid_entrypoint() -> None:
    string = IRType("string")
    node = IRExternalApp(
        node_id="external",
        node_type=NodeType.EXTERNAL_APP,
        inputs={"input": string},
        outputs={"text": string},
        entrypoint="not-a-valid-entrypoint",
    )
    ir = IRWorkflow(
        workflow_id="wf-external-invalid",
        version="1",
        nodes={"external": node},
        edges=[],
        entry_node_id="external",
        output_node_id="external",
    )

    with pytest.raises(ToolExecutionError, match="package.module:callable"):
        _run_node(
            node,
            ir,
            RuntimePlan(
                ir=ir,
                resolver=InMemoryToolResolver([]),
                external_app_entrypoint_allowlist="workflow_external_*",
            ),
            executor=FakeWorkflowExecutor(),
            preview=False,
            inputs={"input": "approve"},
            run_input="approve",
            port_values={},
            guardrail_results=[],
        )


def test_external_app_node_reports_timeout_at_workflow_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = "workflow_external_timeout_runtime"
    module = types.ModuleType(module_name)

    def slow_handler(payload: dict[str, object]) -> dict[str, str]:
        time.sleep(0.02)
        return {"text": str(payload["input"])}

    module.handle = slow_handler
    monkeypatch.setitem(sys.modules, module_name, module)

    data = make_manifest()
    data["nodes"]["external"] = {
        "id": "external",
        "type": "external_app",
        "entrypoint": f"{module_name}:handle",
        "inputs": {"input": {"type": "string"}},
        "outputs": {
            "text": {"type": "string"},
            "result": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
        "execution_policy": {"timeout_seconds": 0.001},
    }
    data["edges"] = [
        {"id": "e_start_external", "from": "start", "to": "external", "map": {"msg": "input"}},
        {"id": "e_external_final", "from": "external", "to": "final", "map": {"text": "response"}},
    ]

    result = execute(_plan(data), "approve", executor=FakeWorkflowExecutor())

    assert result.status == "error"
    assert result.error is not None
    assert "ToolExecutionError: external_app node 'external' timed out after 0.001s" in result.error


def test_runtime_falls_back_to_last_agent_output_without_output_node() -> None:
    string = IRType("string")
    agent = IRAgent(
        node_id="agent",
        node_type=NodeType.AGENT,
        name="solo",
        inputs={"input": string},
        outputs={"final_output": string},
    )
    ir = IRWorkflow(
        workflow_id="wf",
        version="1",
        nodes={
            "start": IRNode("start", NodeType.START, outputs={"msg": string}),
            "agent": agent,
        },
        edges=[IREdge("e1", "start", "msg", "agent", "input", string)],
        entry_node_id="start",
        output_node_id="",
    )

    result = execute(
        RuntimePlan(ir=ir, resolver=InMemoryToolResolver([])),
        "hello",
        executor=FakeWorkflowExecutor(),
    )

    assert result.status == "completed"
    assert result.output == "[solo] processed: hello"


def test_resilient_callable_retries_failures_and_times_out() -> None:
    attempts = 0
    retry_binding = IRToolBinding(
        local_name="flaky",
        registry_ref="tool.flaky.v1",
        version_constraint=">=1",
        requires_approval=False,
        side_effect_level="read",
        allow_in_preview=True,
        module_path="<in-memory>",
        callable_name="flaky",
        max_retries=1,
    )

    def flaky() -> None:
        nonlocal attempts
        attempts += 1
        raise ValueError("still broken")

    with pytest.raises(ToolExecutionError, match="failed after 2 attempt"):
        _resilient_callable(retry_binding, flaky)()
    assert attempts == 2

    timeout_binding = IRToolBinding(
        local_name="slow",
        registry_ref="tool.slow.v1",
        version_constraint=">=1",
        requires_approval=False,
        side_effect_level="read",
        allow_in_preview=True,
        module_path="<in-memory>",
        callable_name="slow",
        timeout_seconds=0.001,
    )

    def slow() -> str:
        time.sleep(0.01)
        return "late"

    with pytest.raises(ToolExecutionError, match="timed out"):
        _resilient_callable(timeout_binding, slow)()


def test_bind_returns_none_for_in_memory_tools() -> None:
    """An ``<in-memory>`` binding has no module to load, so it binds to nothing.

    Previously this test also covered an *unimportable* module path and expected the same
    ``None``. That contract changed when registered tools moved out of process (C8):
    deciding at bind time whether ``caliber.nope`` imports would mean importing it in the
    control plane, which is precisely what the sandbox exists to prevent. The unimportable
    case is now asserted separately, at call time.
    """
    string = IRType("string")
    agent = IRAgent(
        node_id="agent",
        node_type=NodeType.AGENT,
        name="agent",
        tools=[
            IRToolBinding(
                local_name="preview_only",
                registry_ref="tool.preview.v1",
                version_constraint=">=1",
                requires_approval=False,
                side_effect_level="read",
                allow_in_preview=True,
                module_path="<in-memory>",
                callable_name="preview_only",
            ),
        ],
        inputs={"input": string},
        outputs={"final_output": string},
    )
    plan = RuntimePlan(
        ir=IRWorkflow(
            workflow_id="wf",
            version="1",
            nodes={
                "start": IRNode("start", NodeType.START, outputs={"msg": string}),
                "agent": agent,
            },
            edges=[IREdge("e1", "start", "msg", "agent", "input", string)],
            entry_node_id="start",
            output_node_id="",
        ),
        resolver=InMemoryToolResolver([]),
    )

    result = execute(plan, "hello", executor=FakeWorkflowExecutor())

    assert result.status == "completed"
    assert result.steps[0].tool_calls == []


def test_an_unimportable_registered_module_fails_at_call_time_not_bind_time() -> None:
    """C8 moves the import into the subprocess, which moves *when* the failure appears.

    A missing module used to be detected by ``_bind`` — via ``importlib.import_module`` in
    the control plane — and the binding was dropped. That check cannot survive the sandbox:
    knowing whether the module imports requires importing it, in this process, which is the
    exact exposure the boundary removes.

    So the binding now succeeds optimistically and the import failure surfaces from the
    child on invocation. What matters is that it degrades *legibly* rather than silently:
    the run is not reported as completed, and the error names the missing module.
    """
    string = IRType("string")
    agent = IRAgent(
        node_id="agent",
        node_type=NodeType.AGENT,
        name="agent",
        tools=[
            IRToolBinding(
                local_name="missing_module",
                registry_ref="tool.missing.v1",
                version_constraint=">=1",
                requires_approval=False,
                side_effect_level="read",
                allow_in_preview=True,
                module_path="caliber.nope",
                callable_name="fn",
            ),
        ],
        inputs={"input": string},
        outputs={"final_output": string},
    )
    plan = RuntimePlan(
        ir=IRWorkflow(
            workflow_id="wf",
            version="1",
            nodes={
                "start": IRNode("start", NodeType.START, outputs={"msg": string}),
                "agent": agent,
            },
            edges=[IREdge("e1", "start", "msg", "agent", "input", string)],
            entry_node_id="start",
            output_node_id="",
        ),
        resolver=InMemoryToolResolver([]),
    )

    result = execute(plan, "hello", executor=FakeWorkflowExecutor())

    # Not "completed": a tool the author declared could not be run, and saying otherwise
    # would hide a broken registration behind a green run.
    assert result.status == "error"
    assert "caliber.nope" in (result.error or "")


def test_run_node_guardrail_warn_output_and_note_branches() -> None:
    string = IRType("string")
    plan = RuntimePlan(
        ir=IRWorkflow(
            workflow_id="wf",
            version="1",
            nodes={},
            edges=[],
            entry_node_id="start",
            output_node_id="",
        ),
        resolver=InMemoryToolResolver([]),
    )
    port_values: dict[tuple[str, str], object] = {}
    guardrail_results: list[dict[str, object]] = []
    guardrail = IRGuardrail(
        node_id="guard",
        node_type=NodeType.GUARDRAIL,
        inputs={"response": string},
        outputs={},
        checks=[IRGuardrailCheck("non_empty_output")],
        on_failure="warn",
    )

    _, guard_step = _run_node(
        guardrail,
        plan.ir,
        plan,
        executor=FakeWorkflowExecutor(),
        preview=False,
        inputs={"response": ""},
        run_input="fallback",
        port_values=port_values,
        guardrail_results=guardrail_results,
    )
    assert guard_step.status == "ok"
    assert guard_step.detail == "response is empty"
    assert port_values[("guard", "passthrough")] == ""

    _, output_step = _run_node(
        IRNode("out", NodeType.OUTPUT, inputs={"response": string}),
        plan.ir,
        plan,
        executor=FakeWorkflowExecutor(),
        preview=False,
        inputs={"response": 123},
        run_input="fallback",
        port_values={},
        guardrail_results=[],
    )
    assert output_step.output == ""

    _, note_step = _run_node(
        IRNode("note", NodeType.NOTE),
        plan.ir,
        plan,
        executor=FakeWorkflowExecutor(),
        preview=False,
        inputs={},
        run_input="fallback",
        port_values={},
        guardrail_results=[],
    )
    assert note_step.status == "ok"


def test_runtime_helper_fallback_branches() -> None:
    assert _select_input({"x": 1, "y": "chosen"}, "run") == "chosen"
    assert _select_input({"x": 1}, "run") == "run"
    assert _first_str({"x": 1}) is None
    assert _find_recent_tool_calls({("agent", "other"): []}) == []
    assert _condition_matches({"op": "equals", "value": "yes"}, "yes")
    assert _condition_matches({"op": "mentions", "value": "refund"}, "refund policy")
    assert _condition_matches({"op": "regex", "value": "refund.*"}, "refund policy")
    assert _condition_matches({"contains": "refund", "field": "input"}, {"input": "Refund policy"})
    assert not _condition_matches({"op": "regex", "value": "(["}, "refund policy")
    assert not _condition_matches({}, "refund policy")
    assert not _condition_matches({"field": "input"}, {"input": "refund policy"})
    assert not _condition_matches({"not": "invalid"}, {"input": "refund policy"})
    assert not _condition_matches({"all": [123]}, {"input": "refund policy"})
    assert _condition_matches(
        {"all": [{"op": "contains", "field": "input", "value": "refund"}]},
        {"input": "refund policy"},
    )

    router = IRRouter(
        node_id="router",
        node_type=NodeType.ROUTER,
        branches=[
            IRRouterBranch({"op": "equals", "value": "no"}, "no"),
            IRRouterBranch(None, "fallback"),
        ],
    )
    assert _route(router, {"input": "maybe"}) == "fallback"

    malformed_router = IRRouter(
        node_id="router_bad",
        node_type=NodeType.ROUTER,
        branches=[
            IRRouterBranch({}, "bad"),
            IRRouterBranch(None, "fallback"),
        ],
    )
    assert _route(malformed_router, {"input": "maybe"}) == "fallback"
    assert _route(IRRouter("empty", NodeType.ROUTER), {"input": "maybe"}) is None

    ctx = CaliberRunContext("wf", "1", "start")
    assert "caliber.workflow_alias" not in run_tags(ctx)


def test_is_reasoning_model() -> None:
    """Reasoning models (o-series, gpt-5*) must be detected so the executor omits
    the unsupported custom temperature; plain chat models keep it."""
    from caliber.workflows.runtime import _is_reasoning_model

    for model in ("gpt-5.2", "gpt-5-mini", "gpt-5", "o1", "o3-mini", "O4", "GPT-5.2"):
        assert _is_reasoning_model(model) is True, model
    for model in ("gpt-4o", "gpt-4o-mini", "gpt-4.1", "claude-sonnet-4", "", "o"):
        assert _is_reasoning_model(model) is False, model


def test_execute_blocks_when_runtime_human_approval_has_not_been_granted() -> None:
    string = IRType("string")
    plan = RuntimePlan(
        ir=IRWorkflow(
            workflow_id="wf-approval-blocked",
            version="1",
            nodes={
                "start": IRNode("start", NodeType.START, outputs={"msg": string}),
                "approval": IRHumanApproval(
                    node_id="approval",
                    node_type=NodeType.HUMAN_APPROVAL,
                    inputs={"input": string},
                    outputs={"approved": string},
                ),
                "final": IRNode(
                    "final",
                    NodeType.OUTPUT,
                    inputs={"response": string},
                ),
            },
            edges=[
                IREdge("e1", "start", "msg", "approval", "input", string),
                IREdge("e2", "approval", "approved", "final", "response", string),
            ],
            entry_node_id="start",
            output_node_id="final",
        ),
        resolver=InMemoryToolResolver([]),
    )

    result = execute(
        plan,
        "needs human approval",
        executor=FakeWorkflowExecutor(),
        runtime_approvals_enabled=True,
    )

    assert result.status == "blocked"
    assert result.error == "waiting_approval:approval"
    assert [step.node_id for step in result.steps] == ["start", "approval"]
    approval_step = next(step for step in result.steps if step.node_id == "approval")
    assert approval_step.status == "blocked"
    assert approval_step.output == "needs human approval"
    assert approval_step.input_by_port == {"input": "needs human approval"}
    assert approval_step.output_by_port == {}


def test_execute_continues_when_runtime_human_approval_node_is_preapproved() -> None:
    string = IRType("string")
    plan = RuntimePlan(
        ir=IRWorkflow(
            workflow_id="wf-approval-approved",
            version="1",
            nodes={
                "start": IRNode("start", NodeType.START, outputs={"msg": string}),
                "approval": IRHumanApproval(
                    node_id="approval",
                    node_type=NodeType.HUMAN_APPROVAL,
                    inputs={"input": string},
                    outputs={"approved": string},
                ),
                "final": IRNode(
                    "final",
                    NodeType.OUTPUT,
                    inputs={"response": string},
                ),
            },
            edges=[
                IREdge("e1", "start", "msg", "approval", "input", string),
                IREdge("e2", "approval", "approved", "final", "response", string),
            ],
            entry_node_id="start",
            output_node_id="final",
        ),
        resolver=InMemoryToolResolver([]),
    )

    result = execute(
        plan,
        "approved by operator",
        executor=FakeWorkflowExecutor(),
        runtime_approvals_enabled=True,
        approved_human_approval_nodes={"approval"},
    )

    assert result.status == "completed"
    assert result.output == "approved by operator"
    approval_step = next(step for step in result.steps if step.node_id == "approval")
    final_step = next(step for step in result.steps if step.node_id == "final")
    assert approval_step.status == "ok"
    assert approval_step.detail == "approval required (pass-through in MVP)"
    assert approval_step.input_by_port == {"input": "approved by operator"}
    assert approval_step.output_by_port == {"approved": "approved by operator"}
    assert final_step.input_by_port == {"response": "approved by operator"}


def test_fake_executor_marks_approval_required_tools_as_gated_without_running_them() -> None:
    string = IRType("string")
    called = False

    def forbidden_tool(_input: str = "") -> str:
        nonlocal called
        called = True
        raise AssertionError("approval-required tool should not execute autonomously")

    agent = IRAgent(
        node_id="agent",
        node_type=NodeType.AGENT,
        name="agent",
        instructions=PromptRef(kind="inline", inline_text="Only use approved tools."),
        tools=[
            IRToolBinding(
                local_name="delete_record",
                registry_ref="tool.delete_record.v1",
                version_constraint=">=1",
                requires_approval=True,
                side_effect_level="write",
                allow_in_preview=True,
                module_path="<in-memory>",
                callable_name="delete_record",
            )
        ],
        inputs={"input": string},
        outputs={"final_output": string},
    )

    result = FakeWorkflowExecutor().run_agent(
        agent,
        "remove ticket T-100",
        tool_callables={"delete_record": forbidden_tool},
        preview=False,
    )

    assert called is False
    assert result.tool_calls == [
        {
            "tool": "delete_record",
            "result": {
                "_gated": True,
                "tool": "delete_record",
                "reason": "tool requires approval; not executed in an autonomous run",
            },
        }
    ]
    assert result.final_output == "[agent] processed: remove ticket T-100 (used 1 tool(s))"


def test_execute_model_tool_call_gates_approval_required_tool_without_invoking_it() -> None:
    called = False

    def forbidden_tool(**_kwargs: object) -> str:
        nonlocal called
        called = True
        raise AssertionError("model-selected approval tool should not be invoked")

    binding = IRToolBinding(
        local_name="delete_record",
        registry_ref="tool.delete_record.v1",
        version_constraint=">=1",
        requires_approval=True,
        side_effect_level="write",
        allow_in_preview=True,
        module_path="<in-memory>",
        callable_name="delete_record",
    )

    result, result_text = _execute_model_tool_call(
        name="delete_record",
        arguments={"ticket_id": "T-100"},
        tool_callables={"delete_record": forbidden_tool},
        bindings={"delete_record": binding},
        fallback_input="remove ticket T-100",
    )

    assert called is False
    assert result == {
        "_gated": True,
        "tool": "delete_record",
        "reason": "tool requires approval; not executed in an autonomous run",
    }
    assert "requires approval" in result_text


def test_execute_model_tool_call_returns_soft_error_markers_for_unknown_and_failing_tools() -> None:
    missing_result, missing_text = _execute_model_tool_call(
        name="missing_tool",
        arguments={},
        tool_callables={},
        bindings={},
        fallback_input="fallback",
    )

    assert missing_result == {"_error": "unknown tool 'missing_tool'"}
    assert missing_text == '{"_error": "unknown tool \'missing_tool\'"}'

    calls = 0

    def failing_tool(input: str) -> str:
        nonlocal calls
        calls += 1
        raise RuntimeError(f"boom: {input}")

    binding = IRToolBinding(
        local_name="failing_tool",
        registry_ref="tool.failing.v1",
        version_constraint=">=1",
        requires_approval=False,
        side_effect_level="read",
        allow_in_preview=True,
        module_path="<in-memory>",
        callable_name="failing_tool",
    )

    failed_result, failed_text = _execute_model_tool_call(
        name="failing_tool",
        arguments={"input": "payload"},
        tool_callables={"failing_tool": failing_tool},
        bindings={"failing_tool": binding},
        fallback_input="fallback",
    )

    assert calls == 1
    assert failed_result == {
        "_error": "RuntimeError: boom: payload",
        "tool": "failing_tool",
    }
    assert failed_text == '{"_error": "RuntimeError: boom: payload", "tool": "failing_tool"}'


def test_resolve_external_app_entrypoint_supports_nested_attributes() -> None:
    module_name = "workflow_external_nested_entrypoint"
    module = types.ModuleType(module_name)

    class Handlers:
        @staticmethod
        def sync_handler(payload: dict[str, object]) -> str:
            return str(payload["input"])

    module.handlers = Handlers()
    sys.modules[module_name] = module

    try:
        fn, resolved = _resolve_external_app_entrypoint(
            f"{module_name}:handlers.sync_handler",
            allowlist=f"{module_name}:handlers.sync_handler",
        )
    finally:
        sys.modules.pop(module_name, None)

    assert resolved == f"{module_name}:handlers.sync_handler"
    assert fn({"input": "ticket"}) == "ticket"


@pytest.mark.parametrize(
    ("entrypoint", "message"),
    [
        ("", "must not be empty"),
        ("missing-module:handle", "could not import external_app module"),
        ("json:missing", "could not resolve attribute"),
        ("json:encoder", "resolved to a non-callable object"),
    ],
)
def test_resolve_external_app_entrypoint_reports_configuration_errors(
    entrypoint: str,
    message: str,
) -> None:
    # Allowlisted, so each case exercises its own configuration error rather than
    # the allowlist refusal (which has its own tests below).
    with pytest.raises(ToolExecutionError, match=message):
        _resolve_external_app_entrypoint(entrypoint, allowlist=entrypoint or "x:y")


# ── external_app entrypoint allowlist (C8) ─────────────────────────────────


def test_an_external_app_entrypoint_is_refused_without_an_allowlist() -> None:
    """Regression (C8): an operator could type any ``package.module:callable`` into
    the Inspector and CALIBER imported and invoked it in the control-plane process.
    Every other execution surface shipped an allowlist; this one did not."""
    with pytest.raises(ToolExecutionError, match="EXTERNAL_APP_ENTRYPOINT_ALLOWLIST"):
        _resolve_external_app_entrypoint("json:dumps")
    with pytest.raises(ToolExecutionError, match="EXTERNAL_APP_ENTRYPOINT_ALLOWLIST"):
        _resolve_external_app_entrypoint("json:dumps", allowlist="other.module:handler")


def test_the_allowlist_accepts_an_exact_entry_in_either_spelling() -> None:
    """``a.b.c`` and ``a.b:c`` name the same callable and must compare equal."""
    fn, resolved = _resolve_external_app_entrypoint("json:dumps", allowlist="json:dumps")
    assert callable(fn)
    assert resolved == "json:dumps"
    fn2, _ = _resolve_external_app_entrypoint("json.dumps", allowlist="json:dumps")
    assert fn2 is fn


def test_the_allowlist_supports_a_module_prefix_wildcard() -> None:
    """A team with many first-party entrypoints needs a prefix, or the allowlist
    becomes unusable and gets set to something permissive."""
    fn, _ = _resolve_external_app_entrypoint("json:dumps", allowlist="json:*")
    assert callable(fn)


def test_a_bare_wildcard_does_not_allow_everything() -> None:
    """``*`` would silently restore the original defect."""
    with pytest.raises(ToolExecutionError, match="EXTERNAL_APP_ENTRYPOINT_ALLOWLIST"):
        _resolve_external_app_entrypoint("json:dumps", allowlist="*")


def test_allowlist_matching_helper_is_directly_testable() -> None:
    from caliber.workflows.runtime import external_app_entrypoint_allowed

    assert external_app_entrypoint_allowed("a.b:c", "a.b:c") is True
    assert external_app_entrypoint_allowed("a.b.c", "a.b:c") is True
    assert external_app_entrypoint_allowed("a.b:c", " a.b:c , x:y ") is True
    assert external_app_entrypoint_allowed("a.b:c", "a.b:d") is False
    assert external_app_entrypoint_allowed("a.b:c", "") is False
    assert external_app_entrypoint_allowed("", "a.b:c") is False
    assert external_app_entrypoint_allowed("a.b:c", "a.*") is True
    assert external_app_entrypoint_allowed("zzz:c", "a.*") is False


def test_invoke_external_app_callable_adapts_supported_function_shapes() -> None:
    payload = {
        "input": "ticket text",
        "workflow_id": "wf-ext",
        "node_id": "external",
        "preview": True,
    }

    def via_payload(payload: dict[str, object]) -> str:
        return str(payload["workflow_id"])

    def via_kwargs(**kwargs: object) -> str:
        return f"{kwargs['node_id']}:{kwargs['input']}"

    def via_no_args() -> str:
        return "no-args"

    def via_keywords(*, input: str, preview: bool) -> str:
        return f"{input}:{preview}"

    assert _invoke_external_app_callable(via_payload, payload) == "wf-ext"
    assert _invoke_external_app_callable(via_kwargs, payload) == "external:ticket text"
    assert _invoke_external_app_callable(via_no_args, payload) == "no-args"
    assert _invoke_external_app_callable(via_keywords, payload) == "ticket text:True"


def test_run_external_app_entrypoint_enforces_timeout() -> None:
    module_name = "workflow_external_timeout_entrypoint"
    module = types.ModuleType(module_name)

    def slow_handler(payload: dict[str, object]) -> str:
        time.sleep(0.02)
        return str(payload["input"])

    module.handle = slow_handler
    sys.modules[module_name] = module

    node = IRExternalApp(
        node_id="external",
        node_type=NodeType.EXTERNAL_APP,
        inputs={"input": IRType("string")},
        outputs={"text": IRType("string")},
        entrypoint=f"{module_name}:handle",
        execution_policy=IRExecutionPolicy(timeout_seconds=0.001),
    )
    ir = IRWorkflow(
        workflow_id="wf-external-timeout",
        version="1",
        nodes={"external": node},
        edges=[],
        entry_node_id="external",
        output_node_id="external",
    )

    try:
        with pytest.raises(ToolExecutionError, match="timed out after 0.001s"):
            _run_external_app_entrypoint(
                node=node,
                ir=ir,
                nid="external",
                inputs={"input": "approve"},
                run_input="approve",
                preview=False,
                entrypoint_allowlist=f"{module_name}:handle",
            )
    finally:
        sys.modules.pop(module_name, None)


def test_resilient_callable_timeout_returns_control_without_joining_the_orphan() -> None:
    """The timeout must bound the *wall clock*, not just the error message.

    The pool was entered with ``with``, whose ``__exit__`` calls
    ``shutdown(wait=True)`` and joins the thread the timeout just abandoned. So a
    tool declaring a 0.2s timeout and sleeping 3s raised "timed out after 0.2s"
    at t=3.0 — the deadline appeared in the exception and nowhere in reality,
    which is exactly what makes a cancellation or SLO claim untrue.

    The existing timeout test above uses a 10ms body, too short to tell a
    blocking shutdown from a non-blocking one; this one is deliberately far
    apart so the assertion cannot pass by accident.
    """
    released = threading.Event()
    binding = IRToolBinding(
        local_name="slow",
        registry_ref="tool.slow.v1",
        version_constraint=">=1",
        requires_approval=False,
        side_effect_level="read",
        allow_in_preview=True,
        module_path="<in-memory>",
        callable_name="slow",
        timeout_seconds=0.2,
    )

    def slow() -> str:
        # Long enough that joining it would be unmistakable in the timing below.
        time.sleep(3.0)
        released.set()
        return "late"

    started = time.monotonic()
    with pytest.raises(ToolExecutionError, match="timed out"):
        _resilient_callable(binding, slow)()
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, f"timeout joined the abandoned worker: returned after {elapsed:.2f}s"
    # The orphan is still running — that is the honest state. Python cannot kill
    # it; the contract is only that the workflow stops waiting on it.
    assert not released.is_set()
