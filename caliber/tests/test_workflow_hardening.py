"""Tests for the Step-4 extension set (gap-analysis hardening).

Covers: codegen injection escaping (A1), node-id collision (A2), manifest
resource limits + id charset (B1), inline-secret detection (A3), tool
timeout/retry (B2), guardrail block_retry (B3), compiler cache (D1) + compile
timing (C2), and workflow-run retention pruning (D2).
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from caliber.db.models import CaliberWorkflowRun
from caliber.ids import new_workflow_run_id
from caliber.workflows.compiler import (
    CompileError,
    build_ir,
    clear_compile_cache,
    compile_workflow,
)
from caliber.workflows.manifest import MAX_NODES, parse_manifest
from caliber.workflows.promoter import prune_workflow_runs
from caliber.workflows.runtime import FakeWorkflowExecutor, RuntimePlan, execute
from caliber.workflows.tools import InMemoryToolResolver
from caliber.workflows.validation import find_inline_secrets, validate_manifest
from tests.workflow_helpers import fake_resolver, make_manifest, make_support_manifest

# --- A1: codegen injection escaping -----------------------------------------


def test_agent_name_control_chars_rejected() -> None:
    data = make_manifest()
    data["nodes"]["agent"]["name"] = "bad\nname"
    with pytest.raises(ValidationError):
        parse_manifest(data)


def test_codegen_escapes_quotes_in_name() -> None:
    data = make_manifest()
    data["nodes"]["agent"]["name"] = 'a"b'  # quote is legal, must be escaped
    result = compile_workflow(parse_manifest(data), resolver=fake_resolver())
    # Generated module must be valid Python (the quote can't break the literal).
    compile(result.generated_python, "<generated>", "exec")
    assert 'name="a\\"b"' in result.generated_python


# --- B1: id charset + resource limits ----------------------------------------


def test_node_id_must_be_identifier() -> None:
    data = make_manifest()
    data["nodes"]["bad-id"] = {"id": "bad-id", "type": "note", "text": ""}
    with pytest.raises(ValidationError):
        parse_manifest(data)


def test_tool_key_must_be_identifier() -> None:
    data = make_manifest(tools={"bad-key": {"registry_ref": "tool.x.v1"}})
    data["nodes"]["agent"]["tools"] = []
    with pytest.raises(ValidationError):
        parse_manifest(data)


def test_too_many_nodes_rejected() -> None:
    data = make_manifest()
    for i in range(MAX_NODES + 1):
        data["nodes"][f"n{i}"] = {"id": f"n{i}", "type": "note", "text": ""}
    with pytest.raises(ValidationError):
        parse_manifest(data)


# --- A2: identifier collision ------------------------------------------------


def test_identifier_collision_rejected() -> None:
    # Two agent ids that normalize to the same Python identifier ("class" is a
    # keyword → "class_", colliding with the literal "class_").
    data = make_manifest(
        nodes={
            "start": {"id": "start", "type": "start", "outputs": {"m": {"type": "string"}}},
            "class": {
                "id": "class",
                "type": "agent",
                "name": "a",
                "model": "inherit",
                "instructions": {"type": "inline", "text": "x"},
                "inputs": {"input": {"type": "string"}},
                "outputs": {"final_output": {"type": "string"}},
            },
            "class_": {
                "id": "class_",
                "type": "agent",
                "name": "b",
                "model": "inherit",
                "instructions": {"type": "inline", "text": "y"},
                "inputs": {"input": {"type": "string"}},
                "outputs": {"final_output": {"type": "string"}},
            },
            "final": {"id": "final", "type": "output", "inputs": {"response": {"type": "string"}}},
        },
        edges=[
            {"id": "e1", "from": "start", "to": "class", "map": {"m": "input"}},
            {"id": "e2", "from": "class", "to": "class_", "map": {"final_output": "input"}},
            {"id": "e3", "from": "class_", "to": "final", "map": {"final_output": "response"}},
        ],
    )
    with pytest.raises(CompileError, match="identifier collision"):
        compile_workflow(parse_manifest(data), resolver=fake_resolver())


# --- A3: inline-secret detection ---------------------------------------------


def test_find_inline_secrets_flags_param_secret() -> None:
    data = make_support_manifest()
    data["nodes"]["policy_guardrail"]["checks"] = [
        {"forbid_substring": {"substring": "x", "api_key": "sk-secret-123"}}
    ]
    paths = find_inline_secrets(data)
    assert any("api_key" in p for p in paths)


def test_validate_reports_inline_secret() -> None:
    data = make_support_manifest()
    data["nodes"]["support_agent"]["output_type"] = {"password": "hunter2"}
    report = validate_manifest(parse_manifest(data), resolver=fake_resolver())
    assert any(e.code == "inline_secret" for e in report.errors)


def test_secret_refs_allowed() -> None:
    # secret_refs (names only) must NOT be flagged.
    assert find_inline_secrets({"tools": {"t": {"secret_refs": ["billing_api_token"]}}}) == []


# --- B2: tool timeout + retry ------------------------------------------------


def _manifest_with_tool(*, timeout=None, max_retries=0):
    data = make_manifest()
    data["nodes"]["agent"]["tools"] = ["flaky"]
    binding = {"registry_ref": "tool.flaky.v1"}
    if timeout is not None:
        binding["timeout_seconds"] = timeout
    if max_retries:
        binding["max_retries"] = max_retries
    data["tools"] = {"flaky": binding}
    return parse_manifest(data)


def test_tool_timeout_marks_run_error() -> None:
    def _slow(_q=""):
        time.sleep(1.0)
        return {"ok": True}

    resolver = InMemoryToolResolver.from_callables({"tool.flaky.v1": _slow})
    ir = build_ir(_manifest_with_tool(timeout=0.05), resolver, version="1")
    result = execute(RuntimePlan(ir=ir, resolver=resolver), "hi", executor=FakeWorkflowExecutor())
    assert result.status == "error"
    assert "timed out" in (result.error or "")


def test_tool_retry_recovers() -> None:
    calls = {"n": 0}

    def _flaky(_q=""):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return {"ok": True}

    resolver = InMemoryToolResolver.from_callables({"tool.flaky.v1": _flaky})
    ir = build_ir(_manifest_with_tool(max_retries=2), resolver, version="1")
    result = execute(RuntimePlan(ir=ir, resolver=resolver), "hi", executor=FakeWorkflowExecutor())
    assert result.status == "completed"
    assert calls["n"] == 2  # failed once, retried, succeeded


# --- B3: guardrail block_retry -----------------------------------------------


class _GroundAfterExecutor(FakeWorkflowExecutor):
    """Skips tools on the first call per agent, grounds (calls tools) after."""

    def __init__(self) -> None:
        super().__init__()
        self._seen: dict[str, int] = {}

    def run_agent(self, agent, input_text, *, tool_callables, preview):  # type: ignore[override]
        self._seen[agent.node_id] = self._seen.get(agent.node_id, 0) + 1
        self.skip_tools = self._seen[agent.node_id] == 1
        return super().run_agent(agent, input_text, tool_callables=tool_callables, preview=preview)


def test_guardrail_block_retry_recovers() -> None:
    data = make_support_manifest()
    data["nodes"]["policy_guardrail"]["on_failure"] = "block_retry"
    data["nodes"]["policy_guardrail"]["max_retries"] = 2
    resolver = fake_resolver()
    ir = build_ir(parse_manifest(data), resolver, version="1")
    result = execute(
        RuntimePlan(ir=ir, resolver=resolver),
        "what is your refund policy?",
        executor=_GroundAfterExecutor(),
    )
    assert result.status == "completed"  # blocked once, retried, grounded → passes


def test_guardrail_block_no_retry_blocks() -> None:
    data = make_support_manifest()
    resolver = fake_resolver()
    ir = build_ir(parse_manifest(data), resolver, version="1")
    result = execute(
        RuntimePlan(ir=ir, resolver=resolver),
        "what is your refund policy?",
        executor=FakeWorkflowExecutor(skip_tools=True),
    )
    assert result.status == "blocked"


# --- D1/C2: compiler cache + timing ------------------------------------------


def test_compile_cache_hit() -> None:
    clear_compile_cache()
    manifest = parse_manifest(make_support_manifest())
    resolver = fake_resolver()
    first = compile_workflow(manifest, resolver=resolver, version="1", use_cache=True)
    second = compile_workflow(manifest, resolver=resolver, version="1", use_cache=True)
    assert first.cached is False
    assert second.cached is True
    assert second.generated_python == first.generated_python


def test_compile_cache_cleared() -> None:
    clear_compile_cache()
    manifest = parse_manifest(make_support_manifest())
    resolver = fake_resolver()
    compile_workflow(manifest, resolver=resolver, version="1", use_cache=True)
    clear_compile_cache()
    again = compile_workflow(manifest, resolver=resolver, version="1", use_cache=True)
    assert again.cached is False


def test_compile_reports_timing() -> None:
    result = compile_workflow(parse_manifest(make_support_manifest()), resolver=fake_resolver())
    assert result.compile_ms >= 0.0
    assert "compile_ms" not in result.report  # kept off the golden-compared report


# --- D2: workflow-run retention ----------------------------------------------


def test_prune_workflow_runs(db_session) -> None:
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    db_session.add(
        CaliberWorkflowRun(
            workflow_run_id=new_workflow_run_id(),
            workflow_id="wf",
            status="completed",
            started_at=now - timedelta(days=30),
        )
    )
    db_session.add(
        CaliberWorkflowRun(
            workflow_run_id=new_workflow_run_id(),
            workflow_id="wf",
            status="completed",
            started_at=now - timedelta(days=1),
        )
    )
    db_session.commit()
    deleted = prune_workflow_runs(db_session, retention_days=7, now=now)
    db_session.commit()
    assert deleted == 1
    remaining = db_session.query(CaliberWorkflowRun).count()
    assert remaining == 1
