"""Fail-closed coverage for workflow Preview capability isolation."""

from __future__ import annotations

import pytest

import caliber.workflows.runtime as workflow_runtime
from caliber.workflows.ir import (
    IREdge,
    IRFileInput,
    IRManagedFileReference,
    IRNode,
    IRSubworkflow,
    IRType,
    IRWebhook,
    IRWorkflow,
    NodeType,
)
from caliber.workflows.runtime import FakeWorkflowExecutor, RuntimePlan, execute
from caliber.workflows.tools import InMemoryToolResolver

UNISOLATED_NODE_TYPES = (
    NodeType.FILE_INPUT,
    NodeType.FOLDER_INPUT,
    NodeType.INPUT_BUCKET,
    NodeType.OUTPUT_BUCKET,
    NodeType.OUTPUT_FOLDER,
    NodeType.MCP_RESOURCE,
    NodeType.PYTHON_CODE,
    NodeType.EXTERNAL_APP,
    NodeType.WEBHOOK,
    NodeType.API_REQUEST,
)


def _plan(*node_types: NodeType) -> RuntimePlan:
    nodes = {
        "start": IRNode(node_id="start", node_type=NodeType.START),
        "final": IRNode(node_id="final", node_type=NodeType.OUTPUT),
    }
    for node_type in node_types:
        node_id = f"capability_{node_type.value}"
        nodes[node_id] = IRNode(node_id=node_id, node_type=node_type)
    return RuntimePlan(
        ir=IRWorkflow(
            workflow_id="preview-preflight",
            version="1",
            nodes=nodes,
            edges=[],
            entry_node_id="start",
            output_node_id="final",
        ),
        resolver=InMemoryToolResolver([]),
    )


def _connected_plan(
    node: IRNode,
    **plan_kwargs: object,
) -> RuntimePlan:
    string = IRType("string")
    start = IRNode(node_id="start", node_type=NodeType.START, outputs={"msg": string})
    node.inputs = {"input": string}
    node.outputs = {"output": string}
    final = IRNode(node_id="final", node_type=NodeType.OUTPUT, inputs={"response": string})
    return RuntimePlan(
        ir=IRWorkflow(
            workflow_id="connected-preview-preflight",
            version="1",
            nodes={"start": start, node.node_id: node, "final": final},
            edges=[
                IREdge("start_effect", "start", "msg", node.node_id, "input", string),
                IREdge("effect_final", node.node_id, "output", "final", "response", string),
            ],
            entry_node_id="start",
            output_node_id="final",
        ),
        resolver=InMemoryToolResolver([]),
        **plan_kwargs,
    )


