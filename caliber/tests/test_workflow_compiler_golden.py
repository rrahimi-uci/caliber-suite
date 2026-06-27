"""Golden snapshot tests for the workflow compiler (plan §19.5).

Compiling a fixture manifest must produce byte-identical generated Python and an
identical (key-sorted) compiler report. The expected outputs live under
``tests/golden/`` and are checked in. To regenerate after an *intentional*
codegen change, run with ``CALIBER_UPDATE_GOLDEN=1`` and commit the diff.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from caliber.workflows.compiler import compile_workflow
from caliber.workflows.diff import compute_graph_diff
from caliber.workflows.manifest import parse_manifest
from tests.workflow_helpers import fake_resolver, make_manifest, make_support_manifest

GOLDEN_DIR = Path(__file__).parent / "golden"
_UPDATE = os.environ.get("CALIBER_UPDATE_GOLDEN") == "1"


def _single_agent() -> dict[str, Any]:
    return make_manifest("single_agent_wf", name="Single Agent")


def _multi_agent_handoff() -> dict[str, Any]:
    data = make_manifest("multi_agent_wf", name="Multi Agent Handoff")
    data["nodes"]["agent"]["handoffs"] = [{"target": "billing", "description": "billing issues"}]
    data["nodes"]["agent"]["tools"] = ["lookup_policy"]
    data["nodes"]["billing"] = {
        "id": "billing",
        "type": "agent",
        "name": "billing-agent",
        "model": "inherit",
        "instructions": {"type": "inline", "text": "Handle billing."},
        "tools": ["get_order"],
        "inputs": {"input": {"type": "string"}},
        "outputs": {"final_output": {"type": "string"}},
    }
    data["tools"] = {
        "lookup_policy": {
            "registry_ref": "tool.lookup_policy.v1",
            "version_constraint": ">=1.0,<2.0",
        },
        "get_order": {"registry_ref": "tool.get_order.v1", "version_constraint": ">=1.0,<2.0"},
    }
    return data


def _guarded_pipeline() -> dict[str, Any]:
    return make_support_manifest("guarded_pipeline_wf", name="Guarded Pipeline")


FIXTURES = {
    "single_agent": _single_agent,
    "multi_agent_handoff": _multi_agent_handoff,
    "guarded_pipeline": _guarded_pipeline,
}


def _compile(name: str):
    manifest = parse_manifest(FIXTURES[name]())
    return compile_workflow(manifest, resolver=fake_resolver(), version="1")


def _check_or_update(path: Path, actual: str) -> None:
    if _UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual)
        return
    assert path.exists(), f"missing golden file {path}; run CALIBER_UPDATE_GOLDEN=1 to create it"
    expected = path.read_text()
    assert actual == expected, (
        f"generated output drifted from {path.name}; "
        "re-run with CALIBER_UPDATE_GOLDEN=1 if intentional"
    )


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_generated_python_golden(name: str) -> None:
    result = _compile(name)
    _check_or_update(GOLDEN_DIR / f"{name}_compiled.py", result.generated_python)


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_compiler_report_golden(name: str) -> None:
    result = _compile(name)
    actual = json.dumps(result.report, indent=2, sort_keys=True) + "\n"
    _check_or_update(GOLDEN_DIR / f"{name}_report.json", actual)


def test_graph_diff_golden() -> None:
    base = parse_manifest(_single_agent())
    cand_data = _single_agent()
    cand_data["nodes"]["guard"] = {
        "id": "guard",
        "type": "guardrail",
        "mode": "post_agent",
        "inputs": {"response": {"type": "string"}},
        "outputs": {"passthrough": {"type": "string"}},
        "checks": [{"non_empty_output": {}}],
    }
    candidate = parse_manifest(cand_data)
    diff = compute_graph_diff(base, candidate)
    actual = json.dumps(diff, indent=2, sort_keys=True) + "\n"
    _check_or_update(GOLDEN_DIR / "single_agent_add_guard_diff.json", actual)
