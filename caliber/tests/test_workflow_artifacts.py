"""Multi-artifact run output: a node's ``artifacts`` map -> WorkflowRunResult.artifacts.

Lets a single run emit multiple named files (e.g. kg.json + report.html) even
though the output node flattens to one string. The run route persists these to
the run workspace.
"""

from __future__ import annotations

from caliber.workflows.compiler import build_ir
from caliber.workflows.manifest import parse_manifest
from caliber.workflows.runtime import (
    FakeWorkflowExecutor,
    RuntimePlan,
    _collect_artifacts,
    execute,
)
from caliber.workflows.tools import InMemoryToolResolver


def _manifest(code: str) -> dict:
    return {
        "schema_version": 1,
        "workflow_id": "WF-art",
        "name": "art",
        "runtime": {
            "sdk": "openai-agents-python",
            "sdk_version_policy": "runtime-pinned",
            "compiler_version": "caliber-workflow-compiler-v1",
            "default_model_ref": "gpt-4o",
        },
        "nodes": {
            "start": {"id": "start", "type": "start", "outputs": {"doc": {"type": "string"}}},
            "agent": {
                "id": "agent",
                "type": "agent",
                "name": "a",
                "model": "gpt-4o",
                "instructions": {"type": "inline", "text": "echo the input"},
                "inputs": {"input": {"type": "string"}},
                "outputs": {"final_output": {"type": "string"}},
            },
            "code": {
                "id": "code",
                "type": "python_code",
                "timeout_seconds": 10,
                "code": code,
                "inputs": {"input": {"type": "string"}, "context": {"type": "structured"}},
                "outputs": {
                    "text": {"type": "string"},
                    "result": {"type": "structured"},
                    "metadata": {"type": "structured"},
                },
            },
            "output": {
                "id": "output",
                "type": "output",
                "inputs": {"response": {"type": "string"}},
            },
        },
        "edges": [
            {"id": "e0", "from": "start", "to": "agent", "map": {"doc": "input"}},
            {"id": "e1", "from": "agent", "to": "code", "map": {"final_output": "input"}},
            {"id": "e2", "from": "code", "to": "output", "map": {"text": "response"}},
        ],
        "tools": {},
    }


def _run(manifest: dict):
    ir = build_ir(parse_manifest(manifest), InMemoryToolResolver(), version="1")
    return execute(
        RuntimePlan(ir=ir, resolver=InMemoryToolResolver()), "hi", executor=FakeWorkflowExecutor()
    )


def test_python_code_artifacts_collected_into_result() -> None:
    code = 'return {"text": "ok", "artifacts": {"a.json": "{\\"x\\": 1}", "b.html": "<html>hi</html>"}}'
    result = _run(_manifest(code))
    assert result.status == "completed"
    assert result.artifacts == {"a.json": '{"x": 1}', "b.html": "<html>hi</html>"}


def test_no_artifacts_defaults_to_empty() -> None:
    result = _run(_manifest('return {"text": "ok", "result": {"ok": True}}'))
    assert result.artifacts == {}


def test_collect_artifacts_handles_both_shapes() -> None:
    port_values = {
        ("a", "result"): {"artifacts": {"x.txt": "1"}},  # top-level
        ("b", "result"): {"result": {"artifacts": {"y.txt": "2"}}},  # nested under result
        ("c", "text"): "plain string ignored",
        ("d", "result"): {"no_artifacts": True},
    }
    assert _collect_artifacts(port_values) == {"x.txt": "1", "y.txt": "2"}


def test_collect_artifacts_serializes_non_string_content() -> None:
    out = _collect_artifacts({("a", "result"): {"artifacts": {"d.json": {"k": 1}}}})
    assert out["d.json"] == '{"k": 1}'