@pytest.mark.parametrize("node_type", UNISOLATED_NODE_TYPES)
def test_preview_preflight_blocks_each_unisolated_capability_before_interpretation(
    node_type: NodeType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interpreted = False

    def unexpected_interpret(*args: object, **kwargs: object) -> None:
        nonlocal interpreted
        interpreted = True
        raise AssertionError("Preview entered the workflow interpreter")

    monkeypatch.setattr(workflow_runtime, "_interpret", unexpected_interpret)

    result = execute(_plan(node_type), "input", executor=FakeWorkflowExecutor(), preview=True)

    node_id = f"capability_{node_type.value}"
    assert interpreted is False
    assert result.status == "error"
    assert result.output == ""
    assert result.tags["caliber.preview"] == "true"
    assert result.error is not None
    assert f"{node_id} ({node_type.value})" in result.error
    assert [(step.node_id, step.node_type, step.status) for step in result.steps] == [
        (node_id, node_type.value, "error")
    ]


def test_preview_preflight_reports_all_blockers_deterministically() -> None:
    result = execute(
        _plan(*reversed(UNISOLATED_NODE_TYPES)),
        "input",
        executor=FakeWorkflowExecutor(),
        preview=True,
    )

    expected = sorted(
        (f"capability_{node_type.value}", node_type.value) for node_type in UNISOLATED_NODE_TYPES
    )
    assert result.status == "error"
    assert [(step.node_id, step.node_type) for step in result.steps] == expected
    assert result.error is not None
    for node_id, node_type in expected:
        assert f"{node_id} ({node_type})" in result.error


def test_preview_allows_managed_file_and_invokes_scoped_resolver() -> None:
    string = IRType("string")
    structured = IRType("structured")
    snapshot = IRManagedFileReference(
        file_id="FILE-preview",
        file_ref="caliber://projects/PRJ-preview/input/source.md",
        sha256="a" * 64,
        name="source.md",
        size_bytes=15,
        media_type="text/markdown",
        object_version_id="v1",
    )
    source = IRFileInput(
        node_id="managed_source",
        node_type=NodeType.FILE_INPUT,
        inputs={"path": string},
        outputs={"text": string, "metadata": structured},
        file_ref=snapshot,
    )
    start = IRNode(node_id="start", node_type=NodeType.START, outputs={"msg": string})
    final = IRNode(node_id="final", node_type=NodeType.OUTPUT, inputs={"response": string})
    plan = RuntimePlan(
        ir=IRWorkflow(
            workflow_id="managed-preview",
            version="1",
            nodes={start.node_id: start, source.node_id: source, final.node_id: final},
            edges=[
                IREdge(
                    "start_source",
                    start.node_id,
                    "msg",
                    source.node_id,
                    "path",
                    string,
                ),
                IREdge(
                    "source_final",
                    source.node_id,
                    "text",
                    final.node_id,
                    "response",
                    string,
                ),
            ],
            entry_node_id=start.node_id,
            output_node_id=final.node_id,
        ),
        resolver=InMemoryToolResolver([]),
    )
    calls: list[tuple[IRManagedFileReference, str, int]] = []

    def resolve(
        file_ref: IRManagedFileReference, encoding: str, max_bytes: int
    ) -> tuple[str, dict[str, object]]:
        calls.append((file_ref, encoding, max_bytes))
        return "managed preview", {"bytes": 15, "sha256": file_ref.sha256}

    result = execute(
        plan,
        "ignored",
        executor=FakeWorkflowExecutor(),
        preview=True,
        managed_file_resolver=resolve,
    )

    assert result.status == "completed"
    assert calls == [(snapshot, "utf-8", 200_000)]
    source_step = next(step for step in result.steps if step.node_id == source.node_id)
    assert source_step.output == "managed preview"


def test_preview_still_blocks_legacy_file_path_before_interpretation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interpreted = False

    def unexpected_interpret(*args: object, **kwargs: object) -> None:
        nonlocal interpreted
        interpreted = True
        raise AssertionError("Preview entered the workflow interpreter")

    monkeypatch.setattr(workflow_runtime, "_interpret", unexpected_interpret)
    legacy = IRFileInput(
        node_id="legacy_source",
        node_type=NodeType.FILE_INPUT,
        path="/host/path/must-not-be-read.txt",
    )

    result = execute(
        _connected_plan(legacy),
        "ignored",
        executor=FakeWorkflowExecutor(),
        preview=True,
    )

    assert interpreted is False
    assert result.status == "error"
    assert "legacy_source (file_input)" in (result.error or "")


def test_preview_preflight_never_starts_nodes_or_invokes_reachable_effect_callable() -> None:
    effect_calls: list[dict[str, object]] = []
    started_nodes: list[str] = []

    def webhook_sender(payload: dict[str, object]) -> dict[str, object]:
        effect_calls.append(payload)
        return {"status_code": 200, "body": "sent"}

    plan = _connected_plan(
        IRWebhook(node_id="send_webhook", node_type=NodeType.WEBHOOK, url="https://example.test"),
        webhook_sender=webhook_sender,
    )
    result = execute(
        plan,
        "input",
        executor=FakeWorkflowExecutor(),
        preview=True,
        on_node_start=lambda node_id, _node, _inputs: started_nodes.append(node_id),
    )

    assert result.status == "error"
    assert effect_calls == []
    assert started_nodes == []
    assert [(step.node_id, step.node_type) for step in result.steps] == [
        ("send_webhook", "webhook")
    ]


def test_subworkflow_preview_propagates_preflight_to_child() -> None:
    effect_calls: list[dict[str, object]] = []
    child_preview_flags: list[bool] = []

    def webhook_sender(payload: dict[str, object]) -> dict[str, object]:
        effect_calls.append(payload)
        return {"status_code": 200, "body": "sent"}

    child_plan = _connected_plan(
        IRWebhook(node_id="child_webhook", node_type=NodeType.WEBHOOK, url="https://example.test"),
        webhook_sender=webhook_sender,
    )

    def run_child(
        _workflow_id: str,
        _alias: str,
        input_text: str,
        _timeout_seconds: float,
        _depth: int,
        executor: FakeWorkflowExecutor,
        preview: bool,
    ) -> dict[str, object]:
        child_preview_flags.append(preview)
        child_result = execute(child_plan, input_text, executor=executor, preview=preview)
        return {
            "status": child_result.status,
            "output": child_result.output,
            "error": child_result.error,
            "tokens": child_result.tokens,
        }

    parent_plan = _connected_plan(
        IRSubworkflow(
            node_id="child",
            node_type=NodeType.SUBWORKFLOW,
            workflow_id="child-workflow",
        ),
        subworkflow_runner=run_child,
    )
    result = execute(
        parent_plan,
        "input",
        executor=FakeWorkflowExecutor(),
        preview=True,
    )

    assert result.status == "error"
    assert child_preview_flags == [True]
    assert effect_calls == []
    assert result.error is not None
    assert "child_webhook (webhook)" in result.error


def test_preview_without_unisolated_capabilities_still_runs() -> None:
    result = execute(_plan(), "input", executor=FakeWorkflowExecutor(), preview=True)

    assert result.status == "completed"
    assert result.tags["caliber.preview"] == "true"
