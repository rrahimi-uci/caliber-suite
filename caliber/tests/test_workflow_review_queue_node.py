"""Lifecycle and runtime coverage for Review Queue Enqueue workflow nodes."""

from __future__ import annotations

from typing import Any

from caliber.workflows import component_catalog as cc
from caliber.workflows.compiler import build_ir, compile_workflow
from caliber.workflows.ir import IRReviewQueueEnqueue
from caliber.workflows.manifest import ReviewQueueEnqueueNode, parse_manifest
from caliber.workflows.runtime import FakeWorkflowExecutor, RuntimePlan, execute
from caliber.workflows.tools import InMemoryToolResolver
from caliber.workflows.validation import validate_manifest


def _manifest() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "workflow_id": "wf_review_enqueue",
        "name": "Review enqueue demo",
        "runtime": {
            "sdk": "openai-agents-python",
            "sdk_version_policy": "runtime-pinned",
            "compiler_version": "caliber-workflow-compiler-v1",
            "default_model_ref": "CALIBER_WORKFLOW_DEFAULT_MODEL",
            "session": {"type": "none"},
        },
        "nodes": {
            "start": {
                "id": "start",
                "type": "start",
                "outputs": {"user_message": {"type": "string"}},
            },
            "enqueue": {
                "id": "enqueue",
                "type": "review_queue_enqueue",
                "queue_id": "RQ-support",
                "experiment_id": "42",
                "assigned_to": "reviewer@example.com",
                "inputs": {
                    "trace_id": {"type": "string"},
                    "trace_ids": {"type": "structured"},
                },
                "outputs": {
                    "text": {"type": "string"},
                    "result": {"type": "structured"},
                    "created_count": {"type": "structured"},
                },
            },
            "out": {
                "id": "out",
                "type": "output",
                "inputs": {"response": {"type": "string"}},
            },
        },
        "edges": [
            {"id": "e1", "from": "start", "to": "enqueue", "map": {"user_message": "trace_id"}},
            {"id": "e2", "from": "enqueue", "to": "out", "map": {"text": "response"}},
        ],
        "tools": {},
    }


def test_review_queue_enqueue_lifecycle() -> None:
    parsed = parse_manifest(_manifest())
    node = parsed.nodes["enqueue"]
    assert isinstance(node, ReviewQueueEnqueueNode)
    assert validate_manifest(parsed).errors == []

    component = next(
        item
        for item in cc.build_workflow_component_catalog()["components"]
        if item["type"] == "review_queue_enqueue"
    )
    assert component["category"] == "Governance"
    assert component["starter_node"]["queue_id"] == "REVIEW-QUEUE-ID"

    resolver = InMemoryToolResolver({})
    ir = build_ir(parsed, resolver, version="1")
    assert isinstance(ir.nodes["enqueue"], IRReviewQueueEnqueue)
    compiled = compile_workflow(parsed, resolver=resolver, version="1")
    assert compiled.report["validation"]["errors"] == []
    assert "review_queue_enqueue" in compiled.generated_python.lower()


def test_runtime_enqueues_trace_with_governance_metadata() -> None:
    captured: dict[str, Any] = {}

    def enqueue(payload: dict[str, Any]) -> dict[str, Any]:
        captured.update(payload)
        return {
            "queue_id": payload["queue_id"],
            "created_count": 1,
            "item_ids": ["RI-1"],
            "trace_ids": payload["trace_ids"],
        }

    parsed = parse_manifest(_manifest())
    resolver = InMemoryToolResolver({})
    result = execute(
        RuntimePlan(
            ir=build_ir(parsed, resolver, version="1"),
            resolver=resolver,
            review_queue_enqueuer=enqueue,
        ),
        "trace-123",
        executor=FakeWorkflowExecutor(),
    )
    assert result.status == "completed"
    assert result.output == "Enqueued 1 trace for review."
    assert captured == {
        "queue_id": "RQ-support",
        "trace_ids": ["trace-123"],
        "experiment_id": "42",
        "assigned_to": "reviewer@example.com",
    }


def test_runtime_fails_closed_without_database_enqueuer() -> None:
    parsed = parse_manifest(_manifest())
    resolver = InMemoryToolResolver({})
    result = execute(
        RuntimePlan(ir=build_ir(parsed, resolver, version="1"), resolver=resolver),
        "trace-123",
        executor=FakeWorkflowExecutor(),
    )
    assert result.status == "error"
    assert "requires the CALIBER database runtime" in (result.error or "")
