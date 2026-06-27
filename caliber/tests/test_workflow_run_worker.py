"""Integration-style tests for the workflow-run queue worker."""

from __future__ import annotations

import asyncio
import json
import sys
import time
import types
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import caliber.orchestrator.workflow_run_worker as workflow_run_worker_module
from caliber.config import WorkflowStorageConfig
from caliber.db.models import (
    CaliberRuntimeApprovalRequest,
    CaliberWorkflow,
    CaliberWorkflowRun,
    CaliberWorkflowRunCheckpoint,
    CaliberWorkflowRunEvent,
    CaliberWorkflowSessionMemory,
    CaliberWorkflowVersion,
)
from caliber.mcp_gateway import McpGatewayError
from caliber.orchestrator.workflow_run_worker import WorkflowRunWorker
from caliber.storage.base import StorageUnavailableError
from caliber.storage.service import build_backend
from caliber.workflows.runtime import (
    MAX_AGENT_TOOL_ITERATIONS,
    FakeWorkflowExecutor,
    NodeStep,
    WorkflowRunResult,
)
from caliber.workflows.template_catalog import build_workflow_template_catalog
from tests.test_agentic_tool_loop import (
    _AToolResp,
    _FakeOpenAI,
    _FakeOpenAIResponses,
    _final_resp,
    _install_agents_workflow_sdk,
    _responses_final_resp,
    _responses_tool_call_resp,
    _tool_call_resp,
)
from tests.workflow_helpers import (
    PREFIX,
    create_and_publish,
    create_draft,
    create_workflow,
    make_manifest,
    make_support_manifest,
    register_demo_tools,
)


def _as_utc(value: datetime) -> datetime:
    """Normalize a DB-roundtripped timestamp to UTC-aware (SQLite drops tzinfo)."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _set_run_queued_at(client, workflow_run_id: str, queued_at: datetime) -> None:
    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, workflow_run_id)
        assert run is not None
        run.queued_at = queued_at
        session.commit()


def _local_bucket_io(tmp_path):
    backend = build_backend(WorkflowStorageConfig(backend="local", base_uri=f"file://{tmp_path}"))

    def _io(bucket: str, prefix: str):
        key_prefix = "/".join(p for p in [bucket.strip("/"), (prefix or "").strip("/")] if p)
        return backend, key_prefix

    return _io


def _starter_manifest(
    kind: str,
    *,
    workflow_id: str | None = None,
    workflow_name: str | None = None,
) -> dict[str, object]:
    catalog = build_workflow_template_catalog()
    templates = catalog.get("templates")
    assert isinstance(templates, list)
    for template in templates:
        assert isinstance(template, dict)
        if template.get("kind") != kind:
            continue
        manifest = deepcopy(template["manifest_template"])
        if workflow_id is not None:
            manifest["workflow_id"] = workflow_id
        if workflow_name is not None:
            manifest["name"] = workflow_name
        return manifest
    raise AssertionError(f"unknown workflow starter template {kind!r}")


def _enable_queue(client) -> None:
    client.app.state.config = client.app.state.config.model_copy(
        update={"workflow_run_queue_enabled": True}
    )


def _enable_runtime_approvals(client) -> None:
    client.app.state.config = client.app.state.config.model_copy(
        update={
            "workflow_run_queue_enabled": True,
            "workflow_run_runtime_approvals_enabled": True,
            "workflow_run_checkpointing_enabled": True,
        }
    )


def _approval_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["edges"] = [
        {"id": "e1", "from": "start", "to": "agent", "map": {"msg": "input"}},
        {"id": "e2", "from": "agent", "to": "human_gate", "map": {"final_output": "request"}},
        {"id": "e3", "from": "human_gate", "to": "final", "map": {"request": "response"}},
    ]
    nodes = manifest["nodes"]
    assert isinstance(nodes, dict)
    nodes["human_gate"] = {
        "id": "human_gate",
        "type": "human_approval",
        "inputs": {"request": {"type": "string"}},
        "outputs": {"request": {"type": "string"}},
    }
    return manifest


def _openai_structured_approval_manifest(workflow_id: str) -> dict[str, object]:
    manifest = _approval_manifest(workflow_id)
    nodes = manifest["nodes"]
    assert isinstance(nodes, dict)
    agent = nodes["agent"]
    assert isinstance(agent, dict)
    agent["tools"] = ["lookup_policy"]
    agent["output_type"] = {
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "grounded": {"type": "boolean"},
        },
        "required": ["answer", "grounded"],
    }
    agent["outputs"] = {
        "final_output": {"type": "string"},
        "structured_output": {"type": "structured"},
        "tool_calls": {"type": "structured"},
    }
    manifest["tools"] = {
        "lookup_policy": {
            "registry_ref": "tool.lookup_policy.v1",
            "version_constraint": ">=1.0,<2.0",
        }
    }
    return manifest


def _openai_structured_gated_tool_approval_manifest(workflow_id: str) -> dict[str, object]:
    manifest = _openai_structured_approval_manifest(workflow_id)
    nodes = manifest["nodes"]
    assert isinstance(nodes, dict)
    agent = nodes["agent"]
    assert isinstance(agent, dict)
    agent["tools"] = ["escalate"]
    manifest["tools"] = {
        "escalate": {
            "registry_ref": "tool.escalate.v1",
            "version_constraint": ">=1.0",
            "requires_approval": True,
        }
    }
    return manifest


def _openai_structured_mixed_tool_approval_manifest(workflow_id: str) -> dict[str, object]:
    manifest = _openai_structured_approval_manifest(workflow_id)
    nodes = manifest["nodes"]
    assert isinstance(nodes, dict)
    agent = nodes["agent"]
    assert isinstance(agent, dict)
    agent["tools"] = ["lookup_policy", "escalate"]
    manifest["tools"] = {
        "lookup_policy": {
            "registry_ref": "tool.lookup_policy.v1",
            "version_constraint": ">=1.0,<2.0",
        },
        "escalate": {
            "registry_ref": "tool.escalate.v1",
            "version_constraint": ">=1.0",
            "requires_approval": True,
        },
    }
    return manifest


def _openai_structured_mixed_dual_failsoft_tool_approval_manifest(
    workflow_id: str,
) -> dict[str, object]:
    manifest = _openai_structured_mixed_tool_approval_manifest(workflow_id)
    nodes = manifest["nodes"]
    assert isinstance(nodes, dict)
    agent = nodes["agent"]
    assert isinstance(agent, dict)
    agent["tools"] = ["lookup_policy", "escalate", "lookup_policy_retry"]
    tools = manifest["tools"]
    assert isinstance(tools, dict)
    tools["lookup_policy_retry"] = {
        "registry_ref": "tool.lookup_policy.v1",
        "version_constraint": ">=1.0,<2.0",
        "max_retries": 2,
    }
    return manifest


def _openai_structured_retry_approval_manifest(workflow_id: str) -> dict[str, object]:
    manifest = _openai_structured_approval_manifest(workflow_id)
    tools = manifest["tools"]
    assert isinstance(tools, dict)
    lookup_policy = tools["lookup_policy"]
    assert isinstance(lookup_policy, dict)
    lookup_policy["max_retries"] = 1
    return manifest


def _tool_approval_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"] = {
        "start": {
            "id": "start",
            "type": "start",
            "outputs": {"msg": {"type": "string"}},
        },
        "tool_gate": {
            "id": "tool_gate",
            "type": "tool",
            "tool_name": "escalate",
            "inputs": {
                "input": {"type": "string"},
                "arguments": {"type": "structured"},
            },
            "outputs": {
                "text": {"type": "string"},
                "result": {"type": "structured"},
                "metadata": {"type": "structured"},
            },
        },
        "final": {
            "id": "final",
            "type": "output",
            "inputs": {"response": {"type": "string"}},
        },
    }
    manifest["edges"] = [
        {"id": "e1", "from": "start", "to": "tool_gate", "map": {"msg": "input"}},
        {"id": "e2", "from": "tool_gate", "to": "final", "map": {"text": "response"}},
    ]
    manifest["tools"] = {
        "escalate": {
            "registry_ref": "tool.escalate.v1",
            "version_constraint": ">=1.0",
            "requires_approval": True,
        }
    }
    return manifest


def _tool_failure_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"] = {
        "start": {
            "id": "start",
            "type": "start",
            "outputs": {"msg": {"type": "string"}},
        },
        "tool_lookup": {
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
        },
        "final": {
            "id": "final",
            "type": "output",
            "inputs": {"response": {"type": "string"}},
        },
    }
    manifest["edges"] = [
        {"id": "e_start_tool", "from": "start", "to": "tool_lookup", "map": {"msg": "input"}},
        {"id": "e_tool_final", "from": "tool_lookup", "to": "final", "map": {"text": "response"}},
    ]
    manifest["tools"] = {
        "lookup_policy": {
            "registry_ref": "tool.lookup_policy.v1",
            "version_constraint": ">=1.0,<2.0",
        }
    }
    return manifest


def _tool_success_manifest(workflow_id: str) -> dict[str, object]:
    return _tool_failure_manifest(workflow_id)


def _tool_first_manifest(workflow_id: str) -> dict[str, object]:
    manifest = _tool_success_manifest(workflow_id)
    nodes = manifest["nodes"]
    assert isinstance(nodes, dict)
    nodes.pop("agent", None)
    return manifest


def _multi_hop_handoff_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    nodes = manifest["nodes"]
    assert isinstance(nodes, dict)
    agent = nodes["agent"]
    assert isinstance(agent, dict)
    agent["handoffs"] = [{"target": "billing"}]
    nodes["billing"] = {
        "id": "billing",
        "type": "agent",
        "name": "billing-agent",
        "model": "inherit",
        "instructions": {"type": "inline", "text": "b"},
        "handoffs": [{"target": "approvals"}],
        "inputs": {"input": {"type": "string"}},
        "outputs": {"final_output": {"type": "string"}},
    }
    nodes["approvals"] = {
        "id": "approvals",
        "type": "agent",
        "name": "approvals-agent",
        "model": "inherit",
        "instructions": {"type": "inline", "text": "c"},
        "inputs": {"input": {"type": "string"}},
        "outputs": {"final_output": {"type": "string"}},
    }
    return manifest


def _handoff_input_filter_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    nodes = manifest["nodes"]
    assert isinstance(nodes, dict)
    agent = nodes["agent"]
    assert isinstance(agent, dict)
    agent["handoffs"] = [
        {
            "target": "billing",
            "input_filter": "Billing summary for {{input}}\nAgent said: {{final_output}}",
        }
    ]
    nodes["billing"] = {
        "id": "billing",
        "type": "agent",
        "name": "billing-agent",
        "model": "inherit",
        "instructions": {"type": "inline", "text": "b"},
        "inputs": {"input": {"type": "string"}},
        "outputs": {"final_output": {"type": "string"}},
    }
    return manifest


def _loop_completion_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"] = {
        "start": {
            "id": "start",
            "type": "start",
            "outputs": {"msg": {"type": "string"}},
        },
        "counter": {
            "id": "counter",
            "type": "python_code",
            "code": (
                'current = inputs.get("count")\n'
                "if current is None:\n"
                '    current = int(str(run_input or "0") or "0")\n'
                "count = int(current) + 1\n"
                'next_state = {"count": count, "done": count >= 3}\n'
                'return {"text": str(count), "result": next_state}'
            ),
            "inputs": {"count": {"type": "structured"}, "done": {"type": "boolean"}},
            "outputs": {"text": {"type": "string"}, "result": {"type": "structured"}},
        },
        "loop": {
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
        },
        "final": {
            "id": "final",
            "type": "output",
            "inputs": {"response": {"type": "string"}},
        },
    }
    manifest["edges"] = [
        {"id": "e_start_loop", "from": "start", "to": "loop", "map": {"msg": "input"}},
        {"id": "e_loop_final", "from": "loop", "to": "final", "map": {"output": "response"}},
    ]
    return manifest


def _loop_invalid_stop_condition_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"] = {
        "start": {
            "id": "start",
            "type": "start",
            "outputs": {"msg": {"type": "string"}},
        },
        "counter": {
            "id": "counter",
            "type": "python_code",
            "code": 'return {"text": "1", "result": {"count": 1}}',
            "inputs": {"input": {"type": "string"}},
            "outputs": {"text": {"type": "string"}, "result": {"type": "structured"}},
        },
        "loop": {
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
        },
        "final": {
            "id": "final",
            "type": "output",
            "inputs": {"response": {"type": "string"}},
        },
    }
    manifest["edges"] = [
        {"id": "e_start_loop", "from": "start", "to": "loop", "map": {"msg": "input"}},
        {"id": "e_loop_final", "from": "loop", "to": "final", "map": {"output": "response"}},
    ]
    return manifest


def _external_app_timeout_manifest(workflow_id: str, *, entrypoint: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"]["external"] = {
        "id": "external",
        "type": "external_app",
        "entrypoint": entrypoint,
        "inputs": {"input": {"type": "string"}},
        "outputs": {
            "text": {"type": "string"},
            "result": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
        "execution_policy": {"timeout_seconds": 0.001},
    }
    manifest["edges"] = [
        {"id": "e_start_external", "from": "start", "to": "external", "map": {"msg": "input"}},
        {"id": "e_external_final", "from": "external", "to": "final", "map": {"text": "response"}},
    ]
    return manifest


def _external_app_success_manifest(workflow_id: str, *, entrypoint: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"]["external"] = {
        "id": "external",
        "type": "external_app",
        "entrypoint": entrypoint,
        "inputs": {"input": {"type": "string"}},
        "outputs": {
            "text": {"type": "string"},
            "result": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    manifest["edges"] = [
        {"id": "e_start_external", "from": "start", "to": "external", "map": {"msg": "input"}},
        {"id": "e_external_final", "from": "external", "to": "final", "map": {"text": "response"}},
    ]
    return manifest


def _file_input_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"] = {
        "start": {
            "id": "start",
            "type": "start",
            "outputs": {"msg": {"type": "string"}},
        },
        "file_input": {
            "id": "file_input",
            "type": "file_input",
            "inputs": {"path": {"type": "string"}},
            "outputs": {
                "text": {"type": "string"},
                "path": {"type": "string"},
                "metadata": {"type": "structured"},
            },
        },
        "final": {
            "id": "final",
            "type": "output",
            "inputs": {"response": {"type": "string"}},
        },
    }
    manifest["edges"] = [
        {"id": "e_start_file", "from": "start", "to": "file_input", "map": {"msg": "path"}},
        {"id": "e_file_final", "from": "file_input", "to": "final", "map": {"text": "response"}},
    ]
    return manifest


def _folder_input_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"] = {
        "start": {
            "id": "start",
            "type": "start",
            "outputs": {"msg": {"type": "string"}},
        },
        "folder_input": {
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
        },
        "final": {
            "id": "final",
            "type": "output",
            "inputs": {"response": {"type": "string"}},
        },
    }
    manifest["edges"] = [
        {"id": "e_start_folder", "from": "start", "to": "folder_input", "map": {"msg": "path"}},
        {
            "id": "e_folder_final",
            "from": "folder_input",
            "to": "final",
            "map": {"text": "response"},
        },
    ]
    return manifest


def _output_folder_invalid_target_manifest(
    workflow_id: str,
    *,
    path: str,
) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"]["output_folder"] = {
        "id": "output_folder",
        "type": "output_folder",
        "path": path,
        "inputs": {"input": {"type": "string"}},
        "outputs": {
            "files": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    manifest["edges"] = [
        {"id": "e_start_agent", "from": "start", "to": "agent", "map": {"msg": "input"}},
        {
            "id": "e_agent_final",
            "from": "agent",
            "to": "final",
            "map": {"final_output": "response"},
        },
        {
            "id": "e_agent_folder",
            "from": "agent",
            "to": "output_folder",
            "map": {"final_output": "input"},
        },
    ]
    return manifest


def _output_bucket_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"]["output_bucket"] = {
        "id": "output_bucket",
        "type": "output_bucket",
        "bucket": "results",
        "prefix": "run1/",
        "inputs": {"input": {"type": "string"}},
        "outputs": {
            "keys": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    manifest["edges"] = [
        {"id": "e_start_agent", "from": "start", "to": "agent", "map": {"msg": "input"}},
        {
            "id": "e_agent_final",
            "from": "agent",
            "to": "final",
            "map": {"final_output": "response"},
        },
        {
            "id": "e_agent_bucket",
            "from": "agent",
            "to": "output_bucket",
            "map": {"final_output": "input"},
        },
    ]
    return manifest


def _input_bucket_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"] = {
        "start": {
            "id": "start",
            "type": "start",
            "outputs": {"msg": {"type": "string"}},
        },
        "input_bucket": {
            "id": "input_bucket",
            "type": "input_bucket",
            "bucket": "docs",
            "prefix": "",
            "recursive": True,
            "max_files": 10,
            "inputs": {"prefix": {"type": "string"}},
            "outputs": {
                "text": {"type": "string"},
                "files": {"type": "structured"},
                "metadata": {"type": "structured"},
            },
        },
        "final": {
            "id": "final",
            "type": "output",
            "inputs": {"response": {"type": "string"}},
        },
    }
    manifest["edges"] = [
        {"id": "e_start_bucket", "from": "start", "to": "input_bucket", "map": {"msg": "prefix"}},
        {
            "id": "e_bucket_final",
            "from": "input_bucket",
            "to": "final",
            "map": {"text": "response"},
        },
    ]
    return manifest


def _external_app_invalid_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"]["external"] = {
        "id": "external",
        "type": "external_app",
        "entrypoint": "missing-module:handle",
        "inputs": {"input": {"type": "string"}},
        "outputs": {
            "text": {"type": "string"},
            "result": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    manifest["edges"] = [
        {"id": "e_start_external", "from": "start", "to": "external", "map": {"msg": "input"}},
        {"id": "e_external_final", "from": "external", "to": "final", "map": {"text": "response"}},
    ]
    return manifest


def _subworkflow_child_failure_manifest(
    workflow_id: str,
    *,
    child_workflow_id: str,
) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"] = {
        "start": {
            "id": "start",
            "type": "start",
            "outputs": {"msg": {"type": "string"}},
        },
        "child_workflow": {
            "id": "child_workflow",
            "type": "subworkflow",
            "workflow_id": child_workflow_id,
            "alias": "manual",
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
        {"id": "e_start_child", "from": "start", "to": "child_workflow", "map": {"msg": "input"}},
        {
            "id": "e_child_final",
            "from": "child_workflow",
            "to": "final",
            "map": {"output": "response"},
        },
    ]
    return manifest


def _subworkflow_success_manifest(
    workflow_id: str,
    *,
    child_workflow_id: str,
) -> dict[str, object]:
    return _subworkflow_child_failure_manifest(
        workflow_id,
        child_workflow_id=child_workflow_id,
    )


def _mcp_resource_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"]["mcp_lookup"] = {
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
    manifest["edges"] = [
        {"id": "e_start_mcp", "from": "start", "to": "mcp_lookup", "map": {"msg": "input"}},
        {"id": "e_mcp_final", "from": "mcp_lookup", "to": "final", "map": {"text": "response"}},
    ]
    return manifest


def _knowledge_build_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"]["knowledge_build"] = {
        "id": "knowledge_build",
        "type": "knowledge_build",
        "knowledge_base_id": "KB-1",
        "chunking_strategy": "recursive",
        "embedding_model": "BAAI/bge-m3",
    }
    manifest["edges"] = [
        {"id": "e_start_build", "from": "start", "to": "knowledge_build", "map": {"msg": "input"}},
        {
            "id": "e_build_final",
            "from": "knowledge_build",
            "to": "final",
            "map": {"text": "response"},
        },
    ]
    return manifest


def _subworkflow_missing_deployment_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"] = {
        "start": {
            "id": "start",
            "type": "start",
            "outputs": {"msg": {"type": "string"}},
        },
        "child_workflow": {
            "id": "child_workflow",
            "type": "subworkflow",
            "workflow_id": "WF-missing-child",
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
        {"id": "e_start_child", "from": "start", "to": "child_workflow", "map": {"msg": "input"}},
        {
            "id": "e_child_final",
            "from": "child_workflow",
            "to": "final",
            "map": {"output": "response"},
        },
    ]
    return manifest


def _knowledge_query_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"]["knowledge"] = {
        "id": "knowledge",
        "type": "knowledge_query",
        "knowledge_base_id": "KB-missing-active",
        "retrieval_modes": ["dense"],
        "top_k": 4,
    }
    manifest["edges"] = [
        {"id": "e_start_knowledge", "from": "start", "to": "knowledge", "map": {"msg": "question"}},
        {
            "id": "e_knowledge_final",
            "from": "knowledge",
            "to": "final",
            "map": {"answer": "response"},
        },
    ]
    return manifest


def _error_boundary_knowledge_recovery_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"] = {
        "start": {
            "id": "start",
            "type": "start",
            "outputs": {"msg": {"type": "string"}},
        },
        "knowledge": {
            "id": "knowledge",
            "type": "knowledge_query",
            "knowledge_base_id": "KB-missing-active",
            "retrieval_modes": ["dense"],
            "top_k": 4,
            "inputs": {"question": {"type": "string"}},
            "outputs": {"answer": {"type": "string"}, "result": {"type": "structured"}},
        },
        "python": {
            "id": "python",
            "type": "python_code",
            "code": 'return {"text": f"recovered:{input or run_input}", "result": {"ok": True}}',
            "inputs": {"input": {"type": "string"}},
            "outputs": {"text": {"type": "string"}, "result": {"type": "structured"}},
        },
        "boundary": {
            "id": "boundary",
            "type": "error_boundary",
            "target_node_id": "knowledge",
            "compensate_with": "python",
            "fallback_text": "fallback",
            "inputs": {"input": {"type": "string"}},
            "outputs": {"output": {"type": "string"}, "error": {"type": "structured"}},
        },
        "final": {
            "id": "final",
            "type": "output",
            "inputs": {"response": {"type": "string"}},
        },
    }
    manifest["edges"] = [
        {"id": "e_start_boundary", "from": "start", "to": "boundary", "map": {"msg": "input"}},
        {
            "id": "e_boundary_final",
            "from": "boundary",
            "to": "final",
            "map": {"output": "response"},
        },
    ]
    return manifest


def _error_boundary_external_app_recovery_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"] = {
        "start": {
            "id": "start",
            "type": "start",
            "outputs": {"msg": {"type": "string"}},
        },
        "external": {
            "id": "external",
            "type": "external_app",
            "entrypoint": "missing-module:handle",
            "inputs": {"input": {"type": "string"}},
            "outputs": {
                "text": {"type": "string"},
                "result": {"type": "structured"},
                "metadata": {"type": "structured"},
            },
        },
        "python": {
            "id": "python",
            "type": "python_code",
            "code": 'return {"text": f"recovered:{input or run_input}", "result": {"ok": True}}',
            "inputs": {"input": {"type": "string"}},
            "outputs": {"text": {"type": "string"}, "result": {"type": "structured"}},
        },
        "boundary": {
            "id": "boundary",
            "type": "error_boundary",
            "target_node_id": "external",
            "compensate_with": "python",
            "fallback_text": "fallback",
            "inputs": {"input": {"type": "string"}},
            "outputs": {"output": {"type": "string"}, "error": {"type": "structured"}},
        },
        "final": {
            "id": "final",
            "type": "output",
            "inputs": {"response": {"type": "string"}},
        },
    }
    manifest["edges"] = [
        {"id": "e_start_boundary", "from": "start", "to": "boundary", "map": {"msg": "input"}},
        {
            "id": "e_boundary_final",
            "from": "boundary",
            "to": "final",
            "map": {"output": "response"},
        },
    ]
    return manifest


def _error_boundary_tool_recovery_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"] = {
        "start": {
            "id": "start",
            "type": "start",
            "outputs": {"msg": {"type": "string"}},
        },
        "tool_lookup": {
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
        },
        "python": {
            "id": "python",
            "type": "python_code",
            "code": 'return {"text": f"recovered:{input or run_input}", "result": {"ok": True}}',
            "inputs": {"input": {"type": "string"}},
            "outputs": {"text": {"type": "string"}, "result": {"type": "structured"}},
        },
        "boundary": {
            "id": "boundary",
            "type": "error_boundary",
            "target_node_id": "tool_lookup",
            "compensate_with": "python",
            "fallback_text": "fallback",
            "inputs": {"input": {"type": "string"}},
            "outputs": {"output": {"type": "string"}, "error": {"type": "structured"}},
        },
        "final": {
            "id": "final",
            "type": "output",
            "inputs": {"response": {"type": "string"}},
        },
    }
    manifest["edges"] = [
        {"id": "e_start_boundary", "from": "start", "to": "boundary", "map": {"msg": "input"}},
        {
            "id": "e_boundary_final",
            "from": "boundary",
            "to": "final",
            "map": {"output": "response"},
        },
    ]
    manifest["tools"] = {
        "lookup_policy": {
            "registry_ref": "tool.lookup_policy.v1",
            "version_constraint": ">=1.0,<2.0",
        }
    }
    return manifest


def _error_boundary_mcp_resource_recovery_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"] = {
        "start": {
            "id": "start",
            "type": "start",
            "outputs": {"msg": {"type": "string"}},
        },
        "mcp_lookup": {
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
        },
        "python": {
            "id": "python",
            "type": "python_code",
            "code": 'return {"text": f"recovered:{input or run_input}", "result": {"ok": True}}',
            "inputs": {"input": {"type": "string"}},
            "outputs": {"text": {"type": "string"}, "result": {"type": "structured"}},
        },
        "boundary": {
            "id": "boundary",
            "type": "error_boundary",
            "target_node_id": "mcp_lookup",
            "compensate_with": "python",
            "fallback_text": "fallback",
            "inputs": {"input": {"type": "string"}},
            "outputs": {"output": {"type": "string"}, "error": {"type": "structured"}},
        },
        "final": {
            "id": "final",
            "type": "output",
            "inputs": {"response": {"type": "string"}},
        },
    }
    manifest["edges"] = [
        {"id": "e_start_boundary", "from": "start", "to": "boundary", "map": {"msg": "input"}},
        {
            "id": "e_boundary_final",
            "from": "boundary",
            "to": "final",
            "map": {"output": "response"},
        },
    ]
    return manifest


def _error_boundary_knowledge_build_recovery_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"] = {
        "start": {
            "id": "start",
            "type": "start",
            "outputs": {"msg": {"type": "string"}},
        },
        "knowledge_build": {
            "id": "knowledge_build",
            "type": "knowledge_build",
            "knowledge_base_id": "KB-1",
            "chunking_strategy": "recursive",
            "embedding_model": "BAAI/bge-m3",
        },
        "python": {
            "id": "python",
            "type": "python_code",
            "code": 'return {"text": f"recovered:{input or run_input}", "result": {"ok": True}}',
            "inputs": {"input": {"type": "string"}},
            "outputs": {"text": {"type": "string"}, "result": {"type": "structured"}},
        },
        "boundary": {
            "id": "boundary",
            "type": "error_boundary",
            "target_node_id": "knowledge_build",
            "compensate_with": "python",
            "fallback_text": "fallback",
            "inputs": {"input": {"type": "string"}},
            "outputs": {"output": {"type": "string"}, "error": {"type": "structured"}},
        },
        "final": {
            "id": "final",
            "type": "output",
            "inputs": {"response": {"type": "string"}},
        },
    }
    manifest["edges"] = [
        {"id": "e_start_boundary", "from": "start", "to": "boundary", "map": {"msg": "input"}},
        {
            "id": "e_boundary_final",
            "from": "boundary",
            "to": "final",
            "map": {"output": "response"},
        },
    ]
    return manifest


def _error_boundary_subworkflow_recovery_manifest(
    workflow_id: str,
    *,
    child_workflow_id: str,
) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"] = {
        "start": {
            "id": "start",
            "type": "start",
            "outputs": {"msg": {"type": "string"}},
        },
        "child_workflow": {
            "id": "child_workflow",
            "type": "subworkflow",
            "workflow_id": child_workflow_id,
            "alias": "manual",
            "inputs": {"input": {"type": "string"}},
            "outputs": {
                "output": {"type": "string"},
                "result": {"type": "structured"},
            },
        },
        "python": {
            "id": "python",
            "type": "python_code",
            "code": 'return {"text": f"recovered:{input or run_input}", "result": {"ok": True}}',
            "inputs": {"input": {"type": "string"}},
            "outputs": {"text": {"type": "string"}, "result": {"type": "structured"}},
        },
        "boundary": {
            "id": "boundary",
            "type": "error_boundary",
            "target_node_id": "child_workflow",
            "compensate_with": "python",
            "fallback_text": "fallback",
            "inputs": {"input": {"type": "string"}},
            "outputs": {"output": {"type": "string"}, "error": {"type": "structured"}},
        },
        "final": {
            "id": "final",
            "type": "output",
            "inputs": {"response": {"type": "string"}},
        },
    }
    manifest["edges"] = [
        {"id": "e_start_boundary", "from": "start", "to": "boundary", "map": {"msg": "input"}},
        {
            "id": "e_boundary_final",
            "from": "boundary",
            "to": "final",
            "map": {"output": "response"},
        },
    ]
    return manifest


def _error_boundary_python_recovery_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"] = {
        "start": {
            "id": "start",
            "type": "start",
            "outputs": {"msg": {"type": "string"}},
        },
        "python_fail": {
            "id": "python_fail",
            "type": "python_code",
            "code": 'raise ValueError(f"boom:{input or run_input}")',
            "inputs": {"input": {"type": "string"}},
            "outputs": {"text": {"type": "string"}},
        },
        "python_recover": {
            "id": "python_recover",
            "type": "python_code",
            "code": 'return {"text": f"recovered:{input or run_input}", "result": {"ok": True}}',
            "inputs": {"input": {"type": "string"}},
            "outputs": {"text": {"type": "string"}, "result": {"type": "structured"}},
        },
        "boundary": {
            "id": "boundary",
            "type": "error_boundary",
            "target_node_id": "python_fail",
            "compensate_with": "python_recover",
            "fallback_text": "fallback",
            "inputs": {"input": {"type": "string"}},
            "outputs": {"output": {"type": "string"}, "error": {"type": "structured"}},
        },
        "final": {
            "id": "final",
            "type": "output",
            "inputs": {"response": {"type": "string"}},
        },
    }
    manifest["edges"] = [
        {"id": "e_start_boundary", "from": "start", "to": "boundary", "map": {"msg": "input"}},
        {
            "id": "e_boundary_final",
            "from": "boundary",
            "to": "final",
            "map": {"output": "response"},
        },
    ]
    return manifest


def _error_boundary_agent_recovery_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"] = {
        "start": {
            "id": "start",
            "type": "start",
            "outputs": {"msg": {"type": "string"}},
        },
        "agent_fail": {
            "id": "agent_fail",
            "type": "agent",
            "name": "failing-agent",
            "model": "inherit",
            "instructions": {"type": "inline", "text": "You are helpful."},
            "tools": [],
            "inputs": {"input": {"type": "string"}},
            "outputs": {"final_output": {"type": "string"}},
        },
        "python_recover": {
            "id": "python_recover",
            "type": "python_code",
            "code": 'return {"text": f"recovered:{input or run_input}", "result": {"ok": True}}',
            "inputs": {"input": {"type": "string"}},
            "outputs": {"text": {"type": "string"}, "result": {"type": "structured"}},
        },
        "boundary": {
            "id": "boundary",
            "type": "error_boundary",
            "target_node_id": "agent_fail",
            "compensate_with": "python_recover",
            "fallback_text": "fallback",
            "inputs": {"input": {"type": "string"}},
            "outputs": {"output": {"type": "string"}, "error": {"type": "structured"}},
        },
        "final": {
            "id": "final",
            "type": "output",
            "inputs": {"response": {"type": "string"}},
        },
    }
    manifest["edges"] = [
        {"id": "e_start_boundary", "from": "start", "to": "boundary", "map": {"msg": "input"}},
        {
            "id": "e_boundary_final",
            "from": "boundary",
            "to": "final",
            "map": {"output": "response"},
        },
    ]
    return manifest


def _error_boundary_template_recovery_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"] = {
        "start": {
            "id": "start",
            "type": "start",
            "outputs": {"msg": {"type": "string"}},
        },
        "template_fail": {
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
        },
        "python_recover": {
            "id": "python_recover",
            "type": "python_code",
            "code": 'return {"text": f"recovered:{input or run_input}", "result": {"ok": True}}',
            "inputs": {"input": {"type": "string"}},
            "outputs": {"text": {"type": "string"}, "result": {"type": "structured"}},
        },
        "boundary": {
            "id": "boundary",
            "type": "error_boundary",
            "target_node_id": "template_fail",
            "compensate_with": "python_recover",
            "fallback_text": "fallback",
            "inputs": {"input": {"type": "string"}},
            "outputs": {"output": {"type": "string"}, "error": {"type": "structured"}},
        },
        "final": {
            "id": "final",
            "type": "output",
            "inputs": {"response": {"type": "string"}},
        },
    }
    manifest["edges"] = [
        {"id": "e_start_boundary", "from": "start", "to": "boundary", "map": {"msg": "input"}},
        {
            "id": "e_boundary_final",
            "from": "boundary",
            "to": "final",
            "map": {"output": "response"},
        },
    ]
    return manifest


def _error_boundary_template_compensation_failure_manifest(workflow_id: str) -> dict[str, object]:
    manifest = _error_boundary_template_recovery_manifest(workflow_id)
    nodes = manifest["nodes"]
    assert isinstance(nodes, dict)
    python_recover = nodes["python_recover"]
    assert isinstance(python_recover, dict)
    python_recover["code"] = 'raise ValueError(f"compensation boom:{input or run_input}")'
    return manifest


def _for_each_partial_failure_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"] = {
        "start": {
            "id": "start",
            "type": "start",
            "outputs": {"items": {"type": "structured"}},
        },
        "python": {
            "id": "python",
            "type": "python_code",
            "code": (
                "value = str(input or run_input)\n"
                'if value == "c":\n'
                '    raise ValueError("boom:c")\n'
                'return {"text": value, "result": {"seen": value}}'
            ),
            "inputs": {"input": {"type": "structured"}},
            "outputs": {"text": {"type": "string"}, "result": {"type": "structured"}},
        },
        "for_each": {
            "id": "for_each",
            "type": "for_each",
            "target_node_id": "python",
            "inputs": {"items": {"type": "structured"}},
            "outputs": {
                "results": {"type": "structured"},
                "text": {"type": "string"},
                "metadata": {"type": "structured"},
            },
        },
        "final": {
            "id": "final",
            "type": "output",
            "inputs": {"response": {"type": "string"}},
        },
    }
    manifest["edges"] = [
        {"id": "e_start_for_each", "from": "start", "to": "for_each", "map": {"items": "items"}},
        {"id": "e_start_python", "from": "start", "to": "python", "map": {"items": "input"}},
        {"id": "e_for_each_final", "from": "for_each", "to": "final", "map": {"text": "response"}},
    ]
    return manifest


def _for_each_agent_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"] = {
        "start": {
            "id": "start",
            "type": "start",
            "outputs": {"items": {"type": "structured"}},
        },
        "agent": {
            "id": "agent",
            "type": "agent",
            "name": "test-agent",
            "model": "inherit",
            "instructions": {"type": "inline", "text": "You are helpful."},
            "tools": [],
            "inputs": {"input": {"type": "string"}},
            "outputs": {"final_output": {"type": "string"}},
        },
        "for_each": {
            "id": "for_each",
            "type": "for_each",
            "target_node_id": "agent",
            "inputs": {"items": {"type": "structured"}},
            "outputs": {
                "results": {"type": "structured"},
                "text": {"type": "string"},
                "metadata": {"type": "structured"},
            },
        },
        "final": {
            "id": "final",
            "type": "output",
            "inputs": {"response": {"type": "string"}},
        },
    }
    manifest["edges"] = [
        {"id": "e_start_for_each", "from": "start", "to": "for_each", "map": {"items": "items"}},
        {"id": "e_for_each_final", "from": "for_each", "to": "final", "map": {"text": "response"}},
    ]
    return manifest


def _for_each_python_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"] = {
        "start": {
            "id": "start",
            "type": "start",
            "outputs": {"items": {"type": "structured"}},
        },
        "python": {
            "id": "python",
            "type": "python_code",
            "code": (
                'return {"text": str(input or run_input).upper(), "result": {"seen": input or run_input}}'
            ),
            "inputs": {"input": {"type": "string"}},
            "outputs": {"text": {"type": "string"}, "result": {"type": "structured"}},
        },
        "for_each": {
            "id": "for_each",
            "type": "for_each",
            "target_node_id": "python",
            "inputs": {"items": {"type": "structured"}},
            "outputs": {
                "results": {"type": "structured"},
                "text": {"type": "string"},
                "metadata": {"type": "structured"},
            },
        },
        "final": {
            "id": "final",
            "type": "output",
            "inputs": {"response": {"type": "string"}},
        },
    }
    manifest["edges"] = [
        {"id": "e_start_for_each", "from": "start", "to": "for_each", "map": {"items": "items"}},
        {"id": "e_for_each_final", "from": "for_each", "to": "final", "map": {"text": "response"}},
    ]
    return manifest


def _python_code_failure_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"]["python"] = {
        "id": "python",
        "type": "python_code",
        "code": 'raise ValueError("boom")',
        "timeout_seconds": 5,
        "inputs": {"input": {"type": "string"}},
        "outputs": {"text": {"type": "string"}},
    }
    manifest["edges"] = [
        {"id": "e_start_python", "from": "start", "to": "python", "map": {"msg": "input"}},
        {"id": "e_python_final", "from": "python", "to": "final", "map": {"text": "response"}},
    ]
    return manifest


def _python_code_success_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"]["python"] = {
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
    manifest["edges"] = [
        {"id": "e_start_python", "from": "start", "to": "python", "map": {"msg": "input"}},
        {"id": "e_python_final", "from": "python", "to": "final", "map": {"text": "response"}},
    ]
    return manifest


def _template_success_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"]["template"] = {
        "id": "template",
        "type": "template",
        "template": "Hello {{input}}",
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
    manifest["edges"] = [
        {"id": "e_start_template", "from": "start", "to": "template", "map": {"msg": "input"}},
        {"id": "e_template_final", "from": "template", "to": "final", "map": {"text": "response"}},
    ]
    return manifest


def _template_missing_variable_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"]["template"] = {
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
    manifest["edges"] = [
        {"id": "e_start_template", "from": "start", "to": "template", "map": {"msg": "input"}},
        {"id": "e_template_final", "from": "template", "to": "final", "map": {"text": "response"}},
    ]
    return manifest


def _wait_event_manifest(
    workflow_id: str,
    *,
    correlation_key: str = "",
    timeout_seconds: float | None = None,
) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["edges"] = [
        {"id": "e1", "from": "start", "to": "wait_gate", "map": {"msg": "input"}},
        {"id": "e2", "from": "wait_gate", "to": "final", "map": {"output": "response"}},
    ]
    nodes = manifest["nodes"]
    assert isinstance(nodes, dict)
    nodes["wait_gate"] = {
        "id": "wait_gate",
        "type": "wait_for_event",
        "event_name": "ticket.approved",
        "correlation_key": correlation_key,
        "timeout_seconds": timeout_seconds,
        "inputs": {"input": {"type": "string"}},
        "outputs": {
            "output": {"type": "string"},
            "event_payload": {"type": "structured"},
            "event_name": {"type": "string"},
        },
    }
    return manifest


def _wait_until_manifest(
    workflow_id: str,
    *,
    wait_until: str = "2099-01-01T00:00:00Z",
    timezone_name: str = "UTC",
) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["edges"] = [
        {"id": "e1", "from": "start", "to": "wait_gate", "map": {"msg": "input"}},
        {"id": "e2", "from": "wait_gate", "to": "final", "map": {"output": "response"}},
    ]
    nodes = manifest["nodes"]
    assert isinstance(nodes, dict)
    nodes["wait_gate"] = {
        "id": "wait_gate",
        "type": "wait_until",
        "wait_until": wait_until,
        "timezone": timezone_name,
        "inputs": {"input": {"type": "string"}},
        "outputs": {"output": {"type": "string"}},
    }
    return manifest


def _wait_event_payload_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["edges"] = [
        {"id": "e1", "from": "start", "to": "wait_gate", "map": {"msg": "input"}},
        {
            "id": "e2",
            "from": "wait_gate",
            "to": "render_event",
            "map": {"event_payload": "payload"},
        },
        {
            "id": "e3",
            "from": "wait_gate",
            "to": "render_event",
            "map": {"event_name": "event_name"},
        },
        {"id": "e4", "from": "render_event", "to": "final", "map": {"text": "response"}},
    ]
    nodes = manifest["nodes"]
    assert isinstance(nodes, dict)
    nodes["wait_gate"] = {
        "id": "wait_gate",
        "type": "wait_for_event",
        "event_name": "ticket.approved",
        "inputs": {"input": {"type": "string"}},
        "outputs": {
            "output": {"type": "string"},
            "event_payload": {"type": "structured"},
            "event_name": {"type": "string"},
        },
    }
    nodes["render_event"] = {
        "id": "render_event",
        "type": "python_code",
        "code": (
            'payload = inputs.get("payload") or {}\n'
            'event_name = inputs.get("event_name") or ""\n'
            "return {\"text\": f\"{event_name}::{payload.get('ticket_id')}::{payload.get('approved')}\"}"
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
    return manifest


def _parallel_join_any_wait_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"] = {
        "start": {
            "id": "start",
            "type": "start",
            "outputs": {"msg": {"type": "string"}},
        },
        "parallel": {
            "id": "parallel",
            "type": "parallel",
            "inputs": {"input": {"type": "string"}},
            "outputs": {"output": {"type": "string"}},
        },
        "wait_event": {
            "id": "wait_event",
            "type": "wait_for_event",
            "event_name": "resume_event",
            "inputs": {"input": {"type": "string"}},
            "outputs": {"output": {"type": "string"}},
        },
        "agent": {
            "id": "agent",
            "type": "agent",
            "name": "test-agent",
            "model": "inherit",
            "instructions": {"type": "inline", "text": "You are helpful."},
            "tools": [],
            "inputs": {"input": {"type": "string"}},
            "outputs": {"final_output": {"type": "string"}},
        },
        "join_any": {
            "id": "join_any",
            "type": "join",
            "mode": "any",
            "inputs": {"left": {"type": "string"}, "right": {"type": "string"}},
            "outputs": {"output": {"type": "string"}},
        },
        "final": {
            "id": "final",
            "type": "output",
            "inputs": {"response": {"type": "string"}},
        },
    }
    manifest["edges"] = [
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
    return manifest


def _parallel_join_any_failure_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"] = {
        "start": {
            "id": "start",
            "type": "start",
            "outputs": {"msg": {"type": "string"}},
        },
        "parallel": {
            "id": "parallel",
            "type": "parallel",
            "inputs": {"input": {"type": "string"}},
            "outputs": {"output": {"type": "string"}},
        },
        "good_python": {
            "id": "good_python",
            "type": "python_code",
            "code": 'return {"text": inputs.get("input", "")}',
            "timeout_seconds": 5,
            "inputs": {"input": {"type": "string"}},
            "outputs": {"text": {"type": "string"}},
        },
        "bad_python": {
            "id": "bad_python",
            "type": "python_code",
            "code": 'raise ValueError("boom")',
            "timeout_seconds": 5,
            "inputs": {"input": {"type": "string"}},
            "outputs": {"text": {"type": "string"}},
        },
        "join_any": {
            "id": "join_any",
            "type": "join",
            "mode": "any",
            "inputs": {"left": {"type": "string"}, "right": {"type": "string"}},
            "outputs": {"output": {"type": "string"}},
        },
        "final": {
            "id": "final",
            "type": "output",
            "inputs": {"response": {"type": "string"}},
        },
    }
    manifest["edges"] = [
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
        {"id": "e_good_join", "from": "good_python", "to": "join_any", "map": {"text": "left"}},
        {"id": "e_bad_join", "from": "bad_python", "to": "join_any", "map": {"text": "right"}},
        {"id": "e_join_final", "from": "join_any", "to": "final", "map": {"output": "response"}},
    ]
    return manifest


def _parallel_join_all_failure_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"] = {
        "start": {
            "id": "start",
            "type": "start",
            "outputs": {"msg": {"type": "string"}},
        },
        "parallel": {
            "id": "parallel",
            "type": "parallel",
            "inputs": {"input": {"type": "string"}},
            "outputs": {"output": {"type": "string"}},
        },
        "good_python": {
            "id": "good_python",
            "type": "python_code",
            "code": 'return {"text": inputs.get("input", "")}',
            "timeout_seconds": 5,
            "inputs": {"input": {"type": "string"}},
            "outputs": {"text": {"type": "string"}},
        },
        "bad_python": {
            "id": "bad_python",
            "type": "python_code",
            "code": 'raise ValueError("boom")',
            "timeout_seconds": 5,
            "inputs": {"input": {"type": "string"}},
            "outputs": {"text": {"type": "string"}},
        },
        "join_all": {
            "id": "join_all",
            "type": "join",
            "mode": "all",
            "inputs": {"left": {"type": "string"}, "right": {"type": "string"}},
            "outputs": {"output": {"type": "string"}, "merged": {"type": "structured"}},
        },
        "final": {
            "id": "final",
            "type": "output",
            "inputs": {"response": {"type": "string"}},
        },
    }
    manifest["edges"] = [
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
        {"id": "e_good_join", "from": "good_python", "to": "join_all", "map": {"text": "left"}},
        {"id": "e_bad_join", "from": "bad_python", "to": "join_all", "map": {"text": "right"}},
        {"id": "e_join_final", "from": "join_all", "to": "final", "map": {"output": "response"}},
    ]
    return manifest


def _parallel_join_all_wait_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"] = {
        "start": {
            "id": "start",
            "type": "start",
            "outputs": {"msg": {"type": "string"}},
        },
        "parallel": {
            "id": "parallel",
            "type": "parallel",
            "inputs": {"input": {"type": "string"}},
            "outputs": {"output": {"type": "string"}},
        },
        "wait_event": {
            "id": "wait_event",
            "type": "wait_for_event",
            "event_name": "resume_event",
            "inputs": {"input": {"type": "string"}},
            "outputs": {"output": {"type": "string"}},
        },
        "agent": {
            "id": "agent",
            "type": "agent",
            "name": "test-agent",
            "model": "inherit",
            "instructions": {"type": "inline", "text": "You are helpful."},
            "tools": [],
            "inputs": {"input": {"type": "string"}},
            "outputs": {"final_output": {"type": "string"}},
        },
        "join_all": {
            "id": "join_all",
            "type": "join",
            "mode": "all",
            "inputs": {"left": {"type": "string"}, "right": {"type": "string"}},
            "outputs": {"output": {"type": "string"}, "merged": {"type": "structured"}},
        },
        "final": {
            "id": "final",
            "type": "output",
            "inputs": {"response": {"type": "string"}},
        },
    }
    manifest["edges"] = [
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
    return manifest


def _router_branch_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"] = {
        "start": {
            "id": "start",
            "type": "start",
            "outputs": {"msg": {"type": "string"}},
        },
        "router": {
            "id": "router",
            "type": "router",
            "inputs": {"input": {"type": "string"}},
            "outputs": {"route": {"type": "string"}},
            "branches": [
                {"condition": {"op": "contains", "value": "refund"}, "to": "agent"},
                {"to": "final"},
            ],
        },
        "agent": {
            "id": "agent",
            "type": "agent",
            "name": "test-agent",
            "model": "inherit",
            "instructions": {"type": "inline", "text": "You are helpful."},
            "tools": [],
            "inputs": {"input": {"type": "string"}},
            "outputs": {"final_output": {"type": "string"}},
        },
        "final": {
            "id": "final",
            "type": "output",
            "inputs": {"response": {"type": "string"}},
        },
    }
    manifest["edges"] = [
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
    return manifest


def _router_without_branches_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"] = {
        "start": {
            "id": "start",
            "type": "start",
            "outputs": {"msg": {"type": "string"}},
        },
        "router": {
            "id": "router",
            "type": "router",
            "inputs": {"input": {"type": "string"}},
            "outputs": {"route": {"type": "string"}},
            "branches": [],
        },
        "final": {
            "id": "final",
            "type": "output",
            "inputs": {"response": {"type": "string"}},
        },
    }
    manifest["edges"] = [
        {"id": "e_start_router", "from": "start", "to": "router", "map": {"msg": "input"}},
        {"id": "e_router_final", "from": "router", "to": "final", "map": {"route": "response"}},
    ]
    return manifest


def _build_worker(client) -> WorkflowRunWorker:
    return WorkflowRunWorker(
        session_factory=client.app.state.session_factory,
        config=client.app.state.config,
        event_bus=getattr(client.app.state, "event_bus", None),
    )


def _persistent_session_memory_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(
        workflow_id,
        runtime={
            "sdk": "openai-agents-python",
            "sdk_version_policy": "runtime-pinned",
            "compiler_version": "caliber-workflow-compiler-v1",
            "default_model_ref": "CALIBER_WORKFLOW_DEFAULT_MODEL",
            "session": {"type": "persistent"},
        },
    )
    nodes = manifest["nodes"]
    assert isinstance(nodes, dict)
    agent = nodes["agent"]
    assert isinstance(agent, dict)
    agent["outputs"] = {
        "final_output": {"type": "string"},
        "history": {"type": "structured"},
    }
    return manifest


def _install_openai_mode_runtime(
    client,
    monkeypatch: pytest.MonkeyPatch,
    *,
    workflow_api: str,
    tool_name: str = "lookup_policy",
    tool_arguments: dict[str, object] | None = None,
    tool_turns: list[dict[str, object]] | None = None,
    payload: dict[str, object] | None = None,
    response_repetitions: int = 1,
) -> tuple[SimpleNamespace, dict[str, object]]:
    payload = payload or {"answer": "Refunds follow the 30 day policy.", "grounded": True}
    tool_arguments = tool_arguments or {"query": "refund"}
    raw_tool_turns = tool_turns or [tool_arguments]
    normalized_tool_turns: list[tuple[str, dict[str, object]]] = []
    for turn in raw_tool_turns:
        turn_copy = dict(turn)
        current_tool_name = str(turn_copy.pop("tool_name", tool_name))
        normalized_tool_turns.append((current_tool_name, turn_copy))
    client.app.state.config = client.app.state.config.model_copy(
        update={
            "workflow_run_queue_enabled": True,
            "workflow_run_runtime_approvals_enabled": True,
            "workflow_run_checkpointing_enabled": True,
            "llm_provider": "openai",
            "llm_api_key_env": "OPENAI_API_KEY",
            "llm_diagnosis_model": "gpt-4.1-mini",
            "openai_workflow_api": workflow_api,
        }
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")

    if workflow_api == "chat_completions":
        chat_responses: list[object] = []
        for repetition_index in range(response_repetitions):
            repetition_prefix = repetition_index + 1
            chat_responses.extend(
                [
                    _tool_call_resp(
                        f"call-{repetition_prefix}-{index}",
                        current_tool_name,
                        turn,
                    )
                    for index, (current_tool_name, turn) in enumerate(
                        normalized_tool_turns, start=1
                    )
                ]
            )
            chat_responses.append(_final_resp(json.dumps(payload)))
        sdk_client = _FakeOpenAI(chat_responses)
        openai_mod = types.ModuleType("openai")
        openai_mod.OpenAI = lambda **kwargs: sdk_client
        monkeypatch.setitem(sys.modules, "openai", openai_mod)
        tracker = SimpleNamespace(
            call_count=lambda: len(sdk_client.chat.completions.calls),
            expected_initial_calls=len(normalized_tool_turns) + 1,
        )
        return tracker, payload

    if workflow_api == "responses":
        response_turns: list[object] = []
        for repetition_index in range(response_repetitions):
            repetition_prefix = repetition_index + 1
            response_turns.extend(
                [
                    _responses_tool_call_resp(
                        f"resp-{repetition_prefix}-{index}",
                        f"call-{repetition_prefix}-{index}",
                        current_tool_name,
                        turn,
                    )
                    for index, (current_tool_name, turn) in enumerate(
                        normalized_tool_turns, start=1
                    )
                ]
            )
            response_turns.append(
                _responses_final_resp(
                    f"resp-{repetition_prefix}-{len(normalized_tool_turns) + 1}",
                    json.dumps(payload),
                )
            )
        sdk_client = _FakeOpenAIResponses(response_turns)
        openai_mod = types.ModuleType("openai")
        openai_mod.OpenAI = lambda **kwargs: sdk_client
        monkeypatch.setitem(sys.modules, "openai", openai_mod)
        tracker = SimpleNamespace(
            call_count=lambda: len(sdk_client.responses.calls),
            expected_initial_calls=len(normalized_tool_turns) + 1,
        )
        return tracker, payload

    if workflow_api == "agents_sdk":

        def _handler(agent, input, **kwargs):
            del input, kwargs
            tools_by_name = {
                str(getattr(candidate, "name", "")): candidate for candidate in agent.tools
            }
            for current_tool_name, turn in normalized_tool_turns:
                tool = tools_by_name[current_tool_name]
                tool_output = asyncio.run(tool.on_invoke_tool(None, json.dumps(turn)))
                parsed_tool_output = json.loads(tool_output)
                if parsed_tool_output.get("_gated") is True:
                    assert parsed_tool_output["tool"] == current_tool_name
                else:
                    for key, value in turn.items():
                        if key in parsed_tool_output:
                            assert parsed_tool_output[key] == value
            return SimpleNamespace(
                final_output=json.dumps(payload),
                raw_responses=[_AToolResp(json.dumps(payload), (5, 6, 11))],
                last_agent=agent,
            )

        _fake_agent, fake_runner, _fake_provider, _ = _install_agents_workflow_sdk(
            monkeypatch,
            handler=_handler,
        )
        tracker = SimpleNamespace(
            call_count=lambda: len(fake_runner.calls),
            expected_initial_calls=1,
        )
        return tracker, payload

    raise AssertionError(f"unexpected workflow api {workflow_api!r}")


def _assert_openai_waiting_agent_step(
    waiting_data: dict[str, object],
    *,
    prompt_version: str,
    expected_tool_calls: list[dict[str, object]],
    payload: dict[str, object],
) -> None:
    assert waiting_data["status"] == "waiting_approval"
    agent_step = next(
        step for step in waiting_data["summary"]["steps"] if step["node_id"] == "agent"
    )
    assert agent_step["prompt_version"] == prompt_version
    assert agent_step["tool_calls"] == expected_tool_calls
    assert agent_step["output_by_port"]["structured_output"] == payload


def _assert_openai_approval_resume_completion(
    client,
    *,
    run_id: str,
    payload: dict[str, object],
) -> None:
    _assert_openai_approval_resume_completion_with_worker(
        client,
        worker=_build_worker(client),
        run_id=run_id,
        payload=payload,
    )


def _assert_openai_approval_resume_completion_with_worker(
    client,
    *,
    worker,
    run_id: str,
    payload: dict[str, object],
) -> None:
    _approve_and_resume_workflow_run(client, run_id=run_id, reason="approved by test")

    worker._tick()

    final = client.get(f"{PREFIX}/workflow-runs/{run_id}")
    assert final.status_code == 200
    final_data = final.json()["data"]
    assert final_data["status"] == "completed"
    assert json.loads(final_data["summary"]["output"]) == payload


def _approve_and_resume_workflow_run(
    client,
    *,
    run_id: str,
    reason: str,
) -> None:
    approvals = client.get(f"{PREFIX}/workflow-runs/{run_id}/approvals")
    assert approvals.status_code == 200
    approval_id = approvals.json()["data"][0]["runtime_approval_id"]
    approved = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/approval/approve",
        json={"runtime_approval_id": approval_id, "reason": reason},
    )
    assert approved.status_code == 200
    resumed = client.post(f"{PREFIX}/workflow-runs/{run_id}/resume")
    assert resumed.status_code == 202


def _assert_openai_worker_step_event_counts(client, *, run_id: str) -> None:
    with client.app.state.session_factory() as session:
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .filter(CaliberWorkflowRunEvent.event_type == "workflow.run.step")
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        agent_steps = [event for event in events if event.node_id == "agent"]
        human_gate_steps = [event for event in events if event.node_id == "human_gate"]
        final_steps = [event for event in events if event.node_id == "final"]
        assert len(agent_steps) == 1
        assert len(human_gate_steps) == 2
        assert len(final_steps) == 1


def _assert_openai_worker_lifecycle_event_history(client, *, run_id: str) -> None:
    with client.app.state.session_factory() as session:
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        event_types = [event.event_type for event in events]
        assert event_types[-1] == "workflow.run.completed"
        assert event_types.count("workflow.run.started") == 2
        assert event_types.count("workflow.run.waiting_approval") == 1
        assert event_types.count("workflow.run.completed") == 1
        assert event_types.count("workflow.run.step") >= 4
        assert "workflow.run.failed" not in event_types

        first_started_index = event_types.index("workflow.run.started")
        first_waiting_index = event_types.index("workflow.run.waiting_approval")
        second_started_index = event_types.index("workflow.run.started", first_started_index + 1)
        completed_index = event_types.index("workflow.run.completed")
        assert first_started_index < first_waiting_index < second_started_index < completed_index

        node_started_events = [
            event for event in events if event.event_type == "workflow.run.node_started"
        ]
        step_events = [event for event in events if event.event_type == "workflow.run.step"]
        node_started_node_ids = [event.node_id for event in node_started_events]
        step_node_ids = [event.node_id for event in step_events]
        assert len(node_started_events) >= len(step_events)
        assert node_started_node_ids.count("agent") == 1
        assert node_started_node_ids.count("human_gate") == 2
        assert node_started_node_ids.count("final") == 1
        assert step_node_ids.count("agent") == 1
        assert step_node_ids.count("human_gate") == 2
        assert step_node_ids.count("final") == 1


def _expected_openai_mixed_failsoft_tool_calls(
    *,
    outage_immediate_attempt: int,
    outage_retry_attempt: int,
) -> list[dict[str, object]]:
    return [
        {
            "tool": "lookup_policy",
            "result": {
                "policy": "Purchases within 30 days are eligible for a full refund.",
                "query": "refund",
            },
        },
        {
            "tool": "escalate",
            "result": {
                "_gated": True,
                "tool": "escalate",
                "reason": "tool requires approval; not executed in an autonomous run",
            },
        },
        {
            "tool": "lookup_policy",
            "result": {
                "_error": (
                    "ToolExecutionError: tool 'lookup_policy' failed after 1 attempt(s): "
                    f"policy backend failed attempt {outage_immediate_attempt} for outage-immediate"
                ),
                "tool": "lookup_policy",
            },
        },
        {
            "tool": "lookup_policy_retry",
            "result": {
                "_error": (
                    "ToolExecutionError: tool 'lookup_policy_retry' failed after 3 attempt(s): "
                    f"policy backend failed attempt {outage_retry_attempt} for outage-retry"
                ),
                "tool": "lookup_policy_retry",
            },
        },
        {
            "tool": "lookup_policy",
            "result": {
                "policy": "Purchases within 30 days are eligible for a full refund.",
                "query": "shipping",
            },
        },
    ]


def test_worker_executes_queued_run_to_completion(client) -> None:
    _enable_queue(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        version = session.get(CaliberWorkflowVersion, vid)
        assert run is not None
        assert version is not None
        assert run.status == "completed"
        assert run.completed_at is not None
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        event_types = [event.event_type for event in events]
        assert "workflow.run.started" in event_types
        assert "workflow.run.node_started" in event_types
        assert "workflow.run.step" in event_types
        assert event_types[-1] == "workflow.run.completed"
        node_started_index = event_types.index("workflow.run.node_started")
        step_index = event_types.index("workflow.run.step")
        assert node_started_index < step_index
        node_started_event = next(
            event
            for event in events
            if event.event_type == "workflow.run.node_started" and event.node_id == "support_agent"
        )
        node_started_payload = dict(node_started_event.payload or {})
        assert node_started_payload["node_id"] == "support_agent"
        assert node_started_payload["node_type"] == "agent"
        agent_step_event = next(
            event
            for event in events
            if event.event_type == "workflow.run.step" and event.node_id == "support_agent"
        )
        payload = dict(agent_step_event.payload or {}).get("step")
        assert isinstance(payload, dict)
        assert payload.get("input_by_port") == {"input": "hello"}
        output_by_port = payload.get("output_by_port")
        assert isinstance(output_by_port, dict)
        final_output = output_by_port.get("final_output")
        assert isinstance(final_output, str)
        assert "hello" in final_output
        summary = dict(run.summary or {})
        assert summary["manifest_mode"] == "saved_version"
        assert summary["manifest_hash"] == version.manifest_hash
        assert summary["workflow_version_number"] == version.version_number
        summary_steps = summary.get("steps")
        assert isinstance(summary_steps, list)
        assert any(
            isinstance(step, dict)
            and step.get("node_id") == "support_agent"
            and step.get("input_by_port") == {"input": "hello"}
            and isinstance(step.get("output_by_port"), dict)
            and isinstance(step["output_by_port"].get("final_output"), str)
            and "hello" in step["output_by_port"].get("final_output", "")
            for step in summary_steps
        )


def test_worker_executes_start_to_output_passthrough_run(client) -> None:
    _enable_queue(client)
    workflow_id = "passthrough-worker-wf"
    manifest = make_manifest(workflow_id)
    del manifest["nodes"]["agent"]
    manifest["edges"] = [
        {"id": "e_start_final", "from": "start", "to": "final", "map": {"msg": "response"}}
    ]
    _wid, vid = create_and_publish(
        client,
        workflow_name="Passthrough Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.completed_at is not None
        assert run.error_code is None
        assert run.summary is not None
        assert run.summary.get("output") == "hello"
        summary_steps = run.summary.get("steps")
        assert isinstance(summary_steps, list)
        assert [step.get("node_id") for step in summary_steps if isinstance(step, dict)] == [
            "start",
            "final",
        ]

        final_step = next(
            step
            for step in summary_steps
            if isinstance(step, dict) and step.get("node_id") == "final"
        )
        assert final_step.get("node_type") == "output"
        assert final_step.get("output") == "hello"

        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.completed"
        assert any(
            event.event_type == "workflow.run.step" and event.node_id == "final" for event in events
        )


def test_worker_executes_multi_hop_agent_handoff_path_to_completion(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from caliber.workflows import runtime as workflow_runtime

    _enable_queue(client)
    workflow_id = "worker-handoff-wf"
    manifest = _multi_hop_handoff_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Worker Handoff Workflow",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hi"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    def _execute_with_fake_handoffs(plan, input_text: str, *, executor, **kwargs):
        del executor
        return workflow_runtime.execute(
            plan,
            input_text,
            executor=FakeWorkflowExecutor(),
            **kwargs,
        )

    monkeypatch.setattr(
        "caliber.orchestrator.workflow_run_worker.execute",
        _execute_with_fake_handoffs,
    )

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.completed_at is not None
        assert dict(run.summary or {}).get("output") == "[approvals-agent] processed: hi"
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        node_started_ids = [
            event.node_id for event in events if event.event_type == "workflow.run.node_started"
        ]
        assert node_started_ids[:3] == ["start", "agent", "final"]
        step_payloads = [
            dict(event.payload or {}).get("step")
            for event in events
            if event.event_type == "workflow.run.step"
        ]
        assert any(
            isinstance(payload, dict)
            and payload.get("node_id") == "agent"
            and payload.get("handoff_target") == "billing"
            and isinstance(payload.get("output_by_port"), dict)
            and payload["output_by_port"].get("final_output") == "[approvals-agent] processed: hi"
            for payload in step_payloads
        )
        summary = dict(run.summary or {})
        summary_steps = summary.get("steps")
        assert isinstance(summary_steps, list)
        assert any(
            isinstance(step, dict)
            and step.get("node_id") == "agent"
            and step.get("handoff_target") == "billing"
            and isinstance(step.get("output_by_port"), dict)
            and step["output_by_port"].get("final_output") == "[approvals-agent] processed: hi"
            for step in summary_steps
        )


def test_worker_executes_handoff_input_filter_path_to_completion(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from caliber.workflows import runtime as workflow_runtime

    _enable_queue(client)
    workflow_id = "worker-handoff-filter-wf"
    manifest = _handoff_input_filter_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Worker Handoff Filter Workflow",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "refund"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    calls: list[str] = []
    input_calls: list[str] = []
    history_calls: list[list[dict[str, str]]] = []

    class _NoSelectionHandoffExecutor:
        def run_agent(
            self,
            agent,
            input_text: str,
            *,
            history: list[dict[str, str]] | None = None,
            tool_callables,
            preview,
        ):
            del tool_callables, preview
            calls.append(agent.node_id)
            input_calls.append(input_text)
            history_calls.append([dict(item) for item in (history or [])])
            return workflow_runtime.AgentTurnResult(
                final_output=f"[{agent.node_id}] processed: {input_text}",
                tokens=len(input_text.split()) + 1,
            )

    def _execute_with_handoff_filter(plan, input_text: str, *, executor, **kwargs):
        del executor
        return workflow_runtime.execute(
            plan,
            input_text,
            executor=_NoSelectionHandoffExecutor(),
            **kwargs,
        )

    monkeypatch.setattr(
        "caliber.orchestrator.workflow_run_worker.execute",
        _execute_with_handoff_filter,
    )

    worker = _build_worker(client)
    worker._tick()

    expected_filtered_input = "Billing summary for refund\nAgent said: [agent] processed: refund"

    assert calls == ["agent", "billing"]
    assert input_calls == ["refund", expected_filtered_input]
    assert history_calls == [[], []]

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.completed_at is not None
        assert (
            dict(run.summary or {}).get("output")
            == f"[billing] processed: {expected_filtered_input}"
        )
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        step_payloads = [
            dict(event.payload or {}).get("step")
            for event in events
            if event.event_type == "workflow.run.step"
        ]
        assert any(
            isinstance(payload, dict)
            and payload.get("node_id") == "agent"
            and payload.get("handoff_target") == "billing"
            and isinstance(payload.get("output_by_port"), dict)
            and payload["output_by_port"].get("final_output")
            == f"[billing] processed: {expected_filtered_input}"
            for payload in step_payloads
        )
        summary_steps = dict(run.summary or {}).get("steps")
        assert isinstance(summary_steps, list)
        assert any(
            isinstance(step, dict)
            and step.get("node_id") == "agent"
            and step.get("handoff_target") == "billing"
            and isinstance(step.get("output_by_port"), dict)
            and step["output_by_port"].get("final_output")
            == f"[billing] processed: {expected_filtered_input}"
            for step in summary_steps
        )


def test_worker_persists_guardrail_block_failures_for_queued_runs(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from caliber.workflows import runtime as workflow_runtime

    _enable_queue(client)
    workflow_id = "guardrail-block-worker-wf"
    manifest = make_support_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Guardrail Block Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "what is your refund policy?"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    def _execute_with_ungrounded_agent(plan, input_text: str, *, executor, **kwargs):
        del executor
        return workflow_runtime.execute(
            plan,
            input_text,
            executor=FakeWorkflowExecutor(skip_tools=True),
            **kwargs,
        )

    monkeypatch.setattr(
        "caliber.orchestrator.workflow_run_worker.execute",
        _execute_with_ungrounded_agent,
    )

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.error_code == "runtime_error"
        assert run.error_summary is not None
        assert "without calling required tool 'lookup_policy'" in run.error_summary
        assert run.completed_at is not None
        assert run.summary is not None
        summary = dict(run.summary or {})
        assert summary.get("error") == run.error_summary
        guardrail_results = summary.get("guardrail_results")
        assert isinstance(guardrail_results, list)
        assert any(
            isinstance(result, dict)
            and result.get("node_id") == "policy_guardrail"
            and result.get("kind") == "tool_required_before_claim"
            and result.get("passed") is False
            and "without calling required tool 'lookup_policy'" in str(result.get("reason", ""))
            for result in guardrail_results
        )
        summary_steps = summary.get("steps")
        assert isinstance(summary_steps, list)
        assert any(
            isinstance(step, dict)
            and step.get("node_id") == "support_agent"
            and step.get("status") == "ok"
            for step in summary_steps
        )
        assert any(
            isinstance(step, dict)
            and step.get("node_id") == "policy_guardrail"
            and step.get("status") == "blocked"
            and "without calling required tool 'lookup_policy'" in str(step.get("detail", ""))
            for step in summary_steps
        )
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.failed"
        assert "without calling required tool 'lookup_policy'" in str(
            events[-1].payload.get("error", "")
        )


def test_worker_retries_guardrail_block_failures_before_failing_queued_runs(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from caliber.workflows import runtime as workflow_runtime

    _enable_queue(client)
    workflow_id = "guardrail-block-retry-worker-wf"
    manifest = make_support_manifest(workflow_id)
    policy_guardrail = manifest["nodes"]["policy_guardrail"]
    assert isinstance(policy_guardrail, dict)
    policy_guardrail["on_failure"] = "block_retry"
    policy_guardrail["max_retries"] = 1
    _wid, vid = create_and_publish(
        client,
        workflow_name="Guardrail Block Retry Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "what is your refund policy?"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    def _execute_with_ungrounded_agent(plan, input_text: str, *, executor, **kwargs):
        del executor
        return workflow_runtime.execute(
            plan,
            input_text,
            executor=FakeWorkflowExecutor(skip_tools=True),
            **kwargs,
        )

    monkeypatch.setattr(
        "caliber.orchestrator.workflow_run_worker.execute",
        _execute_with_ungrounded_agent,
    )

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.error_code == "runtime_error"
        assert run.error_summary is not None
        assert "without calling required tool 'lookup_policy'" in run.error_summary
        assert run.completed_at is not None
        assert run.summary is not None
        summary_steps = run.summary.get("steps")
        assert isinstance(summary_steps, list)
        assert [step.get("node_id") for step in summary_steps if isinstance(step, dict)].count(
            "support_agent"
        ) == 2
        assert [step.get("node_id") for step in summary_steps if isinstance(step, dict)].count(
            "policy_guardrail"
        ) == 2
        guardrail_results = run.summary.get("guardrail_results")
        assert isinstance(guardrail_results, list)
        assert [
            result.get("node_id") for result in guardrail_results if isinstance(result, dict)
        ].count("policy_guardrail") == 2
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.failed"
        assert "without calling required tool 'lookup_policy'" in str(
            events[-1].payload.get("error", "")
        )


def test_worker_completes_passing_guardrail_runs(client) -> None:
    _enable_queue(client)
    workflow_id = "guardrail-pass-worker-wf"
    manifest = make_support_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Guardrail Pass Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "refund?"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.error_code is None
        assert run.completed_at is not None
        assert run.summary is not None
        assert run.summary.get("output")

        summary = dict(run.summary or {})
        guardrail_results = summary.get("guardrail_results")
        assert isinstance(guardrail_results, list)
        assert any(
            isinstance(result, dict)
            and result.get("node_id") == "policy_guardrail"
            and result.get("kind") == "tool_required_before_claim"
            and result.get("passed") is True
            for result in guardrail_results
        )

        summary_steps = summary.get("steps")
        assert isinstance(summary_steps, list)
        guardrail_step = next(
            step
            for step in summary_steps
            if isinstance(step, dict) and step.get("node_id") == "policy_guardrail"
        )
        assert guardrail_step.get("status") == "ok"
        assert guardrail_step.get("node_type") == "guardrail"
        output_by_port = guardrail_step.get("output_by_port")
        assert isinstance(output_by_port, dict)
        assert output_by_port.get("passthrough") == run.summary.get("output")

        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        event_types = [event.event_type for event in events]
        assert event_types[-1] == "workflow.run.completed"
        assert "workflow.run.failed" not in event_types


@pytest.mark.parametrize(
    ("workflow_api", "prompt_version"),
    [
        ("chat_completions", "openai"),
        ("responses", "openai_responses"),
        ("agents_sdk", "openai_agents"),
    ],
)
def test_worker_openai_modes_execute_structured_agent_before_approval_and_resume_without_rerun(
    client,
    monkeypatch: pytest.MonkeyPatch,
    workflow_api: str,
    prompt_version: str,
) -> None:
    register_demo_tools(client)
    tracker, payload = _install_openai_mode_runtime(
        client,
        monkeypatch,
        workflow_api=workflow_api,
    )
    workflow_id = f"openai-{workflow_api}-approval-worker-wf"
    manifest = _openai_structured_approval_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name=f"OpenAI {workflow_api} Approval Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "what is the refund policy?"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    waiting = client.get(f"{PREFIX}/workflow-runs/{run_id}")
    assert waiting.status_code == 200
    waiting_data = waiting.json()["data"]
    assert waiting_data["status"] == "waiting_approval"
    assert tracker.call_count() == tracker.expected_initial_calls

    agent_step = next(
        step for step in waiting_data["summary"]["steps"] if step["node_id"] == "agent"
    )
    assert agent_step["prompt_version"] == prompt_version
    assert agent_step["tool_calls"] == [
        {
            "tool": "lookup_policy",
            "result": {
                "policy": "Purchases within 30 days are eligible for a full refund.",
                "query": "refund",
            },
        }
    ]
    assert agent_step["output_by_port"]["structured_output"] == payload

    approvals = client.get(f"{PREFIX}/workflow-runs/{run_id}/approvals")
    assert approvals.status_code == 200
    approval_id = approvals.json()["data"][0]["runtime_approval_id"]
    approved = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/approval/approve",
        json={"runtime_approval_id": approval_id, "reason": "approved by test"},
    )
    assert approved.status_code == 200
    resumed = client.post(f"{PREFIX}/workflow-runs/{run_id}/resume")
    assert resumed.status_code == 202

    worker._tick()

    final = client.get(f"{PREFIX}/workflow-runs/{run_id}")
    assert final.status_code == 200
    final_data = final.json()["data"]
    assert final_data["status"] == "completed"
    assert json.loads(final_data["summary"]["output"]) == payload
    assert tracker.call_count() == tracker.expected_initial_calls


@pytest.mark.parametrize(
    ("workflow_api", "prompt_version"),
    [
        ("chat_completions", "openai"),
        ("responses", "openai_responses"),
        ("agents_sdk", "openai_agents"),
    ],
)
def test_worker_openai_modes_preserve_gated_tool_calls_across_approval_resume_without_rerun(
    client,
    monkeypatch: pytest.MonkeyPatch,
    workflow_api: str,
    prompt_version: str,
) -> None:
    register_demo_tools(client)
    payload = {"answer": "Escalation requires operator approval.", "grounded": False}
    tracker, _ = _install_openai_mode_runtime(
        client,
        monkeypatch,
        workflow_api=workflow_api,
        tool_name="escalate",
        tool_arguments={"ticket_id": "T-300"},
        payload=payload,
    )
    workflow_id = f"openai-{workflow_api}-gated-tool-worker-wf"
    manifest = _openai_structured_gated_tool_approval_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name=f"OpenAI {workflow_api} Gated Tool Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "escalate refund ticket T-300"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    waiting = client.get(f"{PREFIX}/workflow-runs/{run_id}")
    assert waiting.status_code == 200
    waiting_data = waiting.json()["data"]
    assert waiting_data["status"] == "waiting_approval"
    assert tracker.call_count() == tracker.expected_initial_calls

    agent_step = next(
        step for step in waiting_data["summary"]["steps"] if step["node_id"] == "agent"
    )
    assert agent_step["prompt_version"] == prompt_version
    assert agent_step["tool_calls"] == [
        {
            "tool": "escalate",
            "result": {
                "_gated": True,
                "tool": "escalate",
                "reason": "tool requires approval; not executed in an autonomous run",
            },
        }
    ]
    assert agent_step["output_by_port"]["structured_output"] == payload

    approvals = client.get(f"{PREFIX}/workflow-runs/{run_id}/approvals")
    assert approvals.status_code == 200
    approval_id = approvals.json()["data"][0]["runtime_approval_id"]
    approved = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/approval/approve",
        json={"runtime_approval_id": approval_id, "reason": "approved by test"},
    )
    assert approved.status_code == 200
    resumed = client.post(f"{PREFIX}/workflow-runs/{run_id}/resume")
    assert resumed.status_code == 202

    worker._tick()

    final = client.get(f"{PREFIX}/workflow-runs/{run_id}")
    assert final.status_code == 200
    final_data = final.json()["data"]
    assert final_data["status"] == "completed"
    assert json.loads(final_data["summary"]["output"]) == payload
    assert tracker.call_count() == tracker.expected_initial_calls

    with client.app.state.session_factory() as session:
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .filter(CaliberWorkflowRunEvent.event_type == "workflow.run.step")
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        agent_steps = [event for event in events if event.node_id == "agent"]
        human_gate_steps = [event for event in events if event.node_id == "human_gate"]
        final_steps = [event for event in events if event.node_id == "final"]
        assert len(agent_steps) == 1
        assert len(human_gate_steps) == 2
        assert dict(human_gate_steps[0].payload or {}).get("step", {}).get("status") == "blocked"
        assert dict(human_gate_steps[1].payload or {}).get("step", {}).get("status") == "ok"
        assert len(final_steps) == 1


@pytest.mark.parametrize(
    ("workflow_api", "prompt_version"),
    [
        ("chat_completions", "openai"),
        ("responses", "openai_responses"),
        ("agents_sdk", "openai_agents"),
    ],
)
def test_worker_openai_modes_preserve_mixed_regular_and_gated_tool_sequences_across_approval_resume(
    client,
    monkeypatch: pytest.MonkeyPatch,
    workflow_api: str,
    prompt_version: str,
) -> None:
    register_demo_tools(client)
    payload = {
        "answer": "Refund and shipping checks completed, and the escalation request stayed gated for operator review.",
        "grounded": False,
    }
    tool_turns = [
        {"tool_name": "lookup_policy", "query": "refund"},
        {"tool_name": "escalate", "ticket_id": "T-300"},
        {"tool_name": "lookup_policy", "query": "shipping"},
    ]
    tracker, _ = _install_openai_mode_runtime(
        client,
        monkeypatch,
        workflow_api=workflow_api,
        tool_turns=tool_turns,
        payload=payload,
    )
    workflow_id = f"openai-{workflow_api}-mixed-gated-tool-worker-wf"
    manifest = _openai_structured_mixed_tool_approval_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name=f"OpenAI {workflow_api} Mixed Gated Tool Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={
            "workflow_version_id": vid,
            "input": "check policy details and queue escalation ticket T-300",
        },
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    waiting = client.get(f"{PREFIX}/workflow-runs/{run_id}")
    assert waiting.status_code == 200
    waiting_data = waiting.json()["data"]
    assert tracker.call_count() == tracker.expected_initial_calls
    _assert_openai_waiting_agent_step(
        waiting_data,
        prompt_version=prompt_version,
        expected_tool_calls=[
            {
                "tool": "lookup_policy",
                "result": {
                    "policy": "Purchases within 30 days are eligible for a full refund.",
                    "query": "refund",
                },
            },
            {
                "tool": "escalate",
                "result": {
                    "_gated": True,
                    "tool": "escalate",
                    "reason": "tool requires approval; not executed in an autonomous run",
                },
            },
            {
                "tool": "lookup_policy",
                "result": {
                    "policy": "Purchases within 30 days are eligible for a full refund.",
                    "query": "shipping",
                },
            },
        ],
        payload=payload,
    )

    _assert_openai_approval_resume_completion(
        client,
        run_id=run_id,
        payload=payload,
    )
    assert tracker.call_count() == tracker.expected_initial_calls

    with client.app.state.session_factory() as session:
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .filter(CaliberWorkflowRunEvent.event_type == "workflow.run.step")
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        agent_steps = [event for event in events if event.node_id == "agent"]
        human_gate_steps = [event for event in events if event.node_id == "human_gate"]
        final_steps = [event for event in events if event.node_id == "final"]
        assert len(agent_steps) == 1
        assert len(human_gate_steps) == 2
        assert len(final_steps) == 1


@pytest.mark.parametrize(
    ("workflow_api", "prompt_version"),
    [
        ("chat_completions", "openai"),
        ("responses", "openai_responses"),
        ("agents_sdk", "openai_agents"),
    ],
)
def test_worker_openai_modes_preserve_mixed_regular_gated_and_retry_exhausted_failsoft_sequences_across_approval_resume(
    client,
    monkeypatch: pytest.MonkeyPatch,
    workflow_api: str,
    prompt_version: str,
) -> None:
    register_demo_tools(client)
    payload = {
        "answer": "Refund and shipping checks completed, escalation stayed gated, and the outage lookup failed soft after retry exhaustion.",
        "grounded": False,
    }
    tool_turns = [
        {"tool_name": "lookup_policy", "query": "refund"},
        {"tool_name": "escalate", "ticket_id": "T-300"},
        {"tool_name": "lookup_policy", "query": "outage"},
        {"tool_name": "lookup_policy", "query": "shipping"},
    ]
    tracker, _ = _install_openai_mode_runtime(
        client,
        monkeypatch,
        workflow_api=workflow_api,
        tool_turns=tool_turns,
        payload=payload,
    )
    workflow_id = f"openai-{workflow_api}-mixed-gated-failsoft-worker-wf"
    manifest = _openai_structured_mixed_tool_approval_manifest(workflow_id)
    tools = manifest["tools"]
    assert isinstance(tools, dict)
    lookup_policy = tools["lookup_policy"]
    assert isinstance(lookup_policy, dict)
    lookup_policy["max_retries"] = 2
    _wid, vid = create_and_publish(
        client,
        workflow_name=f"OpenAI {workflow_api} Mixed Gated Failsoft Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={
            "workflow_version_id": vid,
            "input": "check refund and shipping, queue escalation, and tolerate outage lookup failure",
        },
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    attempts = {"count": 0}

    def _retry_exhausted_lookup_policy(query: str = "") -> dict[str, object]:
        attempts["count"] += 1
        if query == "outage":
            raise RuntimeError(f"policy backend failed attempt {attempts['count']} for outage")
        return {
            "policy": "Purchases within 30 days are eligible for a full refund.",
            "query": query,
        }

    monkeypatch.setattr(
        "caliber.workflows.demo_tools.lookup_policy",
        _retry_exhausted_lookup_policy,
    )

    worker = _build_worker(client)
    worker._tick()

    waiting = client.get(f"{PREFIX}/workflow-runs/{run_id}")
    assert waiting.status_code == 200
    waiting_data = waiting.json()["data"]
    assert tracker.call_count() == tracker.expected_initial_calls
    assert attempts["count"] == 5
    _assert_openai_waiting_agent_step(
        waiting_data,
        prompt_version=prompt_version,
        expected_tool_calls=[
            {
                "tool": "lookup_policy",
                "result": {
                    "policy": "Purchases within 30 days are eligible for a full refund.",
                    "query": "refund",
                },
            },
            {
                "tool": "escalate",
                "result": {
                    "_gated": True,
                    "tool": "escalate",
                    "reason": "tool requires approval; not executed in an autonomous run",
                },
            },
            {
                "tool": "lookup_policy",
                "result": {
                    "_error": (
                        "ToolExecutionError: tool 'lookup_policy' failed after 3 attempt(s): "
                        "policy backend failed attempt 4 for outage"
                    ),
                    "tool": "lookup_policy",
                },
            },
            {
                "tool": "lookup_policy",
                "result": {
                    "policy": "Purchases within 30 days are eligible for a full refund.",
                    "query": "shipping",
                },
            },
        ],
        payload=payload,
    )

    _assert_openai_approval_resume_completion(
        client,
        run_id=run_id,
        payload=payload,
    )
    assert tracker.call_count() == tracker.expected_initial_calls
    assert attempts["count"] == 5

    with client.app.state.session_factory() as session:
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .filter(CaliberWorkflowRunEvent.event_type == "workflow.run.step")
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        agent_steps = [event for event in events if event.node_id == "agent"]
        human_gate_steps = [event for event in events if event.node_id == "human_gate"]
        final_steps = [event for event in events if event.node_id == "final"]
        assert len(agent_steps) == 1
        assert len(human_gate_steps) == 2
        assert len(final_steps) == 1


@pytest.mark.parametrize(
    ("workflow_api", "prompt_version"),
    [
        ("chat_completions", "openai"),
        ("responses", "openai_responses"),
        ("agents_sdk", "openai_agents"),
    ],
)
def test_worker_openai_modes_preserve_mixed_regular_gated_and_immediate_failsoft_sequences_across_approval_resume(
    client,
    monkeypatch: pytest.MonkeyPatch,
    workflow_api: str,
    prompt_version: str,
) -> None:
    register_demo_tools(client)
    payload = {
        "answer": "Refund and shipping checks completed, escalation stayed gated, and the outage lookup failed soft immediately.",
        "grounded": False,
    }
    tool_turns = [
        {"tool_name": "lookup_policy", "query": "refund"},
        {"tool_name": "escalate", "ticket_id": "T-300"},
        {"tool_name": "lookup_policy", "query": "outage"},
        {"tool_name": "lookup_policy", "query": "shipping"},
    ]
    tracker, _ = _install_openai_mode_runtime(
        client,
        monkeypatch,
        workflow_api=workflow_api,
        tool_turns=tool_turns,
        payload=payload,
    )
    workflow_id = f"openai-{workflow_api}-mixed-gated-immediate-failsoft-worker-wf"
    manifest = _openai_structured_mixed_tool_approval_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name=f"OpenAI {workflow_api} Mixed Gated Immediate Failsoft Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={
            "workflow_version_id": vid,
            "input": "check refund and shipping, queue escalation, and tolerate immediate outage lookup failure",
        },
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    attempts = {"count": 0}

    def _immediate_failsoft_lookup_policy(query: str = "") -> dict[str, object]:
        attempts["count"] += 1
        if query == "outage":
            raise RuntimeError(f"policy backend failed attempt {attempts['count']} for outage")
        return {
            "policy": "Purchases within 30 days are eligible for a full refund.",
            "query": query,
        }

    monkeypatch.setattr(
        "caliber.workflows.demo_tools.lookup_policy",
        _immediate_failsoft_lookup_policy,
    )

    worker = _build_worker(client)
    worker._tick()

    waiting = client.get(f"{PREFIX}/workflow-runs/{run_id}")
    assert waiting.status_code == 200
    waiting_data = waiting.json()["data"]
    assert tracker.call_count() == tracker.expected_initial_calls
    assert attempts["count"] == 3
    _assert_openai_waiting_agent_step(
        waiting_data,
        prompt_version=prompt_version,
        expected_tool_calls=[
            {
                "tool": "lookup_policy",
                "result": {
                    "policy": "Purchases within 30 days are eligible for a full refund.",
                    "query": "refund",
                },
            },
            {
                "tool": "escalate",
                "result": {
                    "_gated": True,
                    "tool": "escalate",
                    "reason": "tool requires approval; not executed in an autonomous run",
                },
            },
            {
                "tool": "lookup_policy",
                "result": {
                    "_error": (
                        "ToolExecutionError: tool 'lookup_policy' failed after 1 attempt(s): "
                        "policy backend failed attempt 2 for outage"
                    ),
                    "tool": "lookup_policy",
                },
            },
            {
                "tool": "lookup_policy",
                "result": {
                    "policy": "Purchases within 30 days are eligible for a full refund.",
                    "query": "shipping",
                },
            },
        ],
        payload=payload,
    )

    _assert_openai_approval_resume_completion(
        client,
        run_id=run_id,
        payload=payload,
    )
    assert tracker.call_count() == tracker.expected_initial_calls
    assert attempts["count"] == 3

    with client.app.state.session_factory() as session:
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .filter(CaliberWorkflowRunEvent.event_type == "workflow.run.step")
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        agent_steps = [event for event in events if event.node_id == "agent"]
        human_gate_steps = [event for event in events if event.node_id == "human_gate"]
        final_steps = [event for event in events if event.node_id == "final"]
        assert len(agent_steps) == 1
        assert len(human_gate_steps) == 2
        assert len(final_steps) == 1


@pytest.mark.parametrize(
    ("workflow_api", "prompt_version"),
    [
        ("chat_completions", "openai"),
        ("responses", "openai_responses"),
        ("agents_sdk", "openai_agents"),
    ],
)
def test_worker_openai_modes_preserve_gated_immediate_and_retry_exhausted_failsoft_sequences_in_one_run_across_approval_resume(
    client,
    monkeypatch: pytest.MonkeyPatch,
    workflow_api: str,
    prompt_version: str,
) -> None:
    register_demo_tools(client)
    payload = {
        "answer": (
            "Refund and shipping checks completed, escalation stayed gated, "
            "the first outage failed soft immediately, and the second outage failed soft after retry exhaustion."
        ),
        "grounded": False,
    }
    tool_turns = [
        {"tool_name": "lookup_policy", "query": "refund"},
        {"tool_name": "escalate", "ticket_id": "T-300"},
        {"tool_name": "lookup_policy", "query": "outage-immediate"},
        {"tool_name": "lookup_policy_retry", "query": "outage-retry"},
        {"tool_name": "lookup_policy", "query": "shipping"},
    ]
    tracker, _ = _install_openai_mode_runtime(
        client,
        monkeypatch,
        workflow_api=workflow_api,
        tool_turns=tool_turns,
        payload=payload,
    )
    workflow_id = f"openai-{workflow_api}-mixed-dual-failsoft-worker-wf"
    manifest = _openai_structured_mixed_dual_failsoft_tool_approval_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name=f"OpenAI {workflow_api} Mixed Dual Failsoft Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={
            "workflow_version_id": vid,
            "input": "check refund and shipping, queue escalation, and tolerate both immediate and retry-exhausted outage lookups",
        },
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    attempts = {
        "refund": 0,
        "outage-immediate": 0,
        "outage-retry": 0,
        "shipping": 0,
    }

    def _mixed_failsoft_lookup_policy(query: str = "") -> dict[str, object]:
        attempts.setdefault(query, 0)
        attempts[query] += 1
        if query.startswith("outage-"):
            raise RuntimeError(f"policy backend failed attempt {attempts[query]} for {query}")
        return {
            "policy": "Purchases within 30 days are eligible for a full refund.",
            "query": query,
        }

    monkeypatch.setattr(
        "caliber.workflows.demo_tools.lookup_policy",
        _mixed_failsoft_lookup_policy,
    )

    worker = _build_worker(client)
    worker._tick()

    waiting = client.get(f"{PREFIX}/workflow-runs/{run_id}")
    assert waiting.status_code == 200
    waiting_data = waiting.json()["data"]
    assert tracker.call_count() == tracker.expected_initial_calls
    assert attempts == {
        "refund": 1,
        "outage-immediate": 1,
        "outage-retry": 3,
        "shipping": 1,
    }
    _assert_openai_waiting_agent_step(
        waiting_data,
        prompt_version=prompt_version,
        expected_tool_calls=[
            {
                "tool": "lookup_policy",
                "result": {
                    "policy": "Purchases within 30 days are eligible for a full refund.",
                    "query": "refund",
                },
            },
            {
                "tool": "escalate",
                "result": {
                    "_gated": True,
                    "tool": "escalate",
                    "reason": "tool requires approval; not executed in an autonomous run",
                },
            },
            {
                "tool": "lookup_policy",
                "result": {
                    "_error": (
                        "ToolExecutionError: tool 'lookup_policy' failed after 1 attempt(s): "
                        "policy backend failed attempt 1 for outage-immediate"
                    ),
                    "tool": "lookup_policy",
                },
            },
            {
                "tool": "lookup_policy_retry",
                "result": {
                    "_error": (
                        "ToolExecutionError: tool 'lookup_policy_retry' failed after 3 attempt(s): "
                        "policy backend failed attempt 3 for outage-retry"
                    ),
                    "tool": "lookup_policy_retry",
                },
            },
            {
                "tool": "lookup_policy",
                "result": {
                    "policy": "Purchases within 30 days are eligible for a full refund.",
                    "query": "shipping",
                },
            },
        ],
        payload=payload,
    )

    _assert_openai_approval_resume_completion(
        client,
        run_id=run_id,
        payload=payload,
    )
    assert tracker.call_count() == tracker.expected_initial_calls
    assert attempts == {
        "refund": 1,
        "outage-immediate": 1,
        "outage-retry": 3,
        "shipping": 1,
    }

    with client.app.state.session_factory() as session:
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .filter(CaliberWorkflowRunEvent.event_type == "workflow.run.step")
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        agent_steps = [event for event in events if event.node_id == "agent"]
        human_gate_steps = [event for event in events if event.node_id == "human_gate"]
        final_steps = [event for event in events if event.node_id == "final"]
        assert len(agent_steps) == 1
        assert len(human_gate_steps) == 2
        assert len(final_steps) == 1


@pytest.mark.parametrize(
    ("workflow_api", "prompt_version"),
    [
        ("chat_completions", "openai"),
        ("responses", "openai_responses"),
        ("agents_sdk", "openai_agents"),
    ],
)
def test_worker_openai_modes_preserve_mixed_success_and_failsoft_tool_sequences_across_approval_resume(
    client,
    monkeypatch: pytest.MonkeyPatch,
    workflow_api: str,
    prompt_version: str,
) -> None:
    register_demo_tools(client)
    payload = {
        "answer": "Refund and shipping checks succeeded despite an intermediate tool failure.",
        "grounded": False,
    }
    tool_turns = [
        {"query": "refund"},
        {"query": "outage"},
        {"query": "shipping"},
    ]
    tracker, _ = _install_openai_mode_runtime(
        client,
        monkeypatch,
        workflow_api=workflow_api,
        tool_turns=tool_turns,
        payload=payload,
    )
    workflow_id = f"openai-{workflow_api}-mixed-tool-worker-wf"
    manifest = _openai_structured_approval_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name=f"OpenAI {workflow_api} Mixed Tool Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "collect policy details despite outages"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    def _sometimes_broken_lookup_policy(query: str = "") -> dict[str, object]:
        if query == "outage":
            raise RuntimeError("policy backend failed for outage")
        return {
            "policy": "Purchases within 30 days are eligible for a full refund.",
            "query": query,
        }

    monkeypatch.setattr(
        "caliber.workflows.demo_tools.lookup_policy",
        _sometimes_broken_lookup_policy,
    )

    worker = _build_worker(client)
    worker._tick()

    waiting = client.get(f"{PREFIX}/workflow-runs/{run_id}")
    assert waiting.status_code == 200
    waiting_data = waiting.json()["data"]
    assert waiting_data["status"] == "waiting_approval"
    assert tracker.call_count() == tracker.expected_initial_calls

    agent_step = next(
        step for step in waiting_data["summary"]["steps"] if step["node_id"] == "agent"
    )
    assert agent_step["prompt_version"] == prompt_version
    assert agent_step["tool_calls"] == [
        {
            "tool": "lookup_policy",
            "result": {
                "policy": "Purchases within 30 days are eligible for a full refund.",
                "query": "refund",
            },
        },
        {
            "tool": "lookup_policy",
            "result": {
                "_error": (
                    "ToolExecutionError: tool 'lookup_policy' failed after 1 attempt(s): "
                    "policy backend failed for outage"
                ),
                "tool": "lookup_policy",
            },
        },
        {
            "tool": "lookup_policy",
            "result": {
                "policy": "Purchases within 30 days are eligible for a full refund.",
                "query": "shipping",
            },
        },
    ]
    assert agent_step["output_by_port"]["structured_output"] == payload

    approvals = client.get(f"{PREFIX}/workflow-runs/{run_id}/approvals")
    assert approvals.status_code == 200
    approval_id = approvals.json()["data"][0]["runtime_approval_id"]
    approved = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/approval/approve",
        json={"runtime_approval_id": approval_id, "reason": "approved by test"},
    )
    assert approved.status_code == 200
    resumed = client.post(f"{PREFIX}/workflow-runs/{run_id}/resume")
    assert resumed.status_code == 202

    worker._tick()

    final = client.get(f"{PREFIX}/workflow-runs/{run_id}")
    assert final.status_code == 200
    final_data = final.json()["data"]
    assert final_data["status"] == "completed"
    assert json.loads(final_data["summary"]["output"]) == payload
    assert tracker.call_count() == tracker.expected_initial_calls

    with client.app.state.session_factory() as session:
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .filter(CaliberWorkflowRunEvent.event_type == "workflow.run.step")
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        agent_steps = [event for event in events if event.node_id == "agent"]
        human_gate_steps = [event for event in events if event.node_id == "human_gate"]
        final_steps = [event for event in events if event.node_id == "final"]
        assert len(agent_steps) == 1
        assert len(human_gate_steps) == 2
        assert len(final_steps) == 1


@pytest.mark.parametrize(
    ("workflow_api", "prompt_version"),
    [
        ("chat_completions", "openai"),
        ("responses", "openai_responses"),
        ("agents_sdk", "openai_agents"),
    ],
)
def test_worker_openai_modes_preserve_mixed_success_and_retry_exhausted_failsoft_sequences_across_approval_resume(
    client,
    monkeypatch: pytest.MonkeyPatch,
    workflow_api: str,
    prompt_version: str,
) -> None:
    register_demo_tools(client)
    payload = {
        "answer": "Refund and shipping checks succeeded despite a retry-exhausted outage lookup.",
        "grounded": False,
    }
    tool_turns = [
        {"query": "refund"},
        {"query": "outage"},
        {"query": "shipping"},
    ]
    tracker, _ = _install_openai_mode_runtime(
        client,
        monkeypatch,
        workflow_api=workflow_api,
        tool_turns=tool_turns,
        payload=payload,
    )
    workflow_id = f"openai-{workflow_api}-mixed-retry-exhausted-worker-wf"
    manifest = _openai_structured_retry_approval_manifest(workflow_id)
    tools = manifest["tools"]
    assert isinstance(tools, dict)
    lookup_policy = tools["lookup_policy"]
    assert isinstance(lookup_policy, dict)
    lookup_policy["max_retries"] = 2
    _wid, vid = create_and_publish(
        client,
        workflow_name=f"OpenAI {workflow_api} Mixed Retry Exhausted Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={
            "workflow_version_id": vid,
            "input": "collect policy details despite repeated outages",
        },
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    attempts = {"count": 0}

    def _retry_exhausted_lookup_policy(query: str = "") -> dict[str, object]:
        attempts["count"] += 1
        if query == "outage":
            raise RuntimeError(f"policy backend failed attempt {attempts['count']} for outage")
        return {
            "policy": "Purchases within 30 days are eligible for a full refund.",
            "query": query,
        }

    monkeypatch.setattr(
        "caliber.workflows.demo_tools.lookup_policy",
        _retry_exhausted_lookup_policy,
    )

    worker = _build_worker(client)
    worker._tick()

    waiting = client.get(f"{PREFIX}/workflow-runs/{run_id}")
    assert waiting.status_code == 200
    waiting_data = waiting.json()["data"]
    assert tracker.call_count() == tracker.expected_initial_calls
    assert attempts["count"] == 5
    _assert_openai_waiting_agent_step(
        waiting_data,
        prompt_version=prompt_version,
        expected_tool_calls=[
            {
                "tool": "lookup_policy",
                "result": {
                    "policy": "Purchases within 30 days are eligible for a full refund.",
                    "query": "refund",
                },
            },
            {
                "tool": "lookup_policy",
                "result": {
                    "_error": (
                        "ToolExecutionError: tool 'lookup_policy' failed after 3 attempt(s): "
                        "policy backend failed attempt 4 for outage"
                    ),
                    "tool": "lookup_policy",
                },
            },
            {
                "tool": "lookup_policy",
                "result": {
                    "policy": "Purchases within 30 days are eligible for a full refund.",
                    "query": "shipping",
                },
            },
        ],
        payload=payload,
    )

    _assert_openai_approval_resume_completion(
        client,
        run_id=run_id,
        payload=payload,
    )
    assert tracker.call_count() == tracker.expected_initial_calls
    assert attempts["count"] == 5

    with client.app.state.session_factory() as session:
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .filter(CaliberWorkflowRunEvent.event_type == "workflow.run.step")
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        agent_steps = [event for event in events if event.node_id == "agent"]
        human_gate_steps = [event for event in events if event.node_id == "human_gate"]
        final_steps = [event for event in events if event.node_id == "final"]
        assert len(agent_steps) == 1
        assert len(human_gate_steps) == 2
        assert len(final_steps) == 1


@pytest.mark.parametrize(
    ("workflow_api", "prompt_version"),
    [
        ("chat_completions", "openai"),
        ("responses", "openai_responses"),
        ("agents_sdk", "openai_agents"),
    ],
)
def test_worker_openai_modes_retry_flaky_model_tool_calls_before_approval_resume(
    client,
    monkeypatch: pytest.MonkeyPatch,
    workflow_api: str,
    prompt_version: str,
) -> None:
    register_demo_tools(client)
    tracker, payload = _install_openai_mode_runtime(
        client,
        monkeypatch,
        workflow_api=workflow_api,
    )
    workflow_id = f"openai-{workflow_api}-retry-tool-worker-wf"
    manifest = _openai_structured_retry_approval_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name=f"OpenAI {workflow_api} Retry Tool Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "what is the refund policy?"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    attempts = {"count": 0}

    def _flaky_lookup_policy(query: str = "") -> dict[str, object]:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("temporary policy backend outage")
        return {
            "policy": "Purchases within 30 days are eligible for a full refund.",
            "query": query,
        }

    monkeypatch.setattr(
        "caliber.workflows.demo_tools.lookup_policy",
        _flaky_lookup_policy,
    )

    worker = _build_worker(client)
    worker._tick()

    waiting = client.get(f"{PREFIX}/workflow-runs/{run_id}")
    assert waiting.status_code == 200
    waiting_data = waiting.json()["data"]
    assert tracker.call_count() == tracker.expected_initial_calls
    assert attempts["count"] == 2
    _assert_openai_waiting_agent_step(
        waiting_data,
        prompt_version=prompt_version,
        expected_tool_calls=[
            {
                "tool": "lookup_policy",
                "result": {
                    "policy": "Purchases within 30 days are eligible for a full refund.",
                    "query": "refund",
                },
            }
        ],
        payload=payload,
    )

    _assert_openai_approval_resume_completion(
        client,
        run_id=run_id,
        payload=payload,
    )
    assert tracker.call_count() == tracker.expected_initial_calls
    assert attempts["count"] == 2

    with client.app.state.session_factory() as session:
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .filter(CaliberWorkflowRunEvent.event_type == "workflow.run.step")
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        agent_steps = [event for event in events if event.node_id == "agent"]
        human_gate_steps = [event for event in events if event.node_id == "human_gate"]
        final_steps = [event for event in events if event.node_id == "final"]
        assert len(agent_steps) == 1
        assert len(human_gate_steps) == 2
        assert len(final_steps) == 1


@pytest.mark.parametrize(
    ("workflow_api", "prompt_version"),
    [
        ("chat_completions", "openai"),
        ("responses", "openai_responses"),
        ("agents_sdk", "openai_agents"),
    ],
)
def test_worker_openai_modes_retry_twice_before_approval_resume(
    client,
    monkeypatch: pytest.MonkeyPatch,
    workflow_api: str,
    prompt_version: str,
) -> None:
    register_demo_tools(client)
    tracker, payload = _install_openai_mode_runtime(
        client,
        monkeypatch,
        workflow_api=workflow_api,
    )
    workflow_id = f"openai-{workflow_api}-double-retry-tool-worker-wf"
    manifest = _openai_structured_retry_approval_manifest(workflow_id)
    tools = manifest["tools"]
    assert isinstance(tools, dict)
    lookup_policy = tools["lookup_policy"]
    assert isinstance(lookup_policy, dict)
    lookup_policy["max_retries"] = 2
    _wid, vid = create_and_publish(
        client,
        workflow_name=f"OpenAI {workflow_api} Double Retry Tool Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "what is the refund policy?"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    attempts = {"count": 0}

    def _twice_flaky_lookup_policy(query: str = "") -> dict[str, object]:
        attempts["count"] += 1
        if attempts["count"] <= 2:
            raise RuntimeError(f"temporary policy backend outage #{attempts['count']}")
        return {
            "policy": "Purchases within 30 days are eligible for a full refund.",
            "query": query,
        }

    monkeypatch.setattr(
        "caliber.workflows.demo_tools.lookup_policy",
        _twice_flaky_lookup_policy,
    )

    worker = _build_worker(client)
    worker._tick()

    waiting = client.get(f"{PREFIX}/workflow-runs/{run_id}")
    assert waiting.status_code == 200
    waiting_data = waiting.json()["data"]
    assert tracker.call_count() == tracker.expected_initial_calls
    assert attempts["count"] == 3
    _assert_openai_waiting_agent_step(
        waiting_data,
        prompt_version=prompt_version,
        expected_tool_calls=[
            {
                "tool": "lookup_policy",
                "result": {
                    "policy": "Purchases within 30 days are eligible for a full refund.",
                    "query": "refund",
                },
            }
        ],
        payload=payload,
    )

    _assert_openai_approval_resume_completion(
        client,
        run_id=run_id,
        payload=payload,
    )
    assert tracker.call_count() == tracker.expected_initial_calls
    assert attempts["count"] == 3

    with client.app.state.session_factory() as session:
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .filter(CaliberWorkflowRunEvent.event_type == "workflow.run.step")
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        agent_steps = [event for event in events if event.node_id == "agent"]
        human_gate_steps = [event for event in events if event.node_id == "human_gate"]
        final_steps = [event for event in events if event.node_id == "final"]
        assert len(agent_steps) == 1
        assert len(human_gate_steps) == 2
        assert len(final_steps) == 1


@pytest.mark.parametrize(
    ("workflow_api", "prompt_version"),
    [
        ("chat_completions", "openai"),
        ("responses", "openai_responses"),
        ("agents_sdk", "openai_agents"),
    ],
)
def test_worker_openai_modes_failsoft_after_retry_exhaustion_before_approval_resume(
    client,
    monkeypatch: pytest.MonkeyPatch,
    workflow_api: str,
    prompt_version: str,
) -> None:
    register_demo_tools(client)
    payload = {
        "answer": "The policy lookup kept failing, but the workflow can continue.",
        "grounded": False,
    }
    tracker, _ = _install_openai_mode_runtime(
        client,
        monkeypatch,
        workflow_api=workflow_api,
        payload=payload,
    )
    workflow_id = f"openai-{workflow_api}-retry-exhausted-tool-worker-wf"
    manifest = _openai_structured_retry_approval_manifest(workflow_id)
    tools = manifest["tools"]
    assert isinstance(tools, dict)
    lookup_policy = tools["lookup_policy"]
    assert isinstance(lookup_policy, dict)
    lookup_policy["max_retries"] = 2
    _wid, vid = create_and_publish(
        client,
        workflow_name=f"OpenAI {workflow_api} Retry Exhausted Tool Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "what is the refund policy?"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    attempts = {"count": 0}

    def _always_broken_lookup_policy(query: str = "") -> dict[str, object]:
        attempts["count"] += 1
        raise RuntimeError(f"policy backend failed attempt {attempts['count']} for {query}")

    monkeypatch.setattr(
        "caliber.workflows.demo_tools.lookup_policy",
        _always_broken_lookup_policy,
    )

    worker = _build_worker(client)
    worker._tick()

    waiting = client.get(f"{PREFIX}/workflow-runs/{run_id}")
    assert waiting.status_code == 200
    waiting_data = waiting.json()["data"]
    assert tracker.call_count() == tracker.expected_initial_calls
    assert attempts["count"] == 3
    _assert_openai_waiting_agent_step(
        waiting_data,
        prompt_version=prompt_version,
        expected_tool_calls=[
            {
                "tool": "lookup_policy",
                "result": {
                    "_error": (
                        "ToolExecutionError: tool 'lookup_policy' failed after 3 attempt(s): "
                        "policy backend failed attempt 3 for refund"
                    ),
                    "tool": "lookup_policy",
                },
            }
        ],
        payload=payload,
    )

    _assert_openai_approval_resume_completion(
        client,
        run_id=run_id,
        payload=payload,
    )
    assert tracker.call_count() == tracker.expected_initial_calls
    assert attempts["count"] == 3

    with client.app.state.session_factory() as session:
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .filter(CaliberWorkflowRunEvent.event_type == "workflow.run.step")
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        agent_steps = [event for event in events if event.node_id == "agent"]
        human_gate_steps = [event for event in events if event.node_id == "human_gate"]
        final_steps = [event for event in events if event.node_id == "final"]
        assert len(agent_steps) == 1
        assert len(human_gate_steps) == 2
        assert len(final_steps) == 1


@pytest.mark.parametrize(
    ("workflow_api", "prompt_version"),
    [
        ("chat_completions", "openai"),
        ("responses", "openai_responses"),
        ("agents_sdk", "openai_agents"),
    ],
)
def test_worker_openai_modes_failsoft_on_model_tool_error_before_approval_resume(
    client,
    monkeypatch: pytest.MonkeyPatch,
    workflow_api: str,
    prompt_version: str,
) -> None:
    register_demo_tools(client)
    payload = {"answer": "We hit a tool issue but can still continue.", "grounded": False}
    tracker, _ = _install_openai_mode_runtime(
        client,
        monkeypatch,
        workflow_api=workflow_api,
        payload=payload,
    )
    workflow_id = f"openai-{workflow_api}-tool-error-worker-wf"
    manifest = _openai_structured_approval_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name=f"OpenAI {workflow_api} Tool Error Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "what is the refund policy?"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    calls = {"count": 0}

    def _broken_lookup_policy(query: str = "") -> dict[str, object]:
        calls["count"] += 1
        raise RuntimeError(f"policy backend failed for {query}")

    monkeypatch.setattr(
        "caliber.workflows.demo_tools.lookup_policy",
        _broken_lookup_policy,
    )

    worker = _build_worker(client)
    worker._tick()

    waiting = client.get(f"{PREFIX}/workflow-runs/{run_id}")
    assert waiting.status_code == 200
    waiting_data = waiting.json()["data"]
    assert waiting_data["status"] == "waiting_approval"
    assert tracker.call_count() == tracker.expected_initial_calls
    assert calls["count"] == 1

    agent_step = next(
        step for step in waiting_data["summary"]["steps"] if step["node_id"] == "agent"
    )
    assert agent_step["prompt_version"] == prompt_version
    assert agent_step["tool_calls"] == [
        {
            "tool": "lookup_policy",
            "result": {
                "_error": (
                    "ToolExecutionError: tool 'lookup_policy' failed after 1 attempt(s): "
                    "policy backend failed for refund"
                ),
                "tool": "lookup_policy",
            },
        }
    ]
    assert agent_step["output_by_port"]["structured_output"] == payload

    approvals = client.get(f"{PREFIX}/workflow-runs/{run_id}/approvals")
    assert approvals.status_code == 200
    approval_id = approvals.json()["data"][0]["runtime_approval_id"]
    approved = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/approval/approve",
        json={"runtime_approval_id": approval_id, "reason": "approved by test"},
    )
    assert approved.status_code == 200
    resumed = client.post(f"{PREFIX}/workflow-runs/{run_id}/resume")
    assert resumed.status_code == 202

    worker._tick()

    final = client.get(f"{PREFIX}/workflow-runs/{run_id}")
    assert final.status_code == 200
    final_data = final.json()["data"]
    assert final_data["status"] == "completed"
    assert json.loads(final_data["summary"]["output"]) == payload
    assert tracker.call_count() == tracker.expected_initial_calls
    assert calls["count"] == 1

    with client.app.state.session_factory() as session:
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .filter(CaliberWorkflowRunEvent.event_type == "workflow.run.step")
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        agent_steps = [event for event in events if event.node_id == "agent"]
        human_gate_steps = [event for event in events if event.node_id == "human_gate"]
        final_steps = [event for event in events if event.node_id == "final"]
        assert len(agent_steps) == 1
        assert len(human_gate_steps) == 2
        assert len(final_steps) == 1


@pytest.mark.parametrize(
    ("workflow_api", "prompt_version"),
    [
        ("chat_completions", "openai"),
        ("responses", "openai_responses"),
        ("agents_sdk", "openai_agents"),
    ],
)
def test_worker_openai_modes_preserve_multi_turn_tool_sequences_across_approval_resume(
    client,
    monkeypatch: pytest.MonkeyPatch,
    workflow_api: str,
    prompt_version: str,
) -> None:
    register_demo_tools(client)
    payload = {"answer": "Refund, warranty, and shipping policies collected.", "grounded": True}
    tool_turns = [
        {"query": "refund"},
        {"query": "warranty"},
        {"query": "shipping"},
    ]
    tracker, _ = _install_openai_mode_runtime(
        client,
        monkeypatch,
        workflow_api=workflow_api,
        tool_turns=tool_turns,
        payload=payload,
    )
    workflow_id = f"openai-{workflow_api}-multi-turn-worker-wf"
    manifest = _openai_structured_approval_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name=f"OpenAI {workflow_api} Multi Turn Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "collect policy details"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    waiting = client.get(f"{PREFIX}/workflow-runs/{run_id}")
    assert waiting.status_code == 200
    waiting_data = waiting.json()["data"]
    assert waiting_data["status"] == "waiting_approval"
    assert tracker.call_count() == tracker.expected_initial_calls

    agent_step = next(
        step for step in waiting_data["summary"]["steps"] if step["node_id"] == "agent"
    )
    assert agent_step["prompt_version"] == prompt_version
    assert agent_step["tool_calls"] == [
        {
            "tool": "lookup_policy",
            "result": {
                "policy": "Purchases within 30 days are eligible for a full refund.",
                "query": "refund",
            },
        },
        {
            "tool": "lookup_policy",
            "result": {
                "policy": "Purchases within 30 days are eligible for a full refund.",
                "query": "warranty",
            },
        },
        {
            "tool": "lookup_policy",
            "result": {
                "policy": "Purchases within 30 days are eligible for a full refund.",
                "query": "shipping",
            },
        },
    ]
    assert agent_step["output_by_port"]["structured_output"] == payload

    approvals = client.get(f"{PREFIX}/workflow-runs/{run_id}/approvals")
    assert approvals.status_code == 200
    approval_id = approvals.json()["data"][0]["runtime_approval_id"]
    approved = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/approval/approve",
        json={"runtime_approval_id": approval_id, "reason": "approved by test"},
    )
    assert approved.status_code == 200
    resumed = client.post(f"{PREFIX}/workflow-runs/{run_id}/resume")
    assert resumed.status_code == 202

    worker._tick()

    final = client.get(f"{PREFIX}/workflow-runs/{run_id}")
    assert final.status_code == 200
    final_data = final.json()["data"]
    assert final_data["status"] == "completed"
    assert json.loads(final_data["summary"]["output"]) == payload
    assert tracker.call_count() == tracker.expected_initial_calls

    with client.app.state.session_factory() as session:
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .filter(CaliberWorkflowRunEvent.event_type == "workflow.run.step")
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        agent_steps = [event for event in events if event.node_id == "agent"]
        human_gate_steps = [event for event in events if event.node_id == "human_gate"]
        final_steps = [event for event in events if event.node_id == "final"]
        assert len(agent_steps) == 1
        assert len(human_gate_steps) == 2
        assert len(final_steps) == 1


@pytest.mark.parametrize(
    ("workflow_api", "prompt_version"),
    [
        ("chat_completions", "openai"),
        ("responses", "openai_responses"),
        ("agents_sdk", "openai_agents"),
    ],
)
def test_worker_openai_modes_preserve_near_iteration_cap_tool_churn_across_approval_resume(
    client,
    monkeypatch: pytest.MonkeyPatch,
    workflow_api: str,
    prompt_version: str,
) -> None:
    register_demo_tools(client)
    payload = {"answer": "Collected all policy slices before approval.", "grounded": True}
    tool_turns = [{"query": str(index)} for index in range(MAX_AGENT_TOOL_ITERATIONS)]
    tracker, _ = _install_openai_mode_runtime(
        client,
        monkeypatch,
        workflow_api=workflow_api,
        tool_turns=tool_turns,
        payload=payload,
    )
    workflow_id = f"openai-{workflow_api}-iteration-cap-worker-wf"
    manifest = _openai_structured_approval_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name=f"OpenAI {workflow_api} Iteration Cap Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "collect every policy slice"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    waiting = client.get(f"{PREFIX}/workflow-runs/{run_id}")
    assert waiting.status_code == 200
    waiting_data = waiting.json()["data"]
    assert waiting_data["status"] == "waiting_approval"
    assert tracker.call_count() == tracker.expected_initial_calls

    agent_step = next(
        step for step in waiting_data["summary"]["steps"] if step["node_id"] == "agent"
    )
    assert agent_step["prompt_version"] == prompt_version
    tool_calls = agent_step["tool_calls"]
    assert isinstance(tool_calls, list)
    assert len(tool_calls) == MAX_AGENT_TOOL_ITERATIONS
    assert tool_calls[0] == {
        "tool": "lookup_policy",
        "result": {
            "policy": "Purchases within 30 days are eligible for a full refund.",
            "query": "0",
        },
    }
    assert tool_calls[-1] == {
        "tool": "lookup_policy",
        "result": {
            "policy": "Purchases within 30 days are eligible for a full refund.",
            "query": str(MAX_AGENT_TOOL_ITERATIONS - 1),
        },
    }

    approvals = client.get(f"{PREFIX}/workflow-runs/{run_id}/approvals")
    assert approvals.status_code == 200
    approval_id = approvals.json()["data"][0]["runtime_approval_id"]
    approved = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/approval/approve",
        json={"runtime_approval_id": approval_id, "reason": "approved by test"},
    )
    assert approved.status_code == 200
    resumed = client.post(f"{PREFIX}/workflow-runs/{run_id}/resume")
    assert resumed.status_code == 202

    worker._tick()

    final = client.get(f"{PREFIX}/workflow-runs/{run_id}")
    assert final.status_code == 200
    final_data = final.json()["data"]
    assert final_data["status"] == "completed"
    assert json.loads(final_data["summary"]["output"]) == payload
    assert tracker.call_count() == tracker.expected_initial_calls

    with client.app.state.session_factory() as session:
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .filter(CaliberWorkflowRunEvent.event_type == "workflow.run.step")
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        agent_steps = [event for event in events if event.node_id == "agent"]
        human_gate_steps = [event for event in events if event.node_id == "human_gate"]
        final_steps = [event for event in events if event.node_id == "final"]
        assert len(agent_steps) == 1
        assert len(human_gate_steps) == 2
        assert len(final_steps) == 1


@pytest.mark.parametrize(
    ("workflow_api", "prompt_version"),
    [
        ("chat_completions", "openai"),
        ("responses", "openai_responses"),
        ("agents_sdk", "openai_agents"),
    ],
)
def test_worker_openai_modes_sustain_steady_state_gated_and_failsoft_tool_churn_across_approval_resume(
    client,
    monkeypatch: pytest.MonkeyPatch,
    workflow_api: str,
    prompt_version: str,
) -> None:
    register_demo_tools(client)
    payload = {
        "answer": (
            "Refund and shipping checks completed, escalation stayed gated, "
            "the first outage failed soft immediately, and the second outage failed soft after retry exhaustion."
        ),
        "grounded": False,
    }
    tool_turns = [
        {"tool_name": "lookup_policy", "query": "refund"},
        {"tool_name": "escalate", "ticket_id": "T-300"},
        {"tool_name": "lookup_policy", "query": "outage-immediate"},
        {"tool_name": "lookup_policy_retry", "query": "outage-retry"},
        {"tool_name": "lookup_policy", "query": "shipping"},
    ]
    cycle_inputs = tuple(
        f"cycle {index}: check refund and shipping, queue escalation, and tolerate both outage modes"
        for index in range(1, 6)
    )
    tracker, _ = _install_openai_mode_runtime(
        client,
        monkeypatch,
        workflow_api=workflow_api,
        tool_turns=tool_turns,
        payload=payload,
        response_repetitions=len(cycle_inputs),
    )
    workflow_id = f"openai-{workflow_api}-steady-state-mixed-dual-failsoft-worker-wf"
    manifest = _openai_structured_mixed_dual_failsoft_tool_approval_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name=f"OpenAI {workflow_api} Steady State Mixed Dual Failsoft Worker",
        manifest=manifest,
    )

    attempts = {
        "refund": 0,
        "outage-immediate": 0,
        "outage-retry": 0,
        "shipping": 0,
    }
    expected_attempts_per_cycle = {
        "refund": 1,
        "outage-immediate": 1,
        "outage-retry": 3,
        "shipping": 1,
    }

    def _mixed_failsoft_lookup_policy(query: str = "") -> dict[str, object]:
        attempts.setdefault(query, 0)
        attempts[query] += 1
        if query.startswith("outage-"):
            raise RuntimeError(f"policy backend failed attempt {attempts[query]} for {query}")
        return {
            "policy": "Purchases within 30 days are eligible for a full refund.",
            "query": query,
        }

    monkeypatch.setattr(
        "caliber.workflows.demo_tools.lookup_policy",
        _mixed_failsoft_lookup_policy,
    )

    worker = _build_worker(client)
    for cycle_index, cycle_input in enumerate(cycle_inputs, start=1):
        created = client.post(
            f"{PREFIX}/workflow-runs",
            json={"workflow_version_id": vid, "input": cycle_input},
        )
        assert created.status_code == 202
        run_id = created.json()["data"]["workflow_run_id"]

        expected_cycle_call_count = cycle_index * tracker.expected_initial_calls
        expected_attempts = {
            query: count * cycle_index for query, count in expected_attempts_per_cycle.items()
        }
        expected_tool_calls = _expected_openai_mixed_failsoft_tool_calls(
            outage_immediate_attempt=expected_attempts["outage-immediate"],
            outage_retry_attempt=expected_attempts["outage-retry"],
        )

        worker._tick()

        waiting = client.get(f"{PREFIX}/workflow-runs/{run_id}")
        assert waiting.status_code == 200
        waiting_data = waiting.json()["data"]
        assert tracker.call_count() == expected_cycle_call_count
        assert attempts == expected_attempts
        _assert_openai_waiting_agent_step(
            waiting_data,
            prompt_version=prompt_version,
            expected_tool_calls=expected_tool_calls,
            payload=payload,
        )

        _assert_openai_approval_resume_completion_with_worker(
            client,
            worker=worker,
            run_id=run_id,
            payload=payload,
        )
        assert tracker.call_count() == expected_cycle_call_count
        assert attempts == expected_attempts
        _assert_openai_worker_step_event_counts(
            client,
            run_id=run_id,
        )
        _assert_openai_worker_lifecycle_event_history(
            client,
            run_id=run_id,
        )

        worker._tick()
        assert tracker.call_count() == expected_cycle_call_count
        assert attempts == expected_attempts


@pytest.mark.parametrize(
    ("workflow_api", "prompt_version"),
    [
        ("chat_completions", "openai"),
        ("responses", "openai_responses"),
        ("agents_sdk", "openai_agents"),
    ],
)
def test_worker_openai_modes_drain_backlogged_gated_and_failsoft_runs_across_approval_resume(
    client,
    monkeypatch: pytest.MonkeyPatch,
    workflow_api: str,
    prompt_version: str,
) -> None:
    register_demo_tools(client)
    payload = {
        "answer": (
            "Refund and shipping checks completed, escalation stayed gated, "
            "the first outage failed soft immediately, and the second outage failed soft after retry exhaustion."
        ),
        "grounded": False,
    }
    tool_turns = [
        {"tool_name": "lookup_policy", "query": "refund"},
        {"tool_name": "escalate", "ticket_id": "T-300"},
        {"tool_name": "lookup_policy", "query": "outage-immediate"},
        {"tool_name": "lookup_policy_retry", "query": "outage-retry"},
        {"tool_name": "lookup_policy", "query": "shipping"},
    ]
    batch_inputs = tuple(
        f"batch run {index}: check refund and shipping, queue escalation, and tolerate both outage modes"
        for index in range(1, 4)
    )
    tracker, _ = _install_openai_mode_runtime(
        client,
        monkeypatch,
        workflow_api=workflow_api,
        tool_turns=tool_turns,
        payload=payload,
        response_repetitions=len(batch_inputs),
    )
    workflow_id = f"openai-{workflow_api}-batched-mixed-dual-failsoft-worker-wf"
    manifest = _openai_structured_mixed_dual_failsoft_tool_approval_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name=f"OpenAI {workflow_api} Batched Mixed Dual Failsoft Worker",
        manifest=manifest,
    )

    attempts = {
        "refund": 0,
        "outage-immediate": 0,
        "outage-retry": 0,
        "shipping": 0,
    }
    expected_attempts_per_run = {
        "refund": 1,
        "outage-immediate": 1,
        "outage-retry": 3,
        "shipping": 1,
    }

    def _mixed_failsoft_lookup_policy(query: str = "") -> dict[str, object]:
        attempts.setdefault(query, 0)
        attempts[query] += 1
        if query.startswith("outage-"):
            raise RuntimeError(f"policy backend failed attempt {attempts[query]} for {query}")
        return {
            "policy": "Purchases within 30 days are eligible for a full refund.",
            "query": query,
        }

    monkeypatch.setattr(
        "caliber.workflows.demo_tools.lookup_policy",
        _mixed_failsoft_lookup_policy,
    )

    run_ids: list[str] = []
    for batch_input in batch_inputs:
        created = client.post(
            f"{PREFIX}/workflow-runs",
            json={"workflow_version_id": vid, "input": batch_input},
        )
        assert created.status_code == 202
        run_ids.append(created.json()["data"]["workflow_run_id"])

    worker = _build_worker(client)
    for batch_index, run_id in enumerate(run_ids, start=1):
        worker._tick()

        expected_call_count = batch_index * tracker.expected_initial_calls
        expected_attempts = {
            query: count * batch_index for query, count in expected_attempts_per_run.items()
        }
        assert tracker.call_count() == expected_call_count
        assert attempts == expected_attempts

        waiting = client.get(f"{PREFIX}/workflow-runs/{run_id}")
        assert waiting.status_code == 200
        waiting_data = waiting.json()["data"]
        expected_tool_calls = _expected_openai_mixed_failsoft_tool_calls(
            outage_immediate_attempt=batch_index * expected_attempts_per_run["outage-immediate"],
            outage_retry_attempt=batch_index * expected_attempts_per_run["outage-retry"],
        )
        _assert_openai_waiting_agent_step(
            waiting_data,
            prompt_version=prompt_version,
            expected_tool_calls=expected_tool_calls,
            payload=payload,
        )

        for queued_run_id in run_ids[batch_index:]:
            queued = client.get(f"{PREFIX}/workflow-runs/{queued_run_id}")
            assert queued.status_code == 200
            assert queued.json()["data"]["status"] == "queued"

        _approve_and_resume_workflow_run(
            client,
            run_id=run_id,
            reason="approved by backlog test",
        )
        worker._tick()

        final = client.get(f"{PREFIX}/workflow-runs/{run_id}")
        assert final.status_code == 200
        final_data = final.json()["data"]
        assert final_data["status"] == "completed"
        assert json.loads(final_data["summary"]["output"]) == payload
        _assert_openai_worker_step_event_counts(client, run_id=run_id)
        _assert_openai_worker_lifecycle_event_history(client, run_id=run_id)
        assert tracker.call_count() == expected_call_count
        assert attempts == expected_attempts


def test_worker_executes_knowledge_query_node_with_age_graph_retrieval(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_queue(client)
    register_demo_tools(client)
    workflow_id = create_workflow(client, "Knowledge Query Worker")
    manifest = make_manifest(workflow_id)
    manifest["nodes"]["knowledge"] = {
        "id": "knowledge",
        "type": "knowledge_query",
        "knowledge_base_id": "KB-graph",
        "retrieval_modes": ["age_graph"],
        "top_k": 5,
        "graph_overrides": {
            "age_seed_mode": "query_text_only",
            "age_traversal_hops": 2,
            "strict_age_retrieval": True,
        },
    }
    manifest["edges"] = [
        {"id": "e_start_knowledge", "from": "start", "to": "knowledge", "map": {"msg": "question"}},
        {
            "id": "e_knowledge_final",
            "from": "knowledge",
            "to": "final",
            "map": {"answer": "response"},
        },
    ]
    version_id, _ = create_draft(client, workflow_id, manifest)
    published = client.post(f"{PREFIX}/workflow-versions/{version_id}/publish")
    assert published.status_code == 200

    class _FakeQueryResult:
        def __init__(self, payload) -> None:
            self.payload = payload

        def model_dump(self, mode: str = "json") -> dict[str, object]:
            return {
                "question": self.payload.question,
                "versions": [
                    {
                        "knowledge_base_version_id": self.payload.version_ids[0],
                        "retrieval_mode": self.payload.retrieval_modes[0],
                        "answer": "AGE worker answer",
                        "citations": [],
                        "retrieved_chunks": [],
                        "graph_context": {"age_graph_name": "knowledge_graph"},
                    }
                ],
            }

    class _FakeKnowledgeService:
        last_get: tuple[str, str | None] | None = None
        last_query = None
        last_identity_user: str | None = None

        def __init__(self, *, config, session_factory) -> None:
            self.config = config
            self.session_factory = session_factory

        def get_knowledge_base(self, knowledge_base_id: str, *, identity):
            type(self).last_get = (knowledge_base_id, identity.active_project_id)
            type(self).last_identity_user = identity.user_id
            return SimpleNamespace(active_version_id="KBV-active")

        def query(self, payload, *, identity):
            type(self).last_query = payload
            type(self).last_identity_user = identity.user_id
            return _FakeQueryResult(payload)

    monkeypatch.setattr("caliber.workflows.promoter.KnowledgeBaseService", _FakeKnowledgeService)

    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": version_id, "input": "How do refunds work?"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.summary["output"] == "AGE worker answer"

    assert _FakeKnowledgeService.last_get == ("KB-graph", None)
    assert _FakeKnowledgeService.last_identity_user == "@test"
    assert _FakeKnowledgeService.last_query is not None
    assert _FakeKnowledgeService.last_query.version_ids == ["KBV-active"]
    assert _FakeKnowledgeService.last_query.question == "How do refunds work?"
    assert _FakeKnowledgeService.last_query.retrieval_modes == ["age_graph"]
    assert _FakeKnowledgeService.last_query.top_k == 5
    assert _FakeKnowledgeService.last_query.graph_overrides is not None
    assert _FakeKnowledgeService.last_query.graph_overrides.age_seed_mode == "query_text_only"
    assert _FakeKnowledgeService.last_query.graph_overrides.age_traversal_hops == 2
    assert _FakeKnowledgeService.last_query.graph_overrides.strict_age_retrieval is True


def test_worker_executes_graph_hybrid_rag_starter_template(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_queue(client)
    workflow_name = "Graph Hybrid Starter Worker"
    workflow_id = create_workflow(client, workflow_name)
    manifest = _starter_manifest(
        "graph_hybrid_rag",
        workflow_id=workflow_id,
        workflow_name=workflow_name,
    )
    nodes = manifest["nodes"]
    assert isinstance(nodes, dict)
    knowledge = nodes["knowledge"]
    assert isinstance(knowledge, dict)
    knowledge["knowledge_base_id"] = "KB-HYBRID"
    version_id, _ = create_draft(client, workflow_id, manifest)
    published = client.post(f"{PREFIX}/workflow-versions/{version_id}/publish")
    assert published.status_code == 200

    class _FakeQueryResult:
        def __init__(self, payload) -> None:
            self.payload = payload

        def model_dump(self, mode: str = "json") -> dict[str, object]:
            del mode
            return {
                "question": self.payload.question,
                "versions": [
                    {
                        "knowledge_base_version_id": self.payload.version_ids[0],
                        "retrieval_mode": self.payload.retrieval_modes[0],
                        "answer": "Graph hybrid answer",
                        "citations": [{"chunk_id": "CH-H1", "label": "Hybrid evidence"}],
                        "retrieved_chunks": [
                            {
                                "chunk_id": "CH-H1",
                                "content": (
                                    "Hybrid retrieval joined graph neighbors with chunk similarity."
                                ),
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

    class _FakeKnowledgeService:
        last_get: tuple[str, str | None] | None = None
        last_query = None
        last_identity_user: str | None = None

        def __init__(self, *, config, session_factory) -> None:
            self.config = config
            self.session_factory = session_factory

        def get_knowledge_base(self, knowledge_base_id: str, *, identity):
            type(self).last_get = (knowledge_base_id, identity.active_project_id)
            type(self).last_identity_user = identity.user_id
            return SimpleNamespace(active_version_id="KBV-HYBRID-ACTIVE")

        def query(self, payload, *, identity):
            type(self).last_query = payload
            type(self).last_identity_user = identity.user_id
            return _FakeQueryResult(payload)

    monkeypatch.setattr("caliber.workflows.promoter.KnowledgeBaseService", _FakeKnowledgeService)

    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": version_id, "input": "refund policy"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.summary is not None
        assert run.summary["output"] == "Graph hybrid answer"
        summary_steps = run.summary.get("steps")
        assert isinstance(summary_steps, list)
        knowledge_step = next(
            step
            for step in summary_steps
            if isinstance(step, dict) and step.get("node_id") == "knowledge"
        )
        assert knowledge_step.get("node_type") == "knowledge_query"
        assert "graph_hybrid" in str(knowledge_step.get("detail", ""))
        output_by_port = knowledge_step.get("output_by_port")
        assert isinstance(output_by_port, dict)
        assert output_by_port.get("answer") == "Graph hybrid answer"
        assert output_by_port.get("graph_context") == {
            "retrieval_mode": "graph_hybrid",
            "matched_entities": ["policy", "refund"],
        }
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.completed"

    assert _FakeKnowledgeService.last_get == ("KB-HYBRID", None)
    assert _FakeKnowledgeService.last_identity_user == "@test"
    assert _FakeKnowledgeService.last_query is not None
    assert _FakeKnowledgeService.last_query.version_ids == ["KBV-HYBRID-ACTIVE"]
    assert _FakeKnowledgeService.last_query.question == "refund policy"
    assert _FakeKnowledgeService.last_query.retrieval_modes == ["graph_hybrid"]
    assert _FakeKnowledgeService.last_query.top_k == 6
    assert _FakeKnowledgeService.last_query.graph_overrides is None


def test_worker_waits_for_runtime_approval_and_persists_checkpoint(client) -> None:
    _enable_runtime_approvals(client)
    workflow_id = "approval-worker-wf"
    manifest = _approval_manifest(workflow_id)
    _wid, vid = create_and_publish(client, workflow_name="Approval Worker", manifest=manifest)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "requires approval"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "waiting_approval"
        assert run.error_code == "waiting_approval"
        summary = dict(run.summary or {})
        checkpoint_id = summary.get("resume_checkpoint_id")
        assert isinstance(checkpoint_id, str) and checkpoint_id
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None
        assert checkpoint.node_id == "human_gate"
        approvals = (
            session.query(CaliberRuntimeApprovalRequest)
            .filter(CaliberRuntimeApprovalRequest.workflow_run_id == run_id)
            .all()
        )
        assert len(approvals) == 1
        assert approvals[0].status == "pending"
        assert approvals[0].node_id == "human_gate"


def test_worker_fails_malformed_waiting_approval_result_instead_of_persisting_bad_checkpoint(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime_approvals(client)
    workflow_id = "approval-worker-malformed-wf"
    manifest = _approval_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Approval Worker Malformed",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "requires approval"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    def _fake_execute(*args, **kwargs):
        return WorkflowRunResult(
            status="blocked",
            output="",
            error="waiting_approval:human_gate",
            steps=[NodeStep("agent", "agent", "ok", output="requires approval")],
        )

    monkeypatch.setattr("caliber.orchestrator.workflow_run_worker.execute", _fake_execute)

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.current_node_id is None
        assert run.error_code == "runtime_error"
        assert run.error_summary == (
            "workflow runtime returned waiting_approval without resumable checkpoint context"
        )
        assert not dict(run.summary or {}).get("resume_checkpoint_id")
        assert (
            session.query(CaliberWorkflowRunCheckpoint)
            .filter(CaliberWorkflowRunCheckpoint.workflow_run_id == run_id)
            .count()
            == 0
        )
        assert (
            session.query(CaliberRuntimeApprovalRequest)
            .filter(CaliberRuntimeApprovalRequest.workflow_run_id == run_id)
            .count()
            == 0
        )
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.failed"


def test_worker_fails_waiting_approval_result_missing_input_snapshot_instead_of_persisting_bad_checkpoint(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime_approvals(client)
    workflow_id = "approval-worker-missing-input-wf"
    manifest = _approval_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Approval Worker Missing Input",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "requires approval"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    def _fake_execute(*args, **kwargs):
        return WorkflowRunResult(
            status="blocked",
            output="",
            error="waiting_approval:human_gate",
            steps=[
                NodeStep(
                    "human_gate",
                    "human_approval",
                    "blocked",
                    output="requires approval",
                    detail="waiting_approval:human_gate",
                )
            ],
        )

    monkeypatch.setattr("caliber.orchestrator.workflow_run_worker.execute", _fake_execute)

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.current_node_id is None
        assert run.error_code == "runtime_error"
        assert run.error_summary == (
            "workflow runtime returned waiting_approval without resumable checkpoint context"
        )
        assert not dict(run.summary or {}).get("resume_checkpoint_id")
        assert (
            session.query(CaliberWorkflowRunCheckpoint)
            .filter(CaliberWorkflowRunCheckpoint.workflow_run_id == run_id)
            .count()
            == 0
        )
        assert (
            session.query(CaliberRuntimeApprovalRequest)
            .filter(CaliberRuntimeApprovalRequest.workflow_run_id == run_id)
            .count()
            == 0
        )
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.failed"


def test_worker_fails_waiting_approval_result_with_wrong_node_family_instead_of_persisting_bad_checkpoint(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime_approvals(client)
    workflow_id = "approval-worker-wrong-family-wf"
    manifest = _approval_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Approval Worker Wrong Family",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "requires approval"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    def _fake_execute(*args, **kwargs):
        step = NodeStep(
            "human_gate",
            "python_code",
            "blocked",
            output="requires approval",
            detail="waiting_approval:human_gate",
        )
        step.input_by_port = {"request": "requires approval"}
        return WorkflowRunResult(
            status="blocked",
            output="",
            error="waiting_approval:human_gate",
            steps=[step],
        )

    monkeypatch.setattr("caliber.orchestrator.workflow_run_worker.execute", _fake_execute)

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.current_node_id is None
        assert run.error_code == "runtime_error"
        assert run.error_summary == (
            "workflow runtime returned waiting_approval without resumable checkpoint context"
        )
        assert not dict(run.summary or {}).get("resume_checkpoint_id")
        assert (
            session.query(CaliberWorkflowRunCheckpoint)
            .filter(CaliberWorkflowRunCheckpoint.workflow_run_id == run_id)
            .count()
            == 0
        )
        assert (
            session.query(CaliberRuntimeApprovalRequest)
            .filter(CaliberRuntimeApprovalRequest.workflow_run_id == run_id)
            .count()
            == 0
        )
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.failed"


def test_worker_resumes_after_runtime_approval(client) -> None:
    _enable_runtime_approvals(client)
    workflow_id = "approval-resume-wf"
    manifest = _approval_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client, workflow_name="Approval Resume Worker", manifest=manifest
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "resume me"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    approvals = client.get(f"{PREFIX}/workflow-runs/{run_id}/approvals")
    assert approvals.status_code == 200
    approval_id = approvals.json()["data"][0]["runtime_approval_id"]

    approved = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/approval/approve",
        json={"runtime_approval_id": approval_id, "reason": "approved by test"},
    )
    assert approved.status_code == 200
    resumed = client.post(f"{PREFIX}/workflow-runs/{run_id}/resume")
    assert resumed.status_code == 202

    worker._tick()

    final = client.get(f"{PREFIX}/workflow-runs/{run_id}")
    assert final.status_code == 200
    assert final.json()["data"]["status"] == "completed"

    with client.app.state.session_factory() as session:
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .filter(CaliberWorkflowRunEvent.event_type == "workflow.run.step")
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        agent_steps = [event for event in events if event.node_id == "agent"]
        human_gate_steps = [event for event in events if event.node_id == "human_gate"]
        final_steps = [event for event in events if event.node_id == "final"]
        assert len(agent_steps) == 1
        assert len(human_gate_steps) == 2
        assert dict(human_gate_steps[0].payload or {}).get("step", {}).get("status") == "blocked"
        assert dict(human_gate_steps[1].payload or {}).get("step", {}).get("status") == "ok"
        assert len(final_steps) == 1


def test_worker_human_approval_resume_rejects_missing_input_snapshot_before_worker_resume(
    client,
) -> None:
    _enable_runtime_approvals(client)
    workflow_id = "approval-resume-invalid-checkpoint-wf"
    manifest = _approval_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Approval Resume Invalid Checkpoint Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "resume me"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    approvals = client.get(f"{PREFIX}/workflow-runs/{run_id}/approvals")
    assert approvals.status_code == 200
    approval_id = approvals.json()["data"][0]["runtime_approval_id"]

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        checkpoint_id = dict(run.summary or {}).get("resume_checkpoint_id")
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None
        assert isinstance(checkpoint.state_blob, dict)
        checkpoint.state_blob = {
            **checkpoint.state_blob,
            "input_by_port": ["not", "a", "dict"],
        }
        session.commit()

    approved = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/approval/approve",
        json={"runtime_approval_id": approval_id, "reason": "approved by test"},
    )
    assert approved.status_code == 409
    assert (
        approved.json()["detail"]
        == "workflow run approval checkpoint is missing its input snapshot"
    )

    final = client.get(f"{PREFIX}/workflow-runs/{run_id}")
    assert final.status_code == 200
    data = final.json()["data"]
    assert data["status"] == "waiting_approval"
    assert data["current_node_id"] == "human_gate"

    with client.app.state.session_factory() as session:
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        event_types = [event.event_type for event in events]
        assert "workflow.run.resumed" not in event_types
        assert "workflow.run.approval.approved" not in event_types
        assert event_types[-1] == "workflow.run.waiting_approval"


def test_worker_runtime_tool_approval_blocks_then_executes_after_resume(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime_approvals(client)
    workflow_id = "tool-approval-worker-wf"
    manifest = _tool_approval_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Tool Approval Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "escalate ticket T-300"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    calls: list[tuple[dict[str, object], str]] = []

    def _fake_call_tool(_fn, arguments, *, fallback_input):
        calls.append((dict(arguments), fallback_input))
        return {"message": f"executed {fallback_input}"}

    monkeypatch.setattr("caliber.workflows.runtime._call_tool", _fake_call_tool)

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "waiting_approval"
        assert run.current_node_id == "tool_gate"
        checkpoint_id = dict(run.summary or {}).get("resume_checkpoint_id")
        assert isinstance(checkpoint_id, str) and checkpoint_id
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None
        assert checkpoint.node_id == "tool_gate"
        assert checkpoint.state_blob["kind"] == "runtime_approval"
        assert checkpoint.state_blob["input_by_port"] == {"input": "escalate ticket T-300"}
    assert calls == []

    approvals = client.get(f"{PREFIX}/workflow-runs/{run_id}/approvals")
    assert approvals.status_code == 200
    approval_id = approvals.json()["data"][0]["runtime_approval_id"]

    approved = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/approval/approve",
        json={"runtime_approval_id": approval_id, "reason": "approved by test"},
    )
    assert approved.status_code == 200
    resumed = client.post(f"{PREFIX}/workflow-runs/{run_id}/resume")
    assert resumed.status_code == 202

    worker._tick()

    final = client.get(f"{PREFIX}/workflow-runs/{run_id}")
    assert final.status_code == 200
    assert final.json()["data"]["status"] == "completed"
    assert calls == [({}, "escalate ticket T-300")]


def test_worker_fails_runtime_tool_approval_result_missing_input_snapshot_instead_of_persisting_bad_checkpoint(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime_approvals(client)
    workflow_id = "tool-approval-worker-missing-input-wf"
    manifest = _tool_approval_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Tool Approval Worker Missing Input",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "escalate ticket T-300"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    def _fake_execute(*args, **kwargs):
        return WorkflowRunResult(
            status="blocked",
            output="",
            error="waiting_approval:tool_gate",
            steps=[
                NodeStep(
                    "tool_gate",
                    "tool",
                    "blocked",
                    output="escalate ticket T-300",
                    detail="waiting_approval:tool_gate",
                )
            ],
        )

    monkeypatch.setattr("caliber.orchestrator.workflow_run_worker.execute", _fake_execute)

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.current_node_id is None
        assert run.error_code == "runtime_error"
        assert run.error_summary == (
            "workflow runtime returned waiting_approval without resumable checkpoint context"
        )
        assert not dict(run.summary or {}).get("resume_checkpoint_id")
        assert (
            session.query(CaliberWorkflowRunCheckpoint)
            .filter(CaliberWorkflowRunCheckpoint.workflow_run_id == run_id)
            .count()
            == 0
        )
        assert (
            session.query(CaliberRuntimeApprovalRequest)
            .filter(CaliberRuntimeApprovalRequest.workflow_run_id == run_id)
            .count()
            == 0
        )
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.failed"


def test_worker_fails_runtime_tool_approval_result_with_wrong_node_family_instead_of_persisting_bad_checkpoint(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime_approvals(client)
    workflow_id = "tool-approval-worker-wrong-family-wf"
    manifest = _tool_approval_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Tool Approval Worker Wrong Family",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "escalate ticket T-300"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    def _fake_execute(*args, **kwargs):
        step = NodeStep(
            "tool_gate",
            "agent",
            "blocked",
            output="escalate ticket T-300",
            detail="waiting_approval:tool_gate",
        )
        step.input_by_port = {"input": "escalate ticket T-300"}
        return WorkflowRunResult(
            status="blocked",
            output="",
            error="waiting_approval:tool_gate",
            steps=[step],
        )

    monkeypatch.setattr("caliber.orchestrator.workflow_run_worker.execute", _fake_execute)

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.current_node_id is None
        assert run.error_code == "runtime_error"
        assert run.error_summary == (
            "workflow runtime returned waiting_approval without resumable checkpoint context"
        )
        assert not dict(run.summary or {}).get("resume_checkpoint_id")
        assert (
            session.query(CaliberWorkflowRunCheckpoint)
            .filter(CaliberWorkflowRunCheckpoint.workflow_run_id == run_id)
            .count()
            == 0
        )
        assert (
            session.query(CaliberRuntimeApprovalRequest)
            .filter(CaliberRuntimeApprovalRequest.workflow_run_id == run_id)
            .count()
            == 0
        )
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.failed"


def test_worker_runtime_tool_approval_resume_rejects_missing_input_snapshot_before_worker_resume(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime_approvals(client)
    workflow_id = "tool-approval-invalid-checkpoint-worker-wf"
    manifest = _tool_approval_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Tool Approval Invalid Checkpoint Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "escalate ticket T-300"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    calls: list[tuple[dict[str, object], str]] = []

    def _fake_call_tool(_fn, arguments, *, fallback_input):
        calls.append((dict(arguments), fallback_input))
        return {"message": f"executed {fallback_input}"}

    monkeypatch.setattr("caliber.workflows.runtime._call_tool", _fake_call_tool)

    worker = _build_worker(client)
    worker._tick()

    approvals = client.get(f"{PREFIX}/workflow-runs/{run_id}/approvals")
    assert approvals.status_code == 200
    approval_id = approvals.json()["data"][0]["runtime_approval_id"]

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        checkpoint_id = dict(run.summary or {}).get("resume_checkpoint_id")
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None
        assert isinstance(checkpoint.state_blob, dict)
        checkpoint.state_blob = {
            **checkpoint.state_blob,
            "input_by_port": ["not", "a", "dict"],
        }
        session.commit()

    approved = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/approval/approve",
        json={"runtime_approval_id": approval_id, "reason": "approved by test"},
    )
    assert approved.status_code == 409
    assert (
        approved.json()["detail"]
        == "workflow run approval checkpoint is missing its input snapshot"
    )

    final = client.get(f"{PREFIX}/workflow-runs/{run_id}")
    assert final.status_code == 200
    data = final.json()["data"]
    assert data["status"] == "waiting_approval"
    assert data["current_node_id"] == "tool_gate"
    assert calls == []

    with client.app.state.session_factory() as session:
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        event_types = [event.event_type for event in events]
        assert "workflow.run.resumed" not in event_types
        assert "workflow.run.approval.approved" not in event_types
        assert event_types[-1] == "workflow.run.waiting_approval"


def test_worker_executes_tool_node_path_to_completion(client) -> None:
    _enable_queue(client)
    register_demo_tools(client)
    workflow_id = "tool-success-worker-wf"
    manifest = _tool_success_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Tool Success Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "refund policy"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.error_code is None
        assert run.completed_at is not None
        assert run.summary is not None
        assert "Purchases within 30 days are eligible for a full refund." in str(
            run.summary.get("output", "")
        )

        steps = run.summary.get("steps")
        assert isinstance(steps, list)
        tool_step = next(
            step
            for step in steps
            if isinstance(step, dict) and step.get("node_id") == "tool_lookup"
        )
        assert tool_step.get("node_type") == "tool"
        assert tool_step.get("input_by_port") == {"input": "refund policy"}
        assert tool_step.get("detail") == "invoked lookup_policy"
        output_by_port = tool_step.get("output_by_port")
        assert isinstance(output_by_port, dict)
        assert "Purchases within 30 days are eligible for a full refund." in str(
            output_by_port.get("text", "")
        )
        result_payload = output_by_port.get("result")
        assert isinstance(result_payload, dict)
        assert result_payload == {
            "policy": "Purchases within 30 days are eligible for a full refund.",
            "query": "refund policy",
        }
        metadata = output_by_port.get("metadata")
        assert isinstance(metadata, dict)
        assert metadata == {
            "tool_name": "lookup_policy",
            "registry_ref": "tool.lookup_policy.v1",
            "binding_type": "registered_function",
            "requires_approval": False,
            "side_effect_level": "read",
            "arguments": {},
            "module_path": "caliber.workflows.demo_tools",
            "callable_name": "lookup_policy",
        }
        tool_calls = tool_step.get("tool_calls")
        assert isinstance(tool_calls, list)
        assert tool_calls == [
            {
                "tool": "lookup_policy",
                "registry_ref": "tool.lookup_policy.v1",
                "binding_type": "registered_function",
                "arguments": {},
                "result": {
                    "policy": "Purchases within 30 days are eligible for a full refund.",
                    "query": "refund policy",
                },
            }
        ]

        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        event_types = [event.event_type for event in events]
        assert event_types[-1] == "workflow.run.completed"
        assert "workflow.run.failed" not in event_types
        assert event_types.count("workflow.run.step") >= 2


def test_worker_executes_tool_first_workflow_without_agent(client) -> None:
    _enable_queue(client)
    register_demo_tools(client)
    workflow_id = "tool-first-worker-wf"
    manifest = _tool_first_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Tool First Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "refund policy"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.error_code is None
        assert run.completed_at is not None
        assert run.summary is not None
        assert "Purchases within 30 days are eligible for a full refund." in str(
            run.summary.get("output", "")
        )

        steps = run.summary.get("steps")
        assert isinstance(steps, list)
        assert [step.get("node_id") for step in steps if isinstance(step, dict)] == [
            "start",
            "tool_lookup",
            "final",
        ]
        tool_step = next(
            step
            for step in steps
            if isinstance(step, dict) and step.get("node_id") == "tool_lookup"
        )
        assert tool_step.get("node_type") == "tool"
        assert tool_step.get("input_by_port") == {"input": "refund policy"}
        output_by_port = tool_step.get("output_by_port")
        assert isinstance(output_by_port, dict)
        result_payload = output_by_port.get("result")
        assert isinstance(result_payload, dict)
        assert result_payload == {
            "policy": "Purchases within 30 days are eligible for a full refund.",
            "query": "refund policy",
        }

        final_step = next(
            step for step in steps if isinstance(step, dict) and step.get("node_id") == "final"
        )
        assert "Purchases within 30 days are eligible for a full refund." in str(
            final_step.get("output", "")
        )


def test_worker_marks_tool_callable_failures_as_runtime_errors(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_queue(client)
    workflow_id = "tool-failure-worker-wf"
    manifest = _tool_failure_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Tool Failure Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "refund policy"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    def _boom(query: str = "") -> dict[str, str]:
        raise RuntimeError(f"boom: {query}")

    monkeypatch.setattr("caliber.workflows.demo_tools.lookup_policy", _boom)

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.error_code == "runtime_error"
        assert run.error_summary is not None
        assert "tool 'lookup_policy' failed after 1 attempt(s)" in run.error_summary
        assert "boom: refund policy" in run.error_summary
        assert run.completed_at is not None
        assert run.summary is not None
        assert "tool 'lookup_policy' failed after 1 attempt(s)" in str(run.summary.get("error", ""))
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.failed"
        assert "tool 'lookup_policy' failed after 1 attempt(s)" in str(
            events[-1].payload.get("error", "")
        )


def test_worker_marks_loop_invalid_stop_condition_failures_as_runtime_errors(client) -> None:
    _enable_queue(client)
    workflow_id = "loop-invalid-stop-condition-worker-wf"
    manifest = _loop_invalid_stop_condition_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Loop Invalid Stop Condition Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "0"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.error_code == "runtime_error"
        assert run.error_summary is not None
        assert "loop node 'loop' stop_condition is invalid" in run.error_summary
        assert run.completed_at is not None
        assert run.summary is not None
        assert "loop node 'loop' stop_condition is invalid" in str(run.summary.get("error", ""))
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.failed"
        assert "loop node 'loop' stop_condition is invalid" in str(
            events[-1].payload.get("error", "")
        )


def test_worker_completes_loop_runs_until_stop_condition(client) -> None:
    _enable_queue(client)
    workflow_id = "loop-stop-condition-worker-wf"
    manifest = _loop_completion_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Loop Stop Condition Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "0"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.completed_at is not None
        assert run.summary is not None
        assert run.summary["output"] == "3"
        summary_steps = run.summary.get("steps")
        assert isinstance(summary_steps, list)
        loop_step = next(
            step
            for step in summary_steps
            if isinstance(step, dict) and step.get("node_id") == "loop"
        )
        assert "iterated 3 time(s) via python_code until stop condition" in str(
            loop_step.get("detail", "")
        )
        output_by_port = loop_step.get("output_by_port")
        assert isinstance(output_by_port, dict)
        assert output_by_port["result"]["count"] == 3
        assert output_by_port["metadata"]["stop_reason"] == "stop_condition"
        iterations = output_by_port["iterations"]
        assert isinstance(iterations, list)
        assert len(iterations) == 3
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.completed"


def test_worker_completes_loop_runs_when_max_iterations_are_reached(client) -> None:
    _enable_queue(client)
    workflow_id = "loop-max-iterations-worker-wf"
    manifest = make_manifest(workflow_id)
    manifest["nodes"] = {
        "start": {
            "id": "start",
            "type": "start",
            "outputs": {"msg": {"type": "string"}},
        },
        "counter": {
            "id": "counter",
            "type": "python_code",
            "code": (
                'current = inputs.get("count")\n'
                "if current is None:\n"
                '    current = int(str(run_input or "0") or "0")\n'
                "count = int(current) + 1\n"
                'return {"text": str(count), "result": {"count": count}}'
            ),
            "inputs": {"count": {"type": "structured"}, "done": {"type": "boolean"}},
            "outputs": {"text": {"type": "string"}, "result": {"type": "structured"}},
        },
        "loop": {
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
        },
        "final": {
            "id": "final",
            "type": "output",
            "inputs": {"response": {"type": "string"}},
        },
    }
    manifest["edges"] = [
        {"id": "e_start_loop", "from": "start", "to": "loop", "map": {"msg": "input"}},
        {"id": "e_loop_final", "from": "loop", "to": "final", "map": {"output": "response"}},
    ]
    _wid, vid = create_and_publish(
        client,
        workflow_name="Loop Max Iterations Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "0"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.completed_at is not None
        assert run.summary is not None
        assert run.summary["output"] == "2"
        summary_steps = run.summary.get("steps")
        assert isinstance(summary_steps, list)
        loop_step = next(
            step
            for step in summary_steps
            if isinstance(step, dict) and step.get("node_id") == "loop"
        )
        assert "iterated 2 time(s) via python_code (max reached)" in str(
            loop_step.get("detail", "")
        )
        output_by_port = loop_step.get("output_by_port")
        assert isinstance(output_by_port, dict)
        assert output_by_port["result"]["count"] == 2
        assert output_by_port["metadata"]["stop_reason"] == "max_iterations_reached"
        iterations = output_by_port["iterations"]
        assert isinstance(iterations, list)
        assert len(iterations) == 2
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.completed"


def test_worker_marks_external_app_timeout_failures_as_runtime_errors(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_queue(client)
    module_name = "workflow_external_timeout_worker"
    module = types.ModuleType(module_name)

    def slow_handler(payload: dict[str, object]) -> dict[str, str]:
        time.sleep(0.02)
        return {"text": str(payload["input"])}

    module.handle = slow_handler
    monkeypatch.setitem(sys.modules, module_name, module)

    workflow_id = "external-app-timeout-worker-wf"
    manifest = _external_app_timeout_manifest(
        workflow_id,
        entrypoint=f"{module_name}:handle",
    )
    _wid, vid = create_and_publish(
        client,
        workflow_name="External App Timeout Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "approve"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.error_code == "runtime_error"
        assert run.error_summary is not None
        assert "external_app node 'external' timed out after 0.001s" in run.error_summary
        assert run.completed_at is not None
        assert run.summary is not None
        assert "external_app node 'external' timed out after 0.001s" in str(
            run.summary.get("error", "")
        )
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.failed"
        assert "external_app node 'external' timed out after 0.001s" in str(
            events[-1].payload.get("error", "")
        )


def test_worker_executes_external_app_path_to_completion(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_queue(client)
    module_name = "workflow_external_success_worker"
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
            "echo": run_input,
            "metadata": {
                "handled_by": "async-handle",
                "preview": preview,
                "session_id": session_id,
                "input_count": len(inputs or {}),
                "context_seen": context,
            },
        }

    module.handle = handle
    monkeypatch.setitem(sys.modules, module_name, module)

    workflow_id = "external-app-success-worker-wf"
    manifest = _external_app_success_manifest(
        workflow_id,
        entrypoint=f"{module_name}:handle",
    )
    _wid, vid = create_and_publish(
        client,
        workflow_name="External App Success Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "approve"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.error_code is None
        assert run.completed_at is not None
        assert run.summary is not None
        assert run.summary.get("output") == "handled approve"

        steps = run.summary.get("steps")
        assert isinstance(steps, list)
        external_step = next(
            step for step in steps if isinstance(step, dict) and step.get("node_id") == "external"
        )
        assert external_step.get("node_type") == "external_app"
        assert external_step.get("input_by_port") == {"input": "approve"}
        assert "workflow_external_success_worker:handle" in str(external_step.get("detail", ""))
        output_by_port = external_step.get("output_by_port")
        assert isinstance(output_by_port, dict)
        assert output_by_port.get("text") == "handled approve"
        result_payload = output_by_port.get("result")
        assert isinstance(result_payload, dict)
        assert result_payload.get("text") == "handled approve"
        assert result_payload.get("echo") == "approve"
        metadata = output_by_port.get("metadata")
        assert isinstance(metadata, dict)
        assert metadata.get("handled_by") == "async-handle"
        assert metadata.get("preview") is False
        assert metadata.get("session_id") == run.session_id
        assert metadata.get("input_count") == 1
        assert metadata.get("entrypoint") == "workflow_external_success_worker:handle"
        assert isinstance(metadata.get("duration_ms"), float)
        assert metadata.get("duration_ms", -1.0) >= 0

        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.completed"
        assert dict(events[-1].payload or {}).get("status") == "completed"


def test_worker_persists_session_memory_across_queued_runs(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from caliber.workflows import runtime as workflow_runtime

    _enable_queue(client)
    workflow_id = "session-memory-worker-wf"
    manifest = _persistent_session_memory_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Session Memory Worker",
        manifest=manifest,
    )
    session_id = "SESSION-worker-persistent"

    shared_executor = FakeWorkflowExecutor()

    def _execute_with_fake_executor(plan, input_text: str, *, executor, **kwargs):
        del executor
        return workflow_runtime.execute(
            plan,
            input_text,
            executor=shared_executor,
            **kwargs,
        )

    monkeypatch.setattr(
        "caliber.orchestrator.workflow_run_worker.execute",
        _execute_with_fake_executor,
    )

    created_first = client.post(
        f"{PREFIX}/workflow-runs",
        json={
            "workflow_version_id": vid,
            "input": "hello",
            "session_id": session_id,
        },
    )
    assert created_first.status_code == 202
    first_run_id = created_first.json()["data"]["workflow_run_id"]

    created_second = client.post(
        f"{PREFIX}/workflow-runs",
        json={
            "workflow_version_id": vid,
            "input": "again",
            "session_id": session_id,
        },
    )
    assert created_second.status_code == 202
    second_run_id = created_second.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()
    worker._tick()

    assert shared_executor.history_calls == [
        [],
        [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "[test-agent] processed: hello"},
        ],
    ]

    with client.app.state.session_factory() as session:
        first_run = session.get(CaliberWorkflowRun, first_run_id)
        second_run = session.get(CaliberWorkflowRun, second_run_id)
        assert first_run is not None
        assert second_run is not None
        assert first_run.status == "completed"
        assert second_run.status == "completed"
        assert first_run.session_id == session_id
        assert second_run.session_id == session_id

        first_steps = first_run.summary.get("steps")
        second_steps = second_run.summary.get("steps")
        assert isinstance(first_steps, list)
        assert isinstance(second_steps, list)
        first_agent_step = next(
            step
            for step in first_steps
            if isinstance(step, dict) and step.get("node_id") == "agent"
        )
        second_agent_step = next(
            step
            for step in second_steps
            if isinstance(step, dict) and step.get("node_id") == "agent"
        )
        first_output_by_port = first_agent_step.get("output_by_port")
        second_output_by_port = second_agent_step.get("output_by_port")
        assert isinstance(first_output_by_port, dict)
        assert isinstance(second_output_by_port, dict)
        assert first_output_by_port.get("history") == [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "[test-agent] processed: hello"},
        ]
        assert second_output_by_port.get("history") == [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "[test-agent] processed: hello"},
            {"role": "user", "content": "again"},
            {"role": "assistant", "content": "[test-agent] processed: again"},
        ]

        memory_row = session.get(
            CaliberWorkflowSessionMemory,
            {
                "workflow_id": workflow_id,
                "node_id": "agent",
                "session_id": session_id,
            },
        )
        assert memory_row is not None
        assert memory_row.turn_count == 2
        assert memory_row.message_history == [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "[test-agent] processed: hello"},
            {"role": "user", "content": "again"},
            {"role": "assistant", "content": "[test-agent] processed: again"},
        ]

        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id.in_([first_run_id, second_run_id]))
            .order_by(
                CaliberWorkflowRunEvent.workflow_run_id.asc(),
                CaliberWorkflowRunEvent.sequence.asc(),
            )
            .all()
        )
        grouped_event_types: dict[str, list[str]] = {
            first_run_id: [],
            second_run_id: [],
        }
        for event in events:
            grouped_event_types[event.workflow_run_id].append(event.event_type)
        assert grouped_event_types[first_run_id][-1] == "workflow.run.completed"
        assert grouped_event_types[second_run_id][-1] == "workflow.run.completed"


def test_worker_marks_file_input_missing_path_failures_as_runtime_errors(
    client,
    tmp_path,
) -> None:
    _enable_queue(client)
    missing = tmp_path / "missing.txt"
    workflow_id = "file-input-missing-path-worker-wf"
    manifest = _file_input_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="File Input Missing Path Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": str(missing)},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.error_code == "runtime_error"
        assert run.error_summary is not None
        assert "FileNotFoundError: file input path does not exist" in run.error_summary
        assert str(missing) in run.error_summary
        assert run.completed_at is not None
        assert run.summary is not None
        assert "FileNotFoundError: file input path does not exist" in str(
            run.summary.get("error", "")
        )
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.failed"
        assert "FileNotFoundError: file input path does not exist" in str(
            events[-1].payload.get("error", "")
        )


def test_worker_executes_file_input_path_to_completion(client, tmp_path) -> None:
    _enable_queue(client)
    source = tmp_path / "input.txt"
    source.write_text("hello from a file", encoding="utf-8")
    workflow_id = "file-input-success-worker-wf"
    manifest = _file_input_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="File Input Success Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": str(source)},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.error_code is None
        assert run.completed_at is not None
        assert run.summary is not None
        assert run.summary.get("output") == "hello from a file"

        steps = run.summary.get("steps")
        assert isinstance(steps, list)
        file_step = next(
            step for step in steps if isinstance(step, dict) and step.get("node_id") == "file_input"
        )
        assert file_step.get("node_type") == "file_input"
        assert file_step.get("input_by_port") == {"path": str(source)}
        assert file_step.get("detail") == f"read 17 byte(s) from {source}"
        output_by_port = file_step.get("output_by_port")
        assert isinstance(output_by_port, dict)
        assert output_by_port.get("text") == "hello from a file"
        assert output_by_port.get("path") == str(source)
        assert output_by_port.get("metadata") == {
            "path": str(source),
            "bytes": 17,
            "truncated": False,
            "encoding": "utf-8",
        }

        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        event_types = [event.event_type for event in events]
        assert event_types[-1] == "workflow.run.completed"
        assert "workflow.run.failed" not in event_types
        assert event_types.count("workflow.run.step") >= 2


def test_worker_marks_file_input_directory_target_failures_as_runtime_errors(
    client,
    tmp_path,
) -> None:
    _enable_queue(client)
    folder = tmp_path / "folder-input"
    folder.mkdir()
    workflow_id = "file-input-directory-target-worker-wf"
    manifest = _file_input_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="File Input Directory Target Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": str(folder)},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.error_code == "runtime_error"
        assert run.error_summary is not None
        assert "IsADirectoryError" in run.error_summary
        assert str(folder) in run.error_summary
        assert run.completed_at is not None
        assert run.summary is not None
        assert "IsADirectoryError" in str(run.summary.get("error", ""))
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.failed"
        assert "IsADirectoryError" in str(events[-1].payload.get("error", ""))


def test_worker_marks_folder_input_missing_path_failures_as_runtime_errors(
    client,
    tmp_path,
) -> None:
    _enable_queue(client)
    missing = tmp_path / "missing-folder"
    workflow_id = "folder-input-missing-path-worker-wf"
    manifest = _folder_input_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Folder Input Missing Path Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": str(missing)},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.error_code == "runtime_error"
        assert run.error_summary is not None
        assert "FileNotFoundError: folder input path does not exist" in run.error_summary
        assert str(missing) in run.error_summary
        assert run.completed_at is not None
        assert run.summary is not None
        assert "FileNotFoundError: folder input path does not exist" in str(
            run.summary.get("error", "")
        )
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.failed"
        assert "FileNotFoundError: folder input path does not exist" in str(
            events[-1].payload.get("error", "")
        )


def test_worker_executes_folder_input_path_to_completion(client, tmp_path) -> None:
    _enable_queue(client)
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    (tmp_path / "b.log").write_text("beta", encoding="utf-8")
    workflow_id = "folder-input-success-worker-wf"
    manifest = _folder_input_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Folder Input Success Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": str(tmp_path)},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.error_code is None
        assert run.completed_at is not None
        assert run.summary is not None
        assert run.summary.get("output") == "--- a.txt ---\nalpha"

        steps = run.summary.get("steps")
        assert isinstance(steps, list)
        folder_step = next(
            step
            for step in steps
            if isinstance(step, dict) and step.get("node_id") == "folder_input"
        )
        assert folder_step.get("node_type") == "folder_input"
        assert folder_step.get("input_by_port") == {"path": str(tmp_path)}
        assert folder_step.get("detail") == f"read 1 file(s) from {tmp_path}"
        output_by_port = folder_step.get("output_by_port")
        assert isinstance(output_by_port, dict)
        assert output_by_port.get("text") == "--- a.txt ---\nalpha"
        assert output_by_port.get("files") == [
            {
                "path": str(tmp_path / "a.txt"),
                "relative_path": "a.txt",
                "bytes": 5,
                "truncated": False,
                "text": "alpha",
            }
        ]
        assert output_by_port.get("metadata") == {
            "path": str(tmp_path),
            "pattern": "*.txt",
            "recursive": False,
            "file_count": 1,
            "matched_count": 1,
            "max_files": 5,
            "truncated_file_list": False,
            "encoding": "utf-8",
        }

        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        event_types = [event.event_type for event in events]
        assert event_types[-1] == "workflow.run.completed"
        assert "workflow.run.failed" not in event_types
        assert event_types.count("workflow.run.step") >= 2


def test_worker_marks_folder_input_file_target_failures_as_runtime_errors(
    client,
    tmp_path,
) -> None:
    _enable_queue(client)
    file_path = tmp_path / "not-a-folder.txt"
    file_path.write_text("hello", encoding="utf-8")
    workflow_id = "folder-input-file-target-worker-wf"
    manifest = _folder_input_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Folder Input File Target Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": str(file_path)},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.error_code == "runtime_error"
        assert run.error_summary is not None
        assert "NotADirectoryError" in run.error_summary
        assert str(file_path) in run.error_summary
        assert run.completed_at is not None
        assert run.summary is not None
        assert "NotADirectoryError" in str(run.summary.get("error", ""))
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.failed"
        assert "NotADirectoryError" in str(events[-1].payload.get("error", ""))


def test_worker_marks_output_folder_invalid_target_failures_as_runtime_errors(
    client,
    tmp_path,
) -> None:
    _enable_queue(client)
    blocked = tmp_path / "existing-file.txt"
    blocked.write_text("not a directory", encoding="utf-8")
    workflow_id = "output-folder-missing-path-worker-wf"
    manifest = _output_folder_invalid_target_manifest(workflow_id, path=str(blocked))
    _wid, vid = create_and_publish(
        client,
        workflow_name="Output Folder Invalid Target Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.error_code == "runtime_error"
        assert run.error_summary is not None
        assert "FileExistsError" in run.error_summary
        assert str(blocked) in run.error_summary
        assert run.completed_at is not None
        assert run.summary is not None
        assert "FileExistsError" in str(run.summary.get("error", ""))
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.failed"
        assert "FileExistsError" in str(events[-1].payload.get("error", ""))


def test_worker_executes_output_folder_path_to_completion(client, tmp_path) -> None:
    _enable_queue(client)
    output_dir = tmp_path / "exports"
    workflow_id = "output-folder-success-worker-wf"
    manifest = _output_folder_invalid_target_manifest(workflow_id, path=str(output_dir))
    _wid, vid = create_and_publish(
        client,
        workflow_name="Output Folder Success Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    written_file = output_dir / "output.txt"
    assert written_file.exists()
    assert written_file.read_text(encoding="utf-8") == "[test-agent] processed: hello"

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.error_code is None
        assert run.completed_at is not None
        assert run.summary is not None
        assert run.summary.get("output") == "[test-agent] processed: hello"

        steps = run.summary.get("steps")
        assert isinstance(steps, list)
        output_folder_step = next(
            step
            for step in steps
            if isinstance(step, dict) and step.get("node_id") == "output_folder"
        )
        assert output_folder_step.get("node_type") == "output_folder"
        assert output_folder_step.get("input_by_port") == {"input": "[test-agent] processed: hello"}
        assert output_folder_step.get("detail") == f"wrote 1 file(s) to {output_dir}"
        output_by_port = output_folder_step.get("output_by_port")
        assert isinstance(output_by_port, dict)
        assert output_by_port.get("files") == [str(written_file)]
        assert output_by_port.get("metadata") == {
            "path": str(output_dir),
            "file_count": 1,
            "files": [str(written_file)],
        }

        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        event_types = [event.event_type for event in events]
        assert event_types[-1] == "workflow.run.completed"
        assert "workflow.run.failed" not in event_types
        assert event_types.count("workflow.run.step") >= 3


def test_worker_marks_output_bucket_storage_failures_as_runtime_errors(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FailingBackend:
        def write_bytes(self, key, data, *, media_type=None, overwrite=True):
            del data, media_type, overwrite
            raise StorageUnavailableError(f"backend unavailable for {key}")

    _enable_queue(client)
    monkeypatch.setattr(
        "caliber.workflows.runtime._bucket_io",
        lambda bucket, prefix: (_FailingBackend(), f"{bucket}/{prefix}".strip("/")),
    )
    workflow_id = "output-bucket-storage-failure-worker-wf"
    manifest = _output_bucket_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Output Bucket Storage Failure Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.error_code == "runtime_error"
        assert run.error_summary is not None
        assert "ToolExecutionError: output_bucket write failed" in run.error_summary
        assert "results/run1/output.txt" in run.error_summary
        assert run.completed_at is not None
        assert run.summary is not None
        assert "ToolExecutionError: output_bucket write failed" in str(run.summary.get("error", ""))
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.failed"
        assert "ToolExecutionError: output_bucket write failed" in str(
            events[-1].payload.get("error", "")
        )


def test_worker_reports_partial_output_bucket_progress_on_write_failure(
    client,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _enable_queue(client)
    bucket_io = _local_bucket_io(tmp_path)
    monkeypatch.setattr("caliber.workflows.runtime._bucket_io", bucket_io)
    backend, _key_prefix = bucket_io("results", "")
    original_write_bytes = backend.write_bytes
    write_calls: list[str] = []

    def _write_bytes_then_fail(key: str, data: bytes, *, media_type=None, overwrite=True):
        write_calls.append(key)
        if len(write_calls) > 1:
            raise StorageUnavailableError(f"backend unavailable for {key}")
        return original_write_bytes(
            key,
            data,
            media_type=media_type,
            overwrite=overwrite,
        )

    monkeypatch.setattr(backend, "write_bytes", _write_bytes_then_fail)

    workflow_id = "output-bucket-partial-write-failure-worker-wf"
    manifest = make_manifest(workflow_id)
    manifest["nodes"] = {
        "start": {
            "id": "start",
            "type": "start",
            "outputs": {"msg": {"type": "string"}},
        },
        "emit_artifacts": {
            "id": "emit_artifacts",
            "type": "python_code",
            "code": (
                'return {"text": "artifact-ready", '
                '"result": {"artifacts": {"a.txt": "hello", "b.txt": "world"}}}'
            ),
            "inputs": {"input": {"type": "string"}},
            "outputs": {
                "text": {"type": "string"},
                "result": {"type": "structured"},
            },
        },
        "output_bucket": {
            "id": "output_bucket",
            "type": "output_bucket",
            "bucket": "results",
            "prefix": "run1/",
            "inputs": {"input": {"type": "string"}},
            "outputs": {
                "keys": {"type": "structured"},
                "metadata": {"type": "structured"},
            },
        },
        "final": {
            "id": "final",
            "type": "output",
            "inputs": {"response": {"type": "string"}},
        },
    }
    manifest["edges"] = [
        {"id": "e_start_emit", "from": "start", "to": "emit_artifacts", "map": {"msg": "input"}},
        {
            "id": "e_emit_final",
            "from": "emit_artifacts",
            "to": "final",
            "map": {"text": "response"},
        },
        {
            "id": "e_emit_bucket",
            "from": "emit_artifacts",
            "to": "output_bucket",
            "map": {"text": "input"},
        },
    ]
    _wid, vid = create_and_publish(
        client,
        workflow_name="Output Bucket Partial Write Failure Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "emit artifacts"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    assert (tmp_path / "results" / "run1" / "a.txt").read_text(encoding="utf-8") == "hello"

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.error_code == "runtime_error"
        assert run.error_summary is not None
        assert "ToolExecutionError: output_bucket write failed" in run.error_summary
        assert "results/run1/b.txt" in run.error_summary
        assert "after writing 1 object(s)" in run.error_summary
        assert "results/run1/a.txt" in run.error_summary
        assert run.completed_at is not None
        assert run.summary is not None
        assert "ToolExecutionError: output_bucket write failed" in str(run.summary.get("error", ""))
        assert "after writing 1 object(s)" in str(run.summary.get("error", ""))
        assert "results/run1/a.txt" in str(run.summary.get("error", ""))
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.failed"
        assert "ToolExecutionError: output_bucket write failed" in str(
            events[-1].payload.get("error", "")
        )
        assert "after writing 1 object(s)" in str(events[-1].payload.get("error", ""))
        assert "results/run1/a.txt" in str(events[-1].payload.get("error", ""))


def test_worker_executes_output_bucket_path_to_completion(
    client,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _enable_queue(client)
    monkeypatch.setattr("caliber.workflows.runtime._bucket_io", _local_bucket_io(tmp_path))
    workflow_id = "output-bucket-success-worker-wf"
    manifest = _output_bucket_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Output Bucket Success Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    written_file = tmp_path / "results" / "run1" / "output.txt"
    assert written_file.exists()
    assert written_file.read_text(encoding="utf-8") == "[test-agent] processed: hello"

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.error_code is None
        assert run.completed_at is not None
        assert run.summary is not None
        assert run.summary.get("output") == "[test-agent] processed: hello"

        steps = run.summary.get("steps")
        assert isinstance(steps, list)
        output_bucket_step = next(
            step
            for step in steps
            if isinstance(step, dict) and step.get("node_id") == "output_bucket"
        )
        assert output_bucket_step.get("node_type") == "output_bucket"
        assert output_bucket_step.get("input_by_port") == {"input": "[test-agent] processed: hello"}
        assert output_bucket_step.get("detail") == "wrote 1 object(s) to results/run1/"
        output_by_port = output_bucket_step.get("output_by_port")
        assert isinstance(output_by_port, dict)
        assert output_by_port.get("keys") == ["results/run1/output.txt"]
        assert output_by_port.get("metadata") == {
            "bucket": "results",
            "prefix": "run1/",
            "object_count": 1,
            "keys": ["results/run1/output.txt"],
        }

        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        event_types = [event.event_type for event in events]
        assert event_types[-1] == "workflow.run.completed"
        assert "workflow.run.failed" not in event_types
        assert event_types.count("workflow.run.step") >= 3


def test_worker_executes_input_bucket_path_to_completion(
    client,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _enable_queue(client)
    bucket_io = _local_bucket_io(tmp_path)
    monkeypatch.setattr("caliber.workflows.runtime._bucket_io", bucket_io)
    backend, _key_prefix = bucket_io("docs", "")
    backend.write_bytes(
        "docs/run1/a.txt",
        b"hello",
        media_type="text/plain",
        overwrite=True,
    )

    workflow_id = "input-bucket-success-worker-wf"
    manifest = _input_bucket_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Input Bucket Success Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "run1/"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.error_code is None
        assert run.completed_at is not None
        assert run.summary is not None
        assert run.summary.get("output") == "--- a.txt ---\nhello"

        steps = run.summary.get("steps")
        assert isinstance(steps, list)
        input_bucket_step = next(
            step
            for step in steps
            if isinstance(step, dict) and step.get("node_id") == "input_bucket"
        )
        assert input_bucket_step.get("node_type") == "input_bucket"
        assert input_bucket_step.get("input_by_port") == {"prefix": "run1/"}
        assert input_bucket_step.get("detail") == "read 1 object(s) from docs/run1/"
        output_by_port = input_bucket_step.get("output_by_port")
        assert isinstance(output_by_port, dict)
        assert output_by_port.get("text") == "--- a.txt ---\nhello"
        assert output_by_port.get("files") == [
            {
                "key": "docs/run1/a.txt",
                "relative_path": "a.txt",
                "bytes": 5,
                "truncated": False,
                "text": "hello",
            }
        ]
        assert output_by_port.get("metadata") == {
            "bucket": "docs",
            "prefix": "run1/",
            "recursive": True,
            "object_count": 1,
            "max_files": 10,
            "skipped_object_count": 0,
            "truncated_file_list": False,
            "encoding": "utf-8",
        }

        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        event_types = [event.event_type for event in events]
        assert event_types[-1] == "workflow.run.completed"
        assert "workflow.run.failed" not in event_types
        assert event_types.count("workflow.run.step") >= 2


def test_worker_input_bucket_skips_unreadable_objects_and_completes(
    client,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _enable_queue(client)
    bucket_io = _local_bucket_io(tmp_path)
    monkeypatch.setattr("caliber.workflows.runtime._bucket_io", bucket_io)
    backend, _key_prefix = bucket_io("docs", "")
    backend.write_bytes(
        "docs/run1/a.txt",
        b"hello",
        media_type="text/plain",
        overwrite=True,
    )
    backend.write_bytes(
        "docs/run1/b.txt",
        b"blocked",
        media_type="text/plain",
        overwrite=True,
    )
    original_read_bytes = backend.read_bytes

    def _read_bytes_with_partial_failure(key: str) -> bytes:
        if key.endswith("/b.txt"):
            raise StorageUnavailableError(f"backend unavailable for {key}")
        return original_read_bytes(key)

    monkeypatch.setattr(backend, "read_bytes", _read_bytes_with_partial_failure)

    workflow_id = "input-bucket-partial-read-worker-wf"
    manifest = _input_bucket_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Input Bucket Partial Read Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "run1/"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.error_code is None
        assert run.completed_at is not None
        assert run.summary is not None
        assert run.summary.get("output") == "--- a.txt ---\nhello"

        steps = run.summary.get("steps")
        assert isinstance(steps, list)
        input_bucket_step = next(
            step
            for step in steps
            if isinstance(step, dict) and step.get("node_id") == "input_bucket"
        )
        assert input_bucket_step.get("node_type") == "input_bucket"
        assert input_bucket_step.get("input_by_port") == {"prefix": "run1/"}
        assert input_bucket_step.get("detail") == "read 1 object(s) from docs/run1/"
        output_by_port = input_bucket_step.get("output_by_port")
        assert isinstance(output_by_port, dict)
        assert output_by_port.get("text") == "--- a.txt ---\nhello"
        assert output_by_port.get("files") == [
            {
                "key": "docs/run1/a.txt",
                "relative_path": "a.txt",
                "bytes": 5,
                "truncated": False,
                "text": "hello",
            }
        ]
        assert output_by_port.get("metadata") == {
            "bucket": "docs",
            "prefix": "run1/",
            "recursive": True,
            "object_count": 1,
            "max_files": 10,
            "skipped_object_count": 1,
            "truncated_file_list": False,
            "encoding": "utf-8",
        }

        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        event_types = [event.event_type for event in events]
        assert event_types[-1] == "workflow.run.completed"
        assert "workflow.run.failed" not in event_types
        assert event_types.count("workflow.run.step") >= 2


def test_worker_input_bucket_missing_prefix_reads_empty_and_completes(
    client,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _enable_queue(client)
    bucket_io = _local_bucket_io(tmp_path)
    monkeypatch.setattr("caliber.workflows.runtime._bucket_io", bucket_io)

    workflow_id = "input-bucket-missing-prefix-worker-wf"
    manifest = _input_bucket_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Input Bucket Missing Prefix Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "run-missing/"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.error_code is None
        assert run.completed_at is not None
        assert run.summary is not None
        assert run.summary.get("output") == ""

        steps = run.summary.get("steps")
        assert isinstance(steps, list)
        input_bucket_step = next(
            step
            for step in steps
            if isinstance(step, dict) and step.get("node_id") == "input_bucket"
        )
        assert input_bucket_step.get("node_type") == "input_bucket"
        assert input_bucket_step.get("input_by_port") == {"prefix": "run-missing/"}
        assert input_bucket_step.get("detail") == "read 0 object(s) from docs/run-missing/"
        output_by_port = input_bucket_step.get("output_by_port")
        assert isinstance(output_by_port, dict)
        assert output_by_port.get("text") == ""
        assert output_by_port.get("files") == []
        assert output_by_port.get("metadata") == {
            "bucket": "docs",
            "prefix": "run-missing/",
            "recursive": True,
            "object_count": 0,
            "max_files": 10,
            "skipped_object_count": 0,
            "truncated_file_list": False,
            "encoding": "utf-8",
        }

        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        event_types = [event.event_type for event in events]
        assert event_types[-1] == "workflow.run.completed"
        assert "workflow.run.failed" not in event_types
        assert event_types.count("workflow.run.step") >= 2


def test_worker_input_bucket_marks_truncated_lists_in_summary(
    client,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _enable_queue(client)
    bucket_io = _local_bucket_io(tmp_path)
    monkeypatch.setattr("caliber.workflows.runtime._bucket_io", bucket_io)
    backend, _key_prefix = bucket_io("docs", "")
    backend.write_bytes(
        "docs/run1/a.txt",
        b"hello",
        media_type="text/plain",
        overwrite=True,
    )
    backend.write_bytes(
        "docs/run1/b.txt",
        b"world",
        media_type="text/plain",
        overwrite=True,
    )

    workflow_id = "input-bucket-truncated-worker-wf"
    manifest = _input_bucket_manifest(workflow_id)
    input_bucket_node = manifest["nodes"]["input_bucket"]
    assert isinstance(input_bucket_node, dict)
    input_bucket_node["max_files"] = 1
    _wid, vid = create_and_publish(
        client,
        workflow_name="Input Bucket Truncated Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "run1/"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.error_code is None
        assert run.completed_at is not None
        assert run.summary is not None
        assert run.summary.get("output") == "--- a.txt ---\nhello"

        steps = run.summary.get("steps")
        assert isinstance(steps, list)
        input_bucket_step = next(
            step
            for step in steps
            if isinstance(step, dict) and step.get("node_id") == "input_bucket"
        )
        assert input_bucket_step.get("node_type") == "input_bucket"
        assert input_bucket_step.get("input_by_port") == {"prefix": "run1/"}
        assert input_bucket_step.get("detail") == "read 1 object(s) from docs/run1/"
        output_by_port = input_bucket_step.get("output_by_port")
        assert isinstance(output_by_port, dict)
        assert output_by_port.get("text") == "--- a.txt ---\nhello"
        assert output_by_port.get("files") == [
            {
                "key": "docs/run1/a.txt",
                "relative_path": "a.txt",
                "bytes": 5,
                "truncated": False,
                "text": "hello",
            }
        ]
        assert output_by_port.get("metadata") == {
            "bucket": "docs",
            "prefix": "run1/",
            "recursive": True,
            "object_count": 1,
            "max_files": 1,
            "skipped_object_count": 0,
            "truncated_file_list": True,
            "encoding": "utf-8",
        }

        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        event_types = [event.event_type for event in events]
        assert event_types[-1] == "workflow.run.completed"
        assert "workflow.run.failed" not in event_types
        assert event_types.count("workflow.run.step") >= 2


def test_worker_marks_input_bucket_list_failures_as_runtime_errors(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FailingBackend:
        def list(self, prefix, *, recursive=True, limit=1000, cursor=None):
            del recursive, limit, cursor
            raise StorageUnavailableError(f"backend unavailable for {prefix}")

    _enable_queue(client)
    monkeypatch.setattr(
        "caliber.workflows.runtime._bucket_io",
        lambda bucket, prefix: (_FailingBackend(), f"{bucket}/{prefix}".strip("/")),
    )
    workflow_id = "input-bucket-storage-list-failure-worker-wf"
    manifest = _input_bucket_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Input Bucket Storage List Failure Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "run1/"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.error_code == "runtime_error"
        assert run.error_summary is not None
        assert "ToolExecutionError: input_bucket list failed" in run.error_summary
        assert "docs/run1/" in run.error_summary
        assert run.completed_at is not None
        assert run.summary is not None
        assert "ToolExecutionError: input_bucket list failed" in str(run.summary.get("error", ""))
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.failed"
        assert "ToolExecutionError: input_bucket list failed" in str(
            events[-1].payload.get("error", "")
        )


def test_worker_persists_start_and_output_steps_for_simple_completed_runs(client) -> None:
    _enable_queue(client)
    workflow_id = "start-output-success-worker-wf"
    manifest = make_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Start Output Success Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.error_code is None
        assert run.completed_at is not None
        assert run.summary is not None
        assert run.summary.get("output") == "[test-agent] processed: hello"

        steps = run.summary.get("steps")
        assert isinstance(steps, list)
        start_step = next(
            step for step in steps if isinstance(step, dict) and step.get("node_id") == "start"
        )
        assert start_step.get("node_type") == "start"
        assert start_step.get("status") == "ok"

        final_step = next(
            step for step in steps if isinstance(step, dict) and step.get("node_id") == "final"
        )
        assert final_step.get("node_type") == "output"
        assert final_step.get("status") == "ok"
        assert final_step.get("output") == "[test-agent] processed: hello"

        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        event_types = [event.event_type for event in events]
        assert event_types[-1] == "workflow.run.completed"
        assert "workflow.run.failed" not in event_types
        assert event_types.count("workflow.run.step") >= 3


def test_worker_marks_subworkflow_child_failures_as_runtime_errors(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from caliber.workflows import promoter as promoter_module

    _enable_queue(client)
    child_workflow_id, _child_version_id = create_and_publish(
        client,
        workflow_name="Child Workflow Failure Target",
    )

    workflow_id = "subworkflow-child-failure-worker-wf"
    manifest = _subworkflow_child_failure_manifest(
        workflow_id,
        child_workflow_id=child_workflow_id,
    )
    _wid, vid = create_and_publish(
        client,
        workflow_name="Subworkflow Child Failure Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "Escalate the refund exception."},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    original_execute = promoter_module.execute

    def _execute_with_child_failure(plan, *args, **kwargs):
        if plan.ir.workflow_id == child_workflow_id:
            return WorkflowRunResult(
                status="error",
                output="",
                error="child workflow checkpoint replay failed",
            )
        return original_execute(plan, *args, **kwargs)

    monkeypatch.setattr("caliber.workflows.promoter.execute", _execute_with_child_failure)

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.error_code == "runtime_error"
        assert run.error_summary is not None
        assert f"subworkflow {child_workflow_id!r}@'manual' failed" in run.error_summary
        assert "child workflow checkpoint replay failed" in run.error_summary
        assert run.completed_at is not None
        assert run.summary is not None
        assert "child workflow checkpoint replay failed" in str(run.summary.get("error", ""))
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.failed"
        assert "child workflow checkpoint replay failed" in str(events[-1].payload.get("error", ""))


def test_worker_executes_subworkflow_path_to_completion(client) -> None:
    _enable_queue(client)

    register_demo_tools(client)
    child_workflow_id = create_workflow(client, "Child Workflow Success Target")
    child_manifest = make_manifest(child_workflow_id)
    child_nodes = child_manifest["nodes"]
    assert isinstance(child_nodes, dict)
    child_agent = child_nodes["agent"]
    assert isinstance(child_agent, dict)
    child_agent["name"] = "child-agent"
    child_version_id, _ = create_draft(client, child_workflow_id, child_manifest)
    published = client.post(f"{PREFIX}/workflow-versions/{child_version_id}/publish")
    assert published.status_code == 200

    workflow_id = "subworkflow-success-worker-wf"
    manifest = _subworkflow_success_manifest(
        workflow_id,
        child_workflow_id=child_workflow_id,
    )
    _wid, vid = create_and_publish(
        client,
        workflow_name="Subworkflow Success Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "Escalate the refund exception."},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.error_code is None
        assert run.completed_at is not None
        assert run.summary is not None
        assert (
            run.summary.get("output") == "[child-agent] processed: Escalate the refund exception."
        )

        steps = run.summary.get("steps")
        assert isinstance(steps, list)
        child_step = next(
            step
            for step in steps
            if isinstance(step, dict) and step.get("node_id") == "child_workflow"
        )
        assert child_step.get("node_type") == "subworkflow"
        assert child_step.get("input_by_port") == {"input": "Escalate the refund exception."}
        child_output = child_step.get("output_by_port")
        assert isinstance(child_output, dict)
        assert (
            child_output.get("output") == "[child-agent] processed: Escalate the refund exception."
        )
        child_result = child_output.get("result")
        assert isinstance(child_result, dict)
        assert child_result.get("workflow_id") == child_workflow_id
        assert child_result.get("alias") == "manual"
        assert child_result.get("workflow_version_id") == child_version_id
        assert child_result.get("workflow_version_number") == 1
        assert child_result.get("status") == "completed"
        assert child_result.get("steps") == ["start", "agent", "final"]

        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.completed"
        completed_payload = dict(events[-1].payload or {})
        assert completed_payload.get("status") == "completed"

        run_ids = session.query(CaliberWorkflowRun.workflow_run_id).all()
        assert {item[0] for item in run_ids} == {run_id}


def test_worker_marks_external_app_import_failures_as_runtime_errors(client) -> None:
    _enable_queue(client)
    workflow_id = "external-app-invalid-worker-wf"
    manifest = _external_app_invalid_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="External App Invalid Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "approve"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.error_code == "runtime_error"
        assert run.error_summary is not None
        assert "could not import external_app module" in run.error_summary
        assert run.completed_at is not None
        assert run.summary is not None
        assert "could not import external_app module" in str(run.summary.get("error", ""))
        assert run.summary.get("node_path") == ["start"]
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.failed"
        assert "could not import external_app module" in str(events[-1].payload.get("error", ""))


def test_worker_marks_mcp_resource_gateway_failures_as_runtime_errors(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_queue(client)
    workflow_id = "mcp-resource-failure-worker-wf"
    manifest = _mcp_resource_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="MCP Resource Failure Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "refund policy"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

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

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.error_code == "runtime_error"
        assert run.error_summary is not None
        assert "MCP-DOCS/search_docs unavailable" in run.error_summary
        assert run.completed_at is not None
        assert run.summary is not None
        assert "MCP-DOCS/search_docs unavailable" in str(run.summary.get("error", ""))
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.failed"
        assert "MCP-DOCS/search_docs unavailable" in str(events[-1].payload.get("error", ""))


def test_worker_executes_mcp_resource_path_to_completion(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_queue(client)
    workflow_id = "mcp-resource-success-worker-wf"
    manifest = _mcp_resource_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="MCP Resource Success Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "refund policy"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

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
        captured["arguments"] = dict(arguments)
        captured["timeout_seconds"] = timeout_seconds
        return {"text": "Refund policy found"}

    monkeypatch.setattr("caliber.workflows.runtime.invoke_tool_by_server_id_sync", _fake_invoke)

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.error_code is None
        assert run.completed_at is not None
        assert run.summary is not None
        assert run.summary.get("output") == "Refund policy found"

        steps = run.summary.get("steps")
        assert isinstance(steps, list)
        mcp_step = next(
            step for step in steps if isinstance(step, dict) and step.get("node_id") == "mcp_lookup"
        )
        assert mcp_step.get("node_type") == "mcp_resource"
        assert mcp_step.get("input_by_port") == {"input": "refund policy"}
        assert mcp_step.get("detail") == "invoked search_docs on MCP-DOCS"
        output_by_port = mcp_step.get("output_by_port")
        assert isinstance(output_by_port, dict)
        assert output_by_port.get("text") == "Refund policy found"
        assert output_by_port.get("result") == {"text": "Refund policy found"}
        assert output_by_port.get("metadata") == {
            "server_id": "MCP-DOCS",
            "tool_name": "search_docs",
            "arguments": {"query": "refund policy"},
        }
        tool_calls = mcp_step.get("tool_calls")
        assert isinstance(tool_calls, list)
        assert tool_calls == [
            {
                "tool": "mcp:MCP-DOCS/search_docs",
                "server_id": "MCP-DOCS",
                "tool_name": "search_docs",
                "arguments": {"query": "refund policy"},
                "result": {"text": "Refund policy found"},
            }
        ]

        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        event_types = [event.event_type for event in events]
        assert event_types[-1] == "workflow.run.completed"
        assert "workflow.run.failed" not in event_types

    assert captured == {
        "server_id": "MCP-DOCS",
        "tool_name": "search_docs",
        "arguments": {"query": "refund policy"},
        "timeout_seconds": 30,
    }


def test_worker_marks_knowledge_build_runner_failures_as_runtime_errors(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_queue(client)
    workflow_id = "knowledge-build-failure-worker-wf"
    manifest = _knowledge_build_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Knowledge Build Failure Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "refresh docs"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    class _FailingKnowledgeService:
        def __init__(self, *, config, session_factory) -> None:
            self.config = config
            self.session_factory = session_factory

        def create_version(self, knowledge_base_id, request, *, identity, actor):
            del knowledge_base_id, request, identity, actor
            raise RuntimeError("knowledge build service unavailable")

    monkeypatch.setattr(
        "caliber.workflows.promoter.KnowledgeBaseService",
        _FailingKnowledgeService,
    )

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.error_code == "runtime_error"
        assert run.error_summary is not None
        assert "knowledge build service unavailable" in run.error_summary
        assert run.completed_at is not None
        assert run.summary is not None
        assert "knowledge build service unavailable" in str(run.summary.get("error", ""))
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.failed"
        assert "knowledge build service unavailable" in str(events[-1].payload.get("error", ""))


def test_worker_executes_knowledge_build_path_to_completion(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_queue(client)
    workflow_id = "knowledge-build-success-worker-wf"
    manifest = _knowledge_build_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Knowledge Build Success Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "refresh docs"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    captured: dict[str, object] = {}

    class _FakeModel:
        def __init__(self, **payload: object) -> None:
            self._payload = dict(payload)
            for key, value in payload.items():
                setattr(self, key, value)

        def model_dump(self, *, mode: str = "json") -> dict[str, object]:
            del mode
            return dict(self._payload)

    class _SuccessfulKnowledgeService:
        def __init__(self, *, config, session_factory) -> None:
            self.config = config
            self.session_factory = session_factory

        def create_version(self, knowledge_base_id, request, *, identity, actor):
            del identity, actor
            captured["knowledge_base_id"] = knowledge_base_id
            captured["request"] = request.model_dump(mode="json")
            knowledge_base = _FakeModel(knowledge_base_id=knowledge_base_id)
            version = _FakeModel(
                knowledge_base_version_id="KBV-2",
                version_number=2,
                status="queued",
                chunking_strategy="recursive",
                embedding_model="BAAI/bge-m3",
            )
            run = _FakeModel(
                knowledge_base_run_id="KBR-2",
                status="queued",
            )
            return SimpleNamespace(
                knowledge_base=knowledge_base,
                version=version,
                run=run,
            )

    monkeypatch.setattr(
        "caliber.workflows.promoter.KnowledgeBaseService",
        _SuccessfulKnowledgeService,
    )

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.error_code is None
        assert run.completed_at is not None
        assert run.summary is not None
        assert (
            run.summary.get("output")
            == "Knowledge build queued for v2 using recursive / BAAI/bge-m3."
        )
        assert captured["knowledge_base_id"] == "KB-1"
        assert captured["request"] == {
            "sources": None,
            "chunking_strategy": "recursive",
            "embedding_model": "BAAI/bge-m3",
            "chunking_config": {},
            "graph_config": None,
        }

        summary_steps = run.summary.get("steps")
        assert isinstance(summary_steps, list)
        build_step = next(
            step
            for step in summary_steps
            if isinstance(step, dict) and step.get("node_id") == "knowledge_build"
        )
        assert build_step.get("node_type") == "knowledge_build"
        assert build_step.get("detail") == "v2 · queued"
        output_by_port = build_step.get("output_by_port")
        assert isinstance(output_by_port, dict)
        assert (
            output_by_port.get("text")
            == "Knowledge build queued for v2 using recursive / BAAI/bge-m3."
        )
        assert output_by_port.get("status") == "queued"
        assert output_by_port.get("version_id") == "KBV-2"
        assert output_by_port.get("run_id") == "KBR-2"
        assert output_by_port.get("knowledge_base") == {"knowledge_base_id": "KB-1"}
        assert output_by_port.get("version") == {
            "knowledge_base_version_id": "KBV-2",
            "version_number": 2,
            "status": "queued",
            "chunking_strategy": "recursive",
            "embedding_model": "BAAI/bge-m3",
        }
        assert output_by_port.get("run") == {
            "knowledge_base_run_id": "KBR-2",
            "status": "queued",
        }
        result_payload = output_by_port.get("result")
        assert isinstance(result_payload, dict)
        assert (
            result_payload.get("summary")
            == "Knowledge build queued for v2 using recursive / BAAI/bge-m3."
        )
        assert result_payload.get("await_completion") == {
            "requested": False,
            "status": "not_requested",
            "timeout_seconds": 300.0,
        }
        assert result_payload.get("activation") == {
            "requested": False,
            "status": "skipped",
        }

        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.completed"
        assert dict(events[-1].payload or {}).get("status") == "completed"


def test_worker_executes_knowledge_age_build_starter_template(  # noqa: PLR0915
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_queue(client)
    workflow_name = "AGE Knowledge Build Starter Worker"
    workflow_id = create_workflow(client, workflow_name)
    manifest = _starter_manifest(
        "knowledge_age_build",
        workflow_id=workflow_id,
        workflow_name=workflow_name,
    )
    nodes = manifest["nodes"]
    assert isinstance(nodes, dict)
    build_graph = nodes["build_graph"]
    assert isinstance(build_graph, dict)
    build_graph["knowledge_base_id"] = "KB-AGE"
    version_id, _ = create_draft(client, workflow_id, manifest)
    published = client.post(f"{PREFIX}/workflow-versions/{version_id}/publish")
    assert published.status_code == 200

    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": version_id, "input": "refresh the graph profile"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    captured: dict[str, object] = {}

    class _FakeModel:
        def __init__(self, **payload: object) -> None:
            self._payload = dict(payload)
            for key, value in payload.items():
                setattr(self, key, value)

        def model_dump(self, *, mode: str = "json") -> dict[str, object]:
            del mode
            return dict(self._payload)

    class _SuccessfulKnowledgeService:
        def __init__(self, *, config, session_factory) -> None:
            self.config = config
            self.session_factory = session_factory

        def create_version(self, knowledge_base_id, request, *, identity, actor):
            del identity, actor
            captured["knowledge_base_id"] = knowledge_base_id
            captured["request"] = request.model_dump(mode="json")
            knowledge_base = _FakeModel(knowledge_base_id=knowledge_base_id)
            version = _FakeModel(
                knowledge_base_version_id="KBV-AGE-2",
                version_number=2,
                status="queued",
                chunking_strategy="recursive",
                embedding_model="sentence-transformers/all-MiniLM-L6-v2",
            )
            run = _FakeModel(
                knowledge_base_run_id="KBR-AGE-2",
                status="queued",
            )
            return SimpleNamespace(
                knowledge_base=knowledge_base,
                version=version,
                run=run,
            )

        def get_version(self, version_id, *, identity):
            del identity
            return _FakeModel(
                knowledge_base_version_id=version_id,
                version_number=2,
                status="completed",
                chunking_strategy="recursive",
                embedding_model="sentence-transformers/all-MiniLM-L6-v2",
                error_summary=None,
            )

        def get_knowledge_base(self, knowledge_base_id, *, identity):
            del identity
            return _FakeModel(
                knowledge_base_id=knowledge_base_id,
                active_version_id="KBV-AGE-1",
            )

        def list_runs(self, knowledge_base_id, *, identity):
            del knowledge_base_id, identity
            return [
                _FakeModel(
                    knowledge_base_run_id="KBR-AGE-2",
                    status="completed",
                )
            ]

        def activate_version(self, knowledge_base_id, version_id, *, identity, actor):
            del identity, actor
            return _FakeModel(
                knowledge_base_id=knowledge_base_id,
                active_version_id=version_id,
            )

    monkeypatch.setattr(
        "caliber.workflows.promoter.KnowledgeBaseService",
        _SuccessfulKnowledgeService,
    )

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.error_code is None
        assert run.completed_at is not None
        assert run.summary is not None
        assert (
            run.summary.get("output") == "Knowledge build completed for v2 using recursive / "
            "sentence-transformers/all-MiniLM-L6-v2. Activated as the knowledge base default."
        )
        summary_steps = run.summary.get("steps")
        assert isinstance(summary_steps, list)
        build_step = next(
            step
            for step in summary_steps
            if isinstance(step, dict) and step.get("node_id") == "build_graph"
        )
        assert build_step.get("node_type") == "knowledge_build"
        assert build_step.get("detail") == "v2 · completed · activated"
        output_by_port = build_step.get("output_by_port")
        assert isinstance(output_by_port, dict)
        assert output_by_port.get("text") == (
            "Knowledge build completed for v2 using recursive / "
            "sentence-transformers/all-MiniLM-L6-v2. Activated as the knowledge base default."
        )
        assert output_by_port.get("status") == "completed"
        assert output_by_port.get("version_id") == "KBV-AGE-2"
        assert output_by_port.get("run_id") == "KBR-AGE-2"
        assert output_by_port.get("knowledge_base") == {
            "knowledge_base_id": "KB-AGE",
            "active_version_id": "KBV-AGE-2",
        }
        result_payload = output_by_port.get("result")
        assert isinstance(result_payload, dict)
        assert result_payload.get("await_completion") == {
            "requested": True,
            "status": "completed",
            "timeout_seconds": 900.0,
        }
        assert result_payload.get("activation") == {
            "requested": True,
            "status": "activated",
            "active_version_id": "KBV-AGE-2",
        }
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.completed"

    assert captured["knowledge_base_id"] == "KB-AGE"
    request = captured["request"]
    assert isinstance(request, dict)
    assert request.get("sources") is None
    assert request.get("chunking_strategy") == "recursive"
    assert request.get("embedding_model") == "sentence-transformers/all-MiniLM-L6-v2"
    assert request.get("chunking_config") == {"chunk_size": 1200, "chunk_overlap": 180}
    graph_config = request.get("graph_config")
    assert isinstance(graph_config, dict)
    assert graph_config == {
        "extractor_backend": "heuristic",
        "spacy_model": None,
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


def test_worker_marks_subworkflow_missing_deployment_failures_as_runtime_errors(
    client,
) -> None:
    _enable_queue(client)
    workflow_id = "subworkflow-missing-deployment-worker-wf"
    manifest = _subworkflow_missing_deployment_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Subworkflow Missing Deployment Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "escalate refund exception"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.error_code == "runtime_error"
        assert run.error_summary is not None
        assert "no active deployment for subworkflow" in run.error_summary
        assert run.completed_at is not None
        assert run.summary is not None
        assert "no active deployment for subworkflow" in str(run.summary.get("error", ""))
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.failed"
        assert "no active deployment for subworkflow" in str(events[-1].payload.get("error", ""))


def test_worker_marks_knowledge_query_missing_active_version_failures_as_runtime_errors(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_queue(client)
    workflow_id = "knowledge-query-missing-active-worker-wf"
    manifest = _knowledge_query_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Knowledge Query Missing Active Version Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "refund policy"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    class _MissingActiveKnowledgeService:
        def __init__(self, *, config, session_factory) -> None:
            self.config = config
            self.session_factory = session_factory

        def get_knowledge_base(self, knowledge_base_id: str, *, identity):
            del knowledge_base_id, identity
            return SimpleNamespace(active_version_id=None)

    monkeypatch.setattr(
        "caliber.workflows.promoter.KnowledgeBaseService",
        _MissingActiveKnowledgeService,
    )

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.error_code == "runtime_error"
        assert run.error_summary is not None
        assert "has no active version" in run.error_summary
        assert run.completed_at is not None
        assert run.summary is not None
        assert "has no active version" in str(run.summary.get("error", ""))
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.failed"
        assert "has no active version" in str(events[-1].payload.get("error", ""))


def test_worker_marks_knowledge_query_service_failures_as_runtime_errors(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_queue(client)
    workflow_id = "knowledge-query-service-failure-worker-wf"
    manifest = _knowledge_query_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Knowledge Query Service Failure Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "refund policy"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    class _FailingKnowledgeService:
        def __init__(self, *, config, session_factory) -> None:
            self.config = config
            self.session_factory = session_factory

        def get_knowledge_base(self, knowledge_base_id: str, *, identity):
            del knowledge_base_id, identity
            return SimpleNamespace(active_version_id="KBV-active")

        def query(self, payload, *, identity):
            del payload, identity
            raise RuntimeError("knowledge query service unavailable")

    monkeypatch.setattr(
        "caliber.workflows.promoter.KnowledgeBaseService",
        _FailingKnowledgeService,
    )

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.error_code == "runtime_error"
        assert run.error_summary is not None
        assert "knowledge query service unavailable" in run.error_summary
        assert run.completed_at is not None
        assert run.summary is not None
        assert "knowledge query service unavailable" in str(run.summary.get("error", ""))
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.failed"
        assert "knowledge query service unavailable" in str(events[-1].payload.get("error", ""))


def test_worker_completes_error_boundary_recovery_for_knowledge_query_failures(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_queue(client)
    workflow_id = "error-boundary-knowledge-recovery-worker-wf"
    manifest = _error_boundary_knowledge_recovery_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Error Boundary Knowledge Recovery Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "refund policy"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    class _MissingActiveKnowledgeService:
        def __init__(self, *, config, session_factory) -> None:
            self.config = config
            self.session_factory = session_factory

        def get_knowledge_base(self, knowledge_base_id: str, *, identity):
            del knowledge_base_id, identity
            return SimpleNamespace(active_version_id=None)

    monkeypatch.setattr(
        "caliber.workflows.promoter.KnowledgeBaseService",
        _MissingActiveKnowledgeService,
    )

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.completed_at is not None
        assert run.summary is not None
        assert run.summary["output"] == "recovered:refund policy"
        summary_steps = run.summary.get("steps")
        assert isinstance(summary_steps, list)
        assert any(
            isinstance(step, dict)
            and step.get("node_id") == "boundary"
            and "handled error" in str(step.get("detail", ""))
            for step in summary_steps
        )
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.completed"


def test_worker_completes_error_boundary_recovery_for_external_app_failures(client) -> None:
    _enable_queue(client)
    workflow_id = "error-boundary-external-recovery-worker-wf"
    manifest = _error_boundary_external_app_recovery_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Error Boundary External App Recovery Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "approve"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.completed_at is not None
        assert run.summary is not None
        assert run.summary["output"] == "recovered:approve"
        summary_steps = run.summary.get("steps")
        assert isinstance(summary_steps, list)
        boundary_step = next(
            step
            for step in summary_steps
            if isinstance(step, dict) and step.get("node_id") == "boundary"
        )
        assert "handled error" in str(boundary_step.get("detail", ""))
        output_by_port = boundary_step.get("output_by_port")
        assert isinstance(output_by_port, dict)
        error_port = output_by_port.get("error")
        assert isinstance(error_port, dict)
        assert error_port.get("target_node_type") == "external_app"
        assert error_port.get("compensation_node_type") == "python_code"
        compensation_outputs = error_port.get("compensation_outputs")
        assert isinstance(compensation_outputs, dict)
        assert compensation_outputs.get("result", {}).get("result", {}).get("ok") is True
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.completed"


def test_worker_completes_error_boundary_recovery_for_tool_failures(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_queue(client)
    workflow_id = "error-boundary-tool-recovery-worker-wf"
    manifest = _error_boundary_tool_recovery_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Error Boundary Tool Recovery Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "refund policy"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    def _boom(query: str = "") -> dict[str, str]:
        raise RuntimeError(f"boom: {query}")

    monkeypatch.setattr("caliber.workflows.demo_tools.lookup_policy", _boom)

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.completed_at is not None
        assert run.summary is not None
        assert run.summary["output"] == "recovered:refund policy"
        summary_steps = run.summary.get("steps")
        assert isinstance(summary_steps, list)
        boundary_step = next(
            step
            for step in summary_steps
            if isinstance(step, dict) and step.get("node_id") == "boundary"
        )
        assert "handled error" in str(boundary_step.get("detail", ""))
        output_by_port = boundary_step.get("output_by_port")
        assert isinstance(output_by_port, dict)
        error_port = output_by_port.get("error")
        assert isinstance(error_port, dict)
        assert error_port.get("target_node_type") == "tool"
        assert error_port.get("compensation_node_type") == "python_code"
        compensation_outputs = error_port.get("compensation_outputs")
        assert isinstance(compensation_outputs, dict)
        assert compensation_outputs.get("result", {}).get("result", {}).get("ok") is True
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.completed"


def test_worker_completes_error_boundary_recovery_for_mcp_resource_failures(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_queue(client)
    workflow_id = "error-boundary-mcp-recovery-worker-wf"
    manifest = _error_boundary_mcp_resource_recovery_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Error Boundary MCP Recovery Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "refund policy"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

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

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.completed_at is not None
        assert run.summary is not None
        assert run.summary["output"] == "recovered:refund policy"
        summary_steps = run.summary.get("steps")
        assert isinstance(summary_steps, list)
        boundary_step = next(
            step
            for step in summary_steps
            if isinstance(step, dict) and step.get("node_id") == "boundary"
        )
        assert "handled error" in str(boundary_step.get("detail", ""))
        output_by_port = boundary_step.get("output_by_port")
        assert isinstance(output_by_port, dict)
        error_port = output_by_port.get("error")
        assert isinstance(error_port, dict)
        assert error_port.get("target_node_type") == "mcp_resource"
        assert error_port.get("compensation_node_type") == "python_code"
        compensation_outputs = error_port.get("compensation_outputs")
        assert isinstance(compensation_outputs, dict)
        assert compensation_outputs.get("result", {}).get("result", {}).get("ok") is True
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.completed"


def test_worker_completes_error_boundary_recovery_for_knowledge_build_failures(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_queue(client)
    workflow_id = "error-boundary-knowledge-build-recovery-worker-wf"
    manifest = _error_boundary_knowledge_build_recovery_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Error Boundary Knowledge Build Recovery Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "refresh docs"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    class _FailingKnowledgeService:
        def __init__(self, *, config, session_factory) -> None:
            self.config = config
            self.session_factory = session_factory

        def create_version(self, knowledge_base_id, request, *, identity, actor):
            del knowledge_base_id, request, identity, actor
            raise RuntimeError("knowledge build service unavailable")

    monkeypatch.setattr(
        "caliber.workflows.promoter.KnowledgeBaseService",
        _FailingKnowledgeService,
    )

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.completed_at is not None
        assert run.summary is not None
        assert run.summary["output"] == "recovered:refresh docs"
        summary_steps = run.summary.get("steps")
        assert isinstance(summary_steps, list)
        boundary_step = next(
            step
            for step in summary_steps
            if isinstance(step, dict) and step.get("node_id") == "boundary"
        )
        assert "handled error" in str(boundary_step.get("detail", ""))
        output_by_port = boundary_step.get("output_by_port")
        assert isinstance(output_by_port, dict)
        error_port = output_by_port.get("error")
        assert isinstance(error_port, dict)
        assert error_port.get("target_node_type") == "knowledge_build"
        assert error_port.get("compensation_node_type") == "python_code"
        compensation_outputs = error_port.get("compensation_outputs")
        assert isinstance(compensation_outputs, dict)
        assert compensation_outputs.get("result", {}).get("result", {}).get("ok") is True
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.completed"


def test_worker_completes_error_boundary_recovery_for_subworkflow_failures(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from caliber.workflows import promoter as promoter_module

    _enable_queue(client)
    child_workflow_id, _child_version_id = create_and_publish(
        client,
        workflow_name="Child Workflow Recovery Target",
    )

    workflow_id = "error-boundary-subworkflow-recovery-worker-wf"
    manifest = _error_boundary_subworkflow_recovery_manifest(
        workflow_id,
        child_workflow_id=child_workflow_id,
    )
    _wid, vid = create_and_publish(
        client,
        workflow_name="Error Boundary Subworkflow Recovery Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "Escalate the refund exception."},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    original_execute = promoter_module.execute

    def _execute_with_child_failure(plan, *args, **kwargs):
        if plan.ir.workflow_id == child_workflow_id:
            return WorkflowRunResult(
                status="error",
                output="",
                error="child workflow checkpoint replay failed",
            )
        return original_execute(plan, *args, **kwargs)

    monkeypatch.setattr("caliber.workflows.promoter.execute", _execute_with_child_failure)

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.completed_at is not None
        assert run.summary is not None
        assert run.summary["output"] == "recovered:Escalate the refund exception."
        summary_steps = run.summary.get("steps")
        assert isinstance(summary_steps, list)
        boundary_step = next(
            step
            for step in summary_steps
            if isinstance(step, dict) and step.get("node_id") == "boundary"
        )
        assert "handled error" in str(boundary_step.get("detail", ""))
        output_by_port = boundary_step.get("output_by_port")
        assert isinstance(output_by_port, dict)
        error_port = output_by_port.get("error")
        assert isinstance(error_port, dict)
        assert error_port.get("target_node_type") == "subworkflow"
        assert error_port.get("compensation_node_type") == "python_code"
        compensation_outputs = error_port.get("compensation_outputs")
        assert isinstance(compensation_outputs, dict)
        assert compensation_outputs.get("result", {}).get("result", {}).get("ok") is True
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.completed"


def test_worker_completes_error_boundary_recovery_for_python_code_failures(client) -> None:
    _enable_queue(client)
    workflow_id = "error-boundary-python-recovery-worker-wf"
    manifest = _error_boundary_python_recovery_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Error Boundary Python Recovery Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "refund policy"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.completed_at is not None
        assert run.summary is not None
        assert run.summary["output"] == "recovered:refund policy"
        summary_steps = run.summary.get("steps")
        assert isinstance(summary_steps, list)
        boundary_step = next(
            step
            for step in summary_steps
            if isinstance(step, dict) and step.get("node_id") == "boundary"
        )
        assert "handled error" in str(boundary_step.get("detail", ""))
        output_by_port = boundary_step.get("output_by_port")
        assert isinstance(output_by_port, dict)
        error_port = output_by_port.get("error")
        assert isinstance(error_port, dict)
        assert error_port.get("target_node_type") == "python_code"
        assert error_port.get("compensation_node_type") == "python_code"
        compensation_outputs = error_port.get("compensation_outputs")
        assert isinstance(compensation_outputs, dict)
        assert compensation_outputs.get("result", {}).get("result", {}).get("ok") is True
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.completed"


def test_worker_completes_error_boundary_recovery_for_agent_failures(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from caliber.workflows import runtime as workflow_runtime

    _enable_queue(client)
    workflow_id = "error-boundary-agent-recovery-worker-wf"
    manifest = _error_boundary_agent_recovery_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Error Boundary Agent Recovery Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "refund policy"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    class _BoomExecutor(FakeWorkflowExecutor):
        def run_agent(self, agent, input_text, *, history=None, tool_callables, preview):  # type: ignore[override]
            del agent, history, tool_callables, preview
            raise RuntimeError(f"boom:{input_text}")

    def _execute_with_booming_agent(plan, input_text: str, *, executor, **kwargs):
        del executor
        return workflow_runtime.execute(
            plan,
            input_text,
            executor=_BoomExecutor(),
            **kwargs,
        )

    monkeypatch.setattr(
        "caliber.orchestrator.workflow_run_worker.execute",
        _execute_with_booming_agent,
    )

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.completed_at is not None
        assert run.summary is not None
        assert run.summary["output"] == "recovered:refund policy"
        summary_steps = run.summary.get("steps")
        assert isinstance(summary_steps, list)
        boundary_step = next(
            step
            for step in summary_steps
            if isinstance(step, dict) and step.get("node_id") == "boundary"
        )
        assert "handled error" in str(boundary_step.get("detail", ""))
        output_by_port = boundary_step.get("output_by_port")
        assert isinstance(output_by_port, dict)
        error_port = output_by_port.get("error")
        assert isinstance(error_port, dict)
        assert error_port.get("target_node_type") == "agent"
        assert error_port.get("compensation_node_type") == "python_code"
        compensation_outputs = error_port.get("compensation_outputs")
        assert isinstance(compensation_outputs, dict)
        assert compensation_outputs.get("result", {}).get("result", {}).get("ok") is True
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.completed"


def test_worker_completes_error_boundary_recovery_for_template_failures(client) -> None:
    _enable_queue(client)
    workflow_id = "error-boundary-template-recovery-worker-wf"
    manifest = _error_boundary_template_recovery_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Error Boundary Template Recovery Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "refund policy"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.completed_at is not None
        assert run.summary is not None
        assert run.summary["output"] == "recovered:refund policy"
        summary_steps = run.summary.get("steps")
        assert isinstance(summary_steps, list)
        boundary_step = next(
            step
            for step in summary_steps
            if isinstance(step, dict) and step.get("node_id") == "boundary"
        )
        assert "handled error" in str(boundary_step.get("detail", ""))
        output_by_port = boundary_step.get("output_by_port")
        assert isinstance(output_by_port, dict)
        error_port = output_by_port.get("error")
        assert isinstance(error_port, dict)
        assert error_port.get("target_node_type") == "template"
        assert error_port.get("compensation_node_type") == "python_code"
        compensation_outputs = error_port.get("compensation_outputs")
        assert isinstance(compensation_outputs, dict)
        assert compensation_outputs.get("result", {}).get("result", {}).get("ok") is True
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.completed"


def test_worker_marks_error_boundary_compensation_failures_as_runtime_errors(client) -> None:
    _enable_queue(client)
    workflow_id = "error-boundary-template-compensation-failure-worker-wf"
    manifest = _error_boundary_template_compensation_failure_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Error Boundary Template Compensation Failure Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "refund policy"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.error_code == "runtime_error"
        assert run.error_summary is not None
        assert (
            "error_boundary node 'boundary' compensation target 'python_recover' failed"
            in run.error_summary
        )
        assert "handling target 'template_fail'" in run.error_summary
        assert "original error: template references missing variable" in run.error_summary
        assert "compensation error: python_code node 'python_recover' failed" in run.error_summary
        assert "compensation boom:refund policy" in run.error_summary
        assert run.completed_at is not None
        assert run.summary is not None
        assert "error_boundary node 'boundary' compensation target 'python_recover' failed" in str(
            run.summary.get("error", "")
        )
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.failed"
        assert "error_boundary node 'boundary' compensation target 'python_recover' failed" in str(
            events[-1].payload.get("error", "")
        )


def test_worker_completes_for_each_partial_failure_runs(client) -> None:
    _enable_queue(client)
    workflow_id = "for-each-partial-failure-worker-wf"
    manifest = _for_each_partial_failure_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="For Each Partial Failure Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": '["a","b","c","d"]'},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.completed_at is not None
        assert run.summary is not None
        assert run.summary["output"] == "a\nb\n\nd"
        summary_steps = run.summary.get("steps")
        assert isinstance(summary_steps, list)
        for_each_step = next(
            step
            for step in summary_steps
            if isinstance(step, dict) and step.get("node_id") == "for_each"
        )
        assert "processed 4 item(s) via python_code (1 failed)" in str(
            for_each_step.get("detail", "")
        )
        output_by_port = for_each_step.get("output_by_port")
        assert isinstance(output_by_port, dict)
        assert output_by_port["metadata"]["failed"] == 1
        results = output_by_port["results"]
        assert isinstance(results, list)
        assert results[2]["item"] == "c"
        assert "python_code node 'python' failed" in results[2]["error"]
        assert "boom:c" in results[2]["error"]
        assert results[2]["output"] == ""
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.completed"


def test_worker_completes_for_each_agent_runs(client) -> None:
    _enable_queue(client)
    workflow_id = "for-each-agent-worker-wf"
    manifest = _for_each_agent_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="For Each Agent Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": '["alpha","beta","gamma"]'},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.completed_at is not None
        assert run.summary is not None
        expected_outputs = [
            "[test-agent] processed: alpha",
            "[test-agent] processed: beta",
            "[test-agent] processed: gamma",
        ]
        assert run.summary["output"] == "\n".join(expected_outputs)
        summary_steps = run.summary.get("steps")
        assert isinstance(summary_steps, list)
        for_each_step = next(
            step
            for step in summary_steps
            if isinstance(step, dict) and step.get("node_id") == "for_each"
        )
        assert "processed 3 item(s) via agent" in str(for_each_step.get("detail", ""))
        output_by_port = for_each_step.get("output_by_port")
        assert isinstance(output_by_port, dict)
        assert output_by_port["metadata"]["count"] == 3
        assert output_by_port["metadata"]["failed"] == 0
        assert output_by_port["metadata"]["target_node_type"] == "agent"
        results = output_by_port["results"]
        assert isinstance(results, list)
        assert [item["output"] for item in results] == expected_outputs
        assert [item["node_type"] for item in results] == ["agent", "agent", "agent"]
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.completed"


def test_worker_completes_for_each_python_target_runs(client) -> None:
    _enable_queue(client)
    workflow_id = "for-each-python-worker-wf"
    manifest = _for_each_python_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="For Each Python Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": '["alpha","beta"]'},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.completed_at is not None
        assert run.summary is not None
        assert run.summary["output"] == "ALPHA\nBETA"
        summary_steps = run.summary.get("steps")
        assert isinstance(summary_steps, list)
        for_each_step = next(
            step
            for step in summary_steps
            if isinstance(step, dict) and step.get("node_id") == "for_each"
        )
        assert "processed 2 item(s) via python_code" in str(for_each_step.get("detail", ""))
        output_by_port = for_each_step.get("output_by_port")
        assert isinstance(output_by_port, dict)
        assert output_by_port["metadata"]["count"] == 2
        assert output_by_port["metadata"]["failed"] == 0
        assert output_by_port["metadata"]["target_node_type"] == "python_code"
        results = output_by_port["results"]
        assert isinstance(results, list)
        assert [item["output"] for item in results] == ["ALPHA", "BETA"]
        assert results[0]["outputs"]["result"]["result"]["seen"] == "alpha"
        assert results[1]["outputs"]["result"]["result"]["seen"] == "beta"
        assert [item["node_type"] for item in results] == ["python_code", "python_code"]
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.completed"


def test_worker_marks_python_code_runtime_failures_as_runtime_errors(client) -> None:
    _enable_queue(client)
    workflow_id = "python-code-failure-worker-wf"
    manifest = _python_code_failure_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Python Code Failure Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "refund"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.error_code == "runtime_error"
        assert run.error_summary is not None
        assert "python_code node 'python' failed" in run.error_summary
        assert run.completed_at is not None
        assert run.summary is not None
        assert "python_code node 'python' failed" in str(run.summary.get("error", ""))
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.failed"
        assert "python_code node 'python' failed" in str(events[-1].payload.get("error", ""))


def test_worker_executes_python_code_path_to_completion(client) -> None:
    _enable_queue(client)
    workflow_id = "python-code-success-worker-wf"
    manifest = _python_code_success_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Python Code Success Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "refund"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.error_code is None
        assert run.completed_at is not None
        assert run.summary is not None
        assert run.summary.get("output") == "REFUND"

        steps = run.summary.get("steps")
        assert isinstance(steps, list)
        python_step = next(
            step for step in steps if isinstance(step, dict) and step.get("node_id") == "python"
        )
        assert python_step.get("node_type") == "python_code"
        assert python_step.get("input_by_port") == {"input": "refund"}
        assert "sandbox duration:" in str(python_step.get("detail", ""))
        output_by_port = python_step.get("output_by_port")
        assert isinstance(output_by_port, dict)
        assert output_by_port.get("text") == "REFUND"
        assert output_by_port.get("result") == {
            "text": "REFUND",
            "result": {"chars": 6},
        }
        metadata = output_by_port.get("metadata")
        assert isinstance(metadata, dict)
        assert metadata.get("status") == "completed"
        assert isinstance(metadata.get("duration_ms"), float)
        assert metadata.get("duration_ms", -1.0) >= 0
        assert metadata.get("stdout") == ""
        assert metadata.get("stderr") == ""

        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        event_types = [event.event_type for event in events]
        assert event_types[-1] == "workflow.run.completed"
        assert "workflow.run.failed" not in event_types
        assert event_types.count("workflow.run.step") >= 2


def test_worker_executes_template_path_to_completion(client) -> None:
    _enable_queue(client)
    workflow_id = "template-success-worker-wf"
    manifest = _template_success_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Template Success Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "refund"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.error_code is None
        assert run.completed_at is not None
        assert run.summary is not None
        assert run.summary.get("output") == "Hello refund"

        steps = run.summary.get("steps")
        assert isinstance(steps, list)
        template_step = next(
            step for step in steps if isinstance(step, dict) and step.get("node_id") == "template"
        )
        assert template_step.get("node_type") == "template"
        assert template_step.get("input_by_port") == {"input": "refund"}
        assert template_step.get("detail") == "rendered text template · 1 variable"
        output_by_port = template_step.get("output_by_port")
        assert isinstance(output_by_port, dict)
        assert output_by_port.get("text") == "Hello refund"
        assert output_by_port.get("result") == {"rendered": "Hello refund"}
        assert output_by_port.get("metadata") == {
            "output_format": "text",
            "missing_variable_mode": "preserve",
            "used_variables": ["input"],
            "missing_variables": [],
            "rendered_bytes": len(b"Hello refund"),
        }

        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        event_types = [event.event_type for event in events]
        assert event_types[-1] == "workflow.run.completed"
        assert "workflow.run.failed" not in event_types
        assert event_types.count("workflow.run.step") >= 2


def test_worker_marks_template_missing_variable_failures_as_runtime_errors(client) -> None:
    _enable_queue(client)
    workflow_id = "template-missing-variable-worker-wf"
    manifest = _template_missing_variable_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Template Missing Variable Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "refund"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.error_code == "runtime_error"
        assert run.error_summary is not None
        assert "template references missing variable" in run.error_summary
        assert run.completed_at is not None
        assert run.summary is not None
        assert "template references missing variable" in str(run.summary.get("error", ""))
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.failed"
        assert "template references missing variable" in str(events[-1].payload.get("error", ""))


def test_worker_retry_from_ancestor_approval_checkpoint_preserves_the_gate(client) -> None:
    _enable_runtime_approvals(client)
    workflow_id = "approval-retry-from-checkpoint-wf"
    manifest = _approval_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Approval Retry From Checkpoint Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "recover from approval checkpoint"},
    )
    assert created.status_code == 202
    original_run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        original = session.get(CaliberWorkflowRun, original_run_id)
        assert original is not None
        checkpoint_id = dict(original.summary or {}).get("resume_checkpoint_id")
        assert isinstance(checkpoint_id, str) and checkpoint_id
        original.status = "failed"
        original.completed_at = datetime.now(timezone.utc)
        original.error_code = "simulated_failure"
        original.error_summary = "operator requested checkpoint recovery"
        session.commit()

    retried = client.post(
        f"{PREFIX}/workflow-runs/{original_run_id}/retry",
        json={"checkpoint_id": checkpoint_id, "reason": "recover from stored checkpoint"},
    )
    assert retried.status_code == 202, retried.text
    retried_run_id = retried.json()["data"]["workflow_run_id"]

    worker._tick()

    final = client.get(f"{PREFIX}/workflow-runs/{retried_run_id}")
    assert final.status_code == 200
    data = final.json()["data"]
    assert data["status"] == "waiting_approval"
    assert data["current_node_id"] == "human_gate"

    approvals = client.get(f"{PREFIX}/workflow-runs/{retried_run_id}/approvals")
    assert approvals.status_code == 200
    approval_rows = approvals.json()["data"]
    assert len(approval_rows) == 1
    assert approval_rows[0]["status"] == "pending"
    assert approval_rows[0]["node_id"] == "human_gate"


def test_worker_retry_from_gate_checkpoint_corrupted_to_generic_replay_fails_closed(client) -> None:
    _enable_runtime_approvals(client)
    workflow_id = "approval-retry-generic-gate-replay-worker-wf"
    manifest = _approval_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Approval Retry Generic Gate Replay Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "recover from generic gate checkpoint"},
    )
    assert created.status_code == 202
    original_run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        original = session.get(CaliberWorkflowRun, original_run_id)
        assert original is not None
        checkpoint_id = dict(original.summary or {}).get("resume_checkpoint_id")
        assert isinstance(checkpoint_id, str) and checkpoint_id
        ancestor_checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert ancestor_checkpoint is not None
        assert isinstance(ancestor_checkpoint.state_blob, dict)
        ancestor_checkpoint.state_blob = {
            "kind": "generic_checkpoint",
            "node_id": "human_gate",
            "output": "approved output",
            "output_by_port": {"request": "approved output"},
        }
        original.status = "failed"
        original.completed_at = datetime.now(timezone.utc)
        original.error_code = "simulated_failure"
        original.error_summary = "operator requested checkpoint recovery"
        session.commit()

    retried = client.post(
        f"{PREFIX}/workflow-runs/{original_run_id}/retry",
        json={"checkpoint_id": checkpoint_id, "reason": "recover from stored checkpoint"},
    )
    assert retried.status_code == 202, retried.text
    retried_run_id = retried.json()["data"]["workflow_run_id"]

    worker._tick()
    _assert_fail_closed_retry_state(
        client,
        retried_run_id=retried_run_id,
        checkpoint_id=checkpoint_id,
        source_run_id=original_run_id,
    )


def test_worker_retry_from_runtime_tool_approval_checkpoint_corrupted_to_generic_replay_fails_closed(
    client,
) -> None:
    _enable_runtime_approvals(client)
    workflow_id = "tool-approval-retry-generic-gate-replay-worker-wf"
    manifest = _tool_approval_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Tool Approval Retry Generic Gate Replay Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "recover gated tool replay"},
    )
    assert created.status_code == 202
    original_run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        original = session.get(CaliberWorkflowRun, original_run_id)
        assert original is not None
        checkpoint_id = dict(original.summary or {}).get("resume_checkpoint_id")
        assert isinstance(checkpoint_id, str) and checkpoint_id
        ancestor_checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert ancestor_checkpoint is not None
        assert isinstance(ancestor_checkpoint.state_blob, dict)
        ancestor_checkpoint.state_blob = {
            "kind": "generic_checkpoint",
            "node_id": "tool_gate",
            "output": "executed recover gated tool replay",
            "output_by_port": {"text": "executed recover gated tool replay"},
        }
        original.status = "failed"
        original.completed_at = datetime.now(timezone.utc)
        original.error_code = "simulated_failure"
        original.error_summary = "operator requested checkpoint recovery"
        session.commit()

    retried = client.post(
        f"{PREFIX}/workflow-runs/{original_run_id}/retry",
        json={
            "checkpoint_id": checkpoint_id,
            "reason": "recover from stored gated tool checkpoint",
        },
    )
    assert retried.status_code == 202, retried.text
    retried_run_id = retried.json()["data"]["workflow_run_id"]

    worker._tick()
    _assert_fail_closed_retry_state(
        client,
        retried_run_id=retried_run_id,
        checkpoint_id=checkpoint_id,
        source_run_id=original_run_id,
    )


@pytest.mark.parametrize(
    ("workflow_name", "manifest_factory", "input_text", "corrupted_output"),
    [
        (
            "Wait Event Retry Generic Gate Replay Worker",
            _wait_event_manifest,
            "recover waiting event gate",
            "ticket.approved",
        ),
        (
            "Wait Until Retry Generic Gate Replay Worker",
            _wait_until_manifest,
            "recover waiting schedule gate",
            "resume on schedule",
        ),
    ],
)
def test_worker_retry_from_wait_gate_checkpoint_corrupted_to_generic_replay_fails_closed(
    client,
    workflow_name: str,
    manifest_factory,
    input_text: str,
    corrupted_output: str,
) -> None:
    _enable_runtime_approvals(client)
    workflow_id = workflow_name.lower().replace(" ", "-")
    manifest = manifest_factory(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name=workflow_name,
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": input_text},
    )
    assert created.status_code == 202
    original_run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        original = session.get(CaliberWorkflowRun, original_run_id)
        assert original is not None
        checkpoint_id = dict(original.summary or {}).get("resume_checkpoint_id")
        assert isinstance(checkpoint_id, str) and checkpoint_id
        ancestor_checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert ancestor_checkpoint is not None
        assert isinstance(ancestor_checkpoint.state_blob, dict)
        ancestor_checkpoint.state_blob = {
            "kind": "generic_checkpoint",
            "node_id": "wait_gate",
            "output": corrupted_output,
            "output_by_port": {"output": corrupted_output},
        }
        original.status = "failed"
        original.completed_at = datetime.now(timezone.utc)
        original.error_code = "simulated_failure"
        original.error_summary = "operator requested checkpoint recovery"
        session.commit()

    retried = client.post(
        f"{PREFIX}/workflow-runs/{original_run_id}/retry",
        json={
            "checkpoint_id": checkpoint_id,
            "reason": "recover from corrupted wait gate checkpoint",
        },
    )
    assert retried.status_code == 202, retried.text
    retried_run_id = retried.json()["data"]["workflow_run_id"]

    worker._tick()
    _assert_fail_closed_retry_state(
        client,
        retried_run_id=retried_run_id,
        checkpoint_id=checkpoint_id,
        source_run_id=original_run_id,
    )


def _assert_fail_closed_retry_state(
    client,
    *,
    retried_run_id: str,
    checkpoint_id: str,
    source_run_id: str,
) -> None:
    with client.app.state.session_factory() as session:
        retried_run = session.get(CaliberWorkflowRun, retried_run_id)
        assert retried_run is not None
        assert retried_run.status == "failed"
        assert retried_run.error_code == "resume_checkpoint_unavailable"
        assert retried_run.error_summary is not None
        assert checkpoint_id in retried_run.error_summary
        assert source_run_id in retried_run.error_summary
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == retried_run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events
        assert not any(event.event_type == "workflow.run.started" for event in events)
        assert events[-1].event_type == "workflow.run.failed"
        assert checkpoint_id in str(events[-1].payload.get("error", ""))
        approvals = (
            session.query(CaliberRuntimeApprovalRequest)
            .filter(CaliberRuntimeApprovalRequest.workflow_run_id == retried_run_id)
            .all()
        )
        assert approvals == []


def test_worker_retry_from_missing_ancestor_checkpoint_fails_closed(client) -> None:
    _enable_runtime_approvals(client)
    workflow_id = "approval-retry-missing-checkpoint-worker-wf"
    manifest = _approval_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Approval Retry Missing Checkpoint Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "recover from missing checkpoint"},
    )
    assert created.status_code == 202
    original_run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        original = session.get(CaliberWorkflowRun, original_run_id)
        assert original is not None
        checkpoint_id = dict(original.summary or {}).get("resume_checkpoint_id")
        assert isinstance(checkpoint_id, str) and checkpoint_id
        original.status = "failed"
        original.completed_at = datetime.now(timezone.utc)
        original.error_code = "simulated_failure"
        original.error_summary = "operator requested checkpoint recovery"
        session.commit()

    retried = client.post(
        f"{PREFIX}/workflow-runs/{original_run_id}/retry",
        json={"checkpoint_id": checkpoint_id, "reason": "recover from stored checkpoint"},
    )
    assert retried.status_code == 202, retried.text
    retried_run_id = retried.json()["data"]["workflow_run_id"]

    with client.app.state.session_factory() as session:
        ancestor_checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert ancestor_checkpoint is not None
        session.delete(ancestor_checkpoint)
        session.commit()

    worker._tick()
    _assert_fail_closed_retry_state(
        client,
        retried_run_id=retried_run_id,
        checkpoint_id=checkpoint_id,
        source_run_id=original_run_id,
    )


def test_worker_retry_from_corrupt_ancestor_checkpoint_fails_closed(client) -> None:
    _enable_runtime_approvals(client)
    workflow_id = "approval-retry-corrupt-checkpoint-worker-wf"
    manifest = _approval_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Approval Retry Corrupt Checkpoint Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "recover from corrupt checkpoint"},
    )
    assert created.status_code == 202
    original_run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        original = session.get(CaliberWorkflowRun, original_run_id)
        assert original is not None
        checkpoint_id = dict(original.summary or {}).get("resume_checkpoint_id")
        assert isinstance(checkpoint_id, str) and checkpoint_id
        original.status = "failed"
        original.completed_at = datetime.now(timezone.utc)
        original.error_code = "simulated_failure"
        original.error_summary = "operator requested checkpoint recovery"
        session.commit()

    retried = client.post(
        f"{PREFIX}/workflow-runs/{original_run_id}/retry",
        json={"checkpoint_id": checkpoint_id, "reason": "recover from stored checkpoint"},
    )
    assert retried.status_code == 202, retried.text
    retried_run_id = retried.json()["data"]["workflow_run_id"]

    with client.app.state.session_factory() as session:
        ancestor_checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert ancestor_checkpoint is not None
        ancestor_checkpoint.state_blob = {
            "kind": "runtime_approval",
            "output": "approval gate",
            "input_by_port": {"input": "recover from corrupt checkpoint"},
        }
        session.commit()

    worker._tick()
    _assert_fail_closed_retry_state(
        client,
        retried_run_id=retried_run_id,
        checkpoint_id=checkpoint_id,
        source_run_id=original_run_id,
    )


def test_worker_retry_from_malformed_payload_ancestor_checkpoint_fails_closed(client) -> None:
    _enable_runtime_approvals(client)
    workflow_id = "approval-retry-malformed-payload-checkpoint-worker-wf"
    manifest = _approval_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Approval Retry Malformed Payload Checkpoint Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "recover from malformed checkpoint payload"},
    )
    assert created.status_code == 202
    original_run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        original = session.get(CaliberWorkflowRun, original_run_id)
        assert original is not None
        checkpoint_id = dict(original.summary or {}).get("resume_checkpoint_id")
        assert isinstance(checkpoint_id, str) and checkpoint_id
        original.status = "failed"
        original.completed_at = datetime.now(timezone.utc)
        original.error_code = "simulated_failure"
        original.error_summary = "operator requested checkpoint recovery"
        session.commit()

    retried = client.post(
        f"{PREFIX}/workflow-runs/{original_run_id}/retry",
        json={
            "checkpoint_id": checkpoint_id,
            "reason": "recover from malformed checkpoint payload",
        },
    )
    assert retried.status_code == 202, retried.text
    retried_run_id = retried.json()["data"]["workflow_run_id"]

    with client.app.state.session_factory() as session:
        ancestor_checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert ancestor_checkpoint is not None
        ancestor_checkpoint.state_blob = ["malformed-checkpoint-payload"]
        session.commit()

    worker._tick()
    _assert_fail_closed_retry_state(
        client,
        retried_run_id=retried_run_id,
        checkpoint_id=checkpoint_id,
        source_run_id=original_run_id,
    )


def test_worker_retry_from_out_of_lineage_checkpoint_fails_closed(client) -> None:
    _enable_runtime_approvals(client)
    workflow_id = "approval-retry-foreign-checkpoint-worker-wf"
    manifest = _approval_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Approval Retry Foreign Checkpoint Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "recover from foreign checkpoint"},
    )
    assert created.status_code == 202
    original_run_id = created.json()["data"]["workflow_run_id"]

    foreign_created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "foreign checkpoint source"},
    )
    assert foreign_created.status_code == 202
    foreign_run_id = foreign_created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()
    worker._tick()

    with client.app.state.session_factory() as session:
        original = session.get(CaliberWorkflowRun, original_run_id)
        foreign_run = session.get(CaliberWorkflowRun, foreign_run_id)
        assert original is not None
        assert foreign_run is not None
        original_checkpoint_id = dict(original.summary or {}).get("resume_checkpoint_id")
        foreign_checkpoint_id = dict(foreign_run.summary or {}).get("resume_checkpoint_id")
        assert isinstance(original_checkpoint_id, str) and original_checkpoint_id
        assert isinstance(foreign_checkpoint_id, str) and foreign_checkpoint_id
        original.status = "failed"
        original.completed_at = datetime.now(timezone.utc)
        original.error_code = "simulated_failure"
        original.error_summary = "operator requested checkpoint recovery"
        session.commit()

    retried = client.post(
        f"{PREFIX}/workflow-runs/{original_run_id}/retry",
        json={"checkpoint_id": original_checkpoint_id, "reason": "recover from stored checkpoint"},
    )
    assert retried.status_code == 202, retried.text
    retried_run_id = retried.json()["data"]["workflow_run_id"]

    with client.app.state.session_factory() as session:
        retried_run = session.get(CaliberWorkflowRun, retried_run_id)
        assert retried_run is not None
        retried_run.summary = {
            **dict(retried_run.summary or {}),
            "resume_checkpoint_id": foreign_checkpoint_id,
            "resume_checkpoint_run_id": foreign_run_id,
        }
        session.commit()

    worker._tick()
    _assert_fail_closed_retry_state(
        client,
        retried_run_id=retried_run_id,
        checkpoint_id=foreign_checkpoint_id,
        source_run_id=foreign_run_id,
    )


def test_worker_retry_from_checkpoint_with_missing_current_plan_node_fails_closed(client) -> None:
    _enable_runtime_approvals(client)
    workflow_id = "approval-retry-missing-current-plan-node-worker-wf"
    manifest = _approval_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Approval Retry Missing Current Plan Node Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "recover from renamed approval gate"},
    )
    assert created.status_code == 202
    original_run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        original = session.get(CaliberWorkflowRun, original_run_id)
        assert original is not None
        checkpoint_id = dict(original.summary or {}).get("resume_checkpoint_id")
        assert isinstance(checkpoint_id, str) and checkpoint_id
        original.status = "failed"
        original.completed_at = datetime.now(timezone.utc)
        original.error_code = "simulated_failure"
        original.error_summary = "operator requested checkpoint recovery"
        session.commit()

    retried = client.post(
        f"{PREFIX}/workflow-runs/{original_run_id}/retry",
        json={"checkpoint_id": checkpoint_id, "reason": "recover from renamed approval gate"},
    )
    assert retried.status_code == 202, retried.text
    retried_run_id = retried.json()["data"]["workflow_run_id"]

    with client.app.state.session_factory() as session:
        retried_run = session.get(CaliberWorkflowRun, retried_run_id)
        assert retried_run is not None
        manifest_snapshot = dict(retried_run.manifest_snapshot or {})
        nodes = dict(manifest_snapshot.get("nodes") or {})
        human_gate = dict(nodes.pop("human_gate"))
        human_gate["id"] = "human_gate_v2"
        nodes["human_gate_v2"] = human_gate
        manifest_snapshot["nodes"] = nodes
        manifest_snapshot["edges"] = [
            {
                **dict(edge),
                "to": "human_gate_v2" if edge.get("to") == "human_gate" else edge.get("to"),
                "from": "human_gate_v2" if edge.get("from") == "human_gate" else edge.get("from"),
            }
            for edge in list(manifest_snapshot.get("edges") or [])
        ]
        retried_run.manifest_snapshot = manifest_snapshot
        session.commit()

    worker._tick()
    _assert_fail_closed_retry_state(
        client,
        retried_run_id=retried_run_id,
        checkpoint_id=checkpoint_id,
        source_run_id=original_run_id,
    )

    with client.app.state.session_factory() as session:
        retried_run = session.get(CaliberWorkflowRun, retried_run_id)
        assert retried_run is not None
        assert retried_run.error_summary is not None
        assert (
            "references missing node 'human_gate' in the current workflow plan"
            in retried_run.error_summary
        )


def test_worker_retry_from_checkpoint_with_compile_invalid_manifest_snapshot_fails_closed(
    client,
) -> None:
    _enable_runtime_approvals(client)
    workflow_id = "approval-retry-compile-invalid-manifest-worker-wf"
    manifest = _approval_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Approval Retry Compile Invalid Manifest Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "recover from compile-invalid snapshot"},
    )
    assert created.status_code == 202
    original_run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        original = session.get(CaliberWorkflowRun, original_run_id)
        assert original is not None
        checkpoint_id = dict(original.summary or {}).get("resume_checkpoint_id")
        assert isinstance(checkpoint_id, str) and checkpoint_id
        original.status = "failed"
        original.completed_at = datetime.now(timezone.utc)
        original.error_code = "simulated_failure"
        original.error_summary = "operator requested checkpoint recovery"
        session.commit()

    retried = client.post(
        f"{PREFIX}/workflow-runs/{original_run_id}/retry",
        json={"checkpoint_id": checkpoint_id, "reason": "recover from compile-invalid snapshot"},
    )
    assert retried.status_code == 202, retried.text
    retried_run_id = retried.json()["data"]["workflow_run_id"]

    with client.app.state.session_factory() as session:
        retried_run = session.get(CaliberWorkflowRun, retried_run_id)
        assert retried_run is not None
        manifest_snapshot = dict(retried_run.manifest_snapshot or {})
        nodes = dict(manifest_snapshot.get("nodes") or {})
        nodes["human_gate"] = {
            "id": "human_gate",
            "type": "router",
            "inputs": {"input": {"type": "string"}},
            "outputs": {"route": {"type": "string"}},
            "branches": [],
        }
        manifest_snapshot["nodes"] = nodes
        retried_run.manifest_snapshot = manifest_snapshot
        session.commit()

    worker._tick()

    with client.app.state.session_factory() as session:
        retried_run = session.get(CaliberWorkflowRun, retried_run_id)
        assert retried_run is not None
        assert retried_run.status == "failed"
        assert retried_run.error_code == "runtime_error"
        assert retried_run.error_summary is not None
        assert "manifest failed validation; cannot compile" in retried_run.error_summary
        assert "Add at least one branch before this router can run." in retried_run.error_summary
        summary = dict(retried_run.summary or {})
        assert summary.get("status") == "failed"
        assert "Add at least one branch before this router can run." in str(
            summary.get("error", "")
        )
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == retried_run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events
        assert not any(event.event_type == "workflow.run.started" for event in events)
        assert events[-1].event_type == "workflow.run.failed"
        assert "Add at least one branch before this router can run." in str(
            events[-1].payload.get("error", "")
        )
        approvals = (
            session.query(CaliberRuntimeApprovalRequest)
            .filter(CaliberRuntimeApprovalRequest.workflow_run_id == retried_run_id)
            .all()
        )
        assert approvals == []


def test_worker_retry_from_checkpoint_with_compile_invalid_knowledge_build_manifest_snapshot_fails_closed(
    client,
) -> None:
    _enable_runtime_approvals(client)
    workflow_id = "approval-retry-compile-invalid-knowledge-build-manifest-worker-wf"
    manifest = _approval_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Approval Retry Compile Invalid Knowledge Build Manifest Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={
            "workflow_version_id": vid,
            "input": "recover from compile-invalid knowledge-build snapshot",
        },
    )
    assert created.status_code == 202
    original_run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        original = session.get(CaliberWorkflowRun, original_run_id)
        assert original is not None
        checkpoint_id = dict(original.summary or {}).get("resume_checkpoint_id")
        assert isinstance(checkpoint_id, str) and checkpoint_id
        original.status = "failed"
        original.completed_at = datetime.now(timezone.utc)
        original.error_code = "simulated_failure"
        original.error_summary = "operator requested checkpoint recovery"
        session.commit()

    retried = client.post(
        f"{PREFIX}/workflow-runs/{original_run_id}/retry",
        json={
            "checkpoint_id": checkpoint_id,
            "reason": "recover from compile-invalid knowledge-build snapshot",
        },
    )
    assert retried.status_code == 202, retried.text
    retried_run_id = retried.json()["data"]["workflow_run_id"]

    with client.app.state.session_factory() as session:
        retried_run = session.get(CaliberWorkflowRun, retried_run_id)
        assert retried_run is not None
        manifest_snapshot = dict(retried_run.manifest_snapshot or {})
        nodes = dict(manifest_snapshot.get("nodes") or {})
        nodes["human_gate"] = {
            "id": "human_gate",
            "type": "knowledge_build",
            "knowledge_base_id": "",
            "chunking_strategy": "",
            "embedding_model": "",
            "inputs": {"request": {"type": "string"}},
            "outputs": {"request": {"type": "string"}},
        }
        manifest_snapshot["nodes"] = nodes
        retried_run.manifest_snapshot = manifest_snapshot
        session.commit()

    worker._tick()

    with client.app.state.session_factory() as session:
        retried_run = session.get(CaliberWorkflowRun, retried_run_id)
        assert retried_run is not None
        assert retried_run.status == "failed"
        assert retried_run.error_code == "runtime_error"
        assert retried_run.error_summary is not None
        assert "manifest failed validation; cannot compile" in retried_run.error_summary
        for message in (
            "Select a knowledge base to refresh.",
            "Choose a chunking strategy or map one into the node.",
            "Choose an embedding model or map one into the node.",
        ):
            assert message in retried_run.error_summary
        summary = dict(retried_run.summary or {})
        assert summary.get("status") == "failed"
        for message in (
            "Select a knowledge base to refresh.",
            "Choose a chunking strategy or map one into the node.",
            "Choose an embedding model or map one into the node.",
        ):
            assert message in str(summary.get("error", ""))
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == retried_run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events
        assert not any(event.event_type == "workflow.run.started" for event in events)
        assert events[-1].event_type == "workflow.run.failed"
        for message in (
            "Select a knowledge base to refresh.",
            "Choose a chunking strategy or map one into the node.",
            "Choose an embedding model or map one into the node.",
        ):
            assert message in str(events[-1].payload.get("error", ""))
        approvals = (
            session.query(CaliberRuntimeApprovalRequest)
            .filter(CaliberRuntimeApprovalRequest.workflow_run_id == retried_run_id)
            .all()
        )
        assert approvals == []


def test_worker_retry_from_checkpoint_with_parse_invalid_manifest_snapshot_fails_closed(  # noqa: PLR0915
    client,
) -> None:
    _enable_runtime_approvals(client)
    workflow_id = "approval-retry-parse-invalid-manifest-worker-wf"
    manifest = _approval_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Approval Retry Parse Invalid Manifest Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "recover from parse-invalid snapshot"},
    )
    assert created.status_code == 202
    original_run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        original = session.get(CaliberWorkflowRun, original_run_id)
        assert original is not None
        checkpoint_id = dict(original.summary or {}).get("resume_checkpoint_id")
        assert isinstance(checkpoint_id, str) and checkpoint_id
        original.status = "failed"
        original.completed_at = datetime.now(timezone.utc)
        original.error_code = "simulated_failure"
        original.error_summary = "operator requested checkpoint recovery"
        session.commit()

    retried = client.post(
        f"{PREFIX}/workflow-runs/{original_run_id}/retry",
        json={"checkpoint_id": checkpoint_id, "reason": "recover from parse-invalid snapshot"},
    )
    assert retried.status_code == 202, retried.text
    retried_run_id = retried.json()["data"]["workflow_run_id"]

    with client.app.state.session_factory() as session:
        retried_run = session.get(CaliberWorkflowRun, retried_run_id)
        assert retried_run is not None
        manifest_snapshot = dict(retried_run.manifest_snapshot or {})
        nodes = dict(manifest_snapshot.get("nodes") or {})
        human_gate = dict(nodes.get("human_gate") or {})
        human_gate["approval_count"] = 0
        nodes["human_gate"] = human_gate
        manifest_snapshot["nodes"] = nodes
        retried_run.manifest_snapshot = manifest_snapshot
        session.commit()

    worker._tick()

    with client.app.state.session_factory() as session:
        retried_run = session.get(CaliberWorkflowRun, retried_run_id)
        assert retried_run is not None
        assert retried_run.status == "failed"
        assert retried_run.error_code == "runtime_error"
        assert retried_run.error_summary is not None
        assert "manifest is invalid and cannot be parsed" in retried_run.error_summary
        assert "approval_count" in retried_run.error_summary
        assert "greater than or equal to 1" in retried_run.error_summary
        summary = dict(retried_run.summary or {})
        assert summary.get("status") == "failed"
        assert "approval_count" in str(summary.get("error", ""))
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == retried_run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events
        assert not any(event.event_type == "workflow.run.started" for event in events)
        assert events[-1].event_type == "workflow.run.failed"
        assert "approval_count" in str(events[-1].payload.get("error", ""))
        approvals = (
            session.query(CaliberRuntimeApprovalRequest)
            .filter(CaliberRuntimeApprovalRequest.workflow_run_id == retried_run_id)
            .all()
        )
        assert approvals == []


def test_worker_retry_from_checkpoint_with_kind_drift_in_current_plan_fails_closed(client) -> None:
    _enable_runtime_approvals(client)
    workflow_id = "approval-retry-kind-drift-worker-wf"
    manifest = _approval_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Approval Retry Kind Drift Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "recover from checkpoint kind drift"},
    )
    assert created.status_code == 202
    original_run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        original = session.get(CaliberWorkflowRun, original_run_id)
        assert original is not None
        checkpoint_id = dict(original.summary or {}).get("resume_checkpoint_id")
        assert isinstance(checkpoint_id, str) and checkpoint_id
        original.status = "failed"
        original.completed_at = datetime.now(timezone.utc)
        original.error_code = "simulated_failure"
        original.error_summary = "operator requested checkpoint recovery"
        session.commit()

    retried = client.post(
        f"{PREFIX}/workflow-runs/{original_run_id}/retry",
        json={"checkpoint_id": checkpoint_id, "reason": "recover from checkpoint kind drift"},
    )
    assert retried.status_code == 202, retried.text
    retried_run_id = retried.json()["data"]["workflow_run_id"]

    with client.app.state.session_factory() as session:
        retried_run = session.get(CaliberWorkflowRun, retried_run_id)
        assert retried_run is not None
        manifest_snapshot = dict(retried_run.manifest_snapshot or {})
        nodes = dict(manifest_snapshot.get("nodes") or {})
        nodes["human_gate"] = {
            "id": "human_gate",
            "type": "python_code",
            "code": 'return {"output": payload["input"]}',
            "inputs": {"input": {"type": "string"}},
            "outputs": {"output": {"type": "string"}},
        }
        manifest_snapshot["nodes"] = nodes
        manifest_snapshot["edges"] = [
            {
                **dict(edge),
                "map": {"final_output": "input"}
                if edge.get("id") == "e2"
                else {"output": "response"}
                if edge.get("id") == "e3"
                else dict(edge.get("map") or {}),
            }
            for edge in list(manifest_snapshot.get("edges") or [])
        ]
        retried_run.manifest_snapshot = manifest_snapshot
        session.commit()

    worker._tick()
    _assert_fail_closed_retry_state(
        client,
        retried_run_id=retried_run_id,
        checkpoint_id=checkpoint_id,
        source_run_id=original_run_id,
    )

    with client.app.state.session_factory() as session:
        retried_run = session.get(CaliberWorkflowRun, retried_run_id)
        assert retried_run is not None
        assert retried_run.error_summary is not None
        assert (
            "kind 'human_approval' does not match current node 'human_gate' type 'python_code'"
            in retried_run.error_summary
        )


def test_worker_retry_from_wait_checkpoint_with_kind_drift_in_current_plan_fails_closed(
    client,
) -> None:
    _enable_runtime_approvals(client)
    workflow_id = "wait-until-retry-kind-drift-worker-wf"
    manifest = _wait_until_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Wait Until Retry Kind Drift Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "recover from wait checkpoint kind drift"},
    )
    assert created.status_code == 202
    original_run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        original = session.get(CaliberWorkflowRun, original_run_id)
        assert original is not None
        checkpoint_id = dict(original.summary or {}).get("resume_checkpoint_id")
        assert isinstance(checkpoint_id, str) and checkpoint_id
        original.status = "failed"
        original.completed_at = datetime.now(timezone.utc)
        original.error_code = "simulated_failure"
        original.error_summary = "operator requested checkpoint recovery"
        session.commit()

    retried = client.post(
        f"{PREFIX}/workflow-runs/{original_run_id}/retry",
        json={"checkpoint_id": checkpoint_id, "reason": "recover from wait checkpoint kind drift"},
    )
    assert retried.status_code == 202, retried.text
    retried_run_id = retried.json()["data"]["workflow_run_id"]

    with client.app.state.session_factory() as session:
        retried_run = session.get(CaliberWorkflowRun, retried_run_id)
        assert retried_run is not None
        manifest_snapshot = dict(retried_run.manifest_snapshot or {})
        nodes = dict(manifest_snapshot.get("nodes") or {})
        nodes["wait_gate"] = {
            "id": "wait_gate",
            "type": "python_code",
            "code": 'return {"output": payload["input"]}',
            "inputs": {"input": {"type": "string"}},
            "outputs": {"output": {"type": "string"}},
        }
        manifest_snapshot["nodes"] = nodes
        retried_run.manifest_snapshot = manifest_snapshot
        session.commit()

    worker._tick()
    _assert_fail_closed_retry_state(
        client,
        retried_run_id=retried_run_id,
        checkpoint_id=checkpoint_id,
        source_run_id=original_run_id,
    )

    with client.app.state.session_factory() as session:
        retried_run = session.get(CaliberWorkflowRun, retried_run_id)
        assert retried_run is not None
        assert retried_run.error_summary is not None
        assert (
            "kind 'wait_until' does not match current node 'wait_gate' type 'python_code'"
            in retried_run.error_summary
        )


def test_worker_retry_from_wait_for_event_checkpoint_with_mismatched_event_name_fails_closed(
    client,
) -> None:
    _enable_runtime_approvals(client)
    workflow_id = "wait-event-retry-mismatched-event-name-worker-wf"
    manifest = _wait_event_payload_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Wait Event Retry Mismatched Event Name Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "recover from mismatched event replay"},
    )
    assert created.status_code == 202
    original_run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        original = session.get(CaliberWorkflowRun, original_run_id)
        assert original is not None
        checkpoint_id = dict(original.summary or {}).get("resume_checkpoint_id")
        assert isinstance(checkpoint_id, str) and checkpoint_id
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None
        assert isinstance(checkpoint.state_blob, dict)
        checkpoint.state_blob = {
            **checkpoint.state_blob,
            "expected_event_name": "ticket.approved",
            "resume_event_inputs": {
                "event_name": "ticket.denied",
                "event_payload": {"ticket_id": "T-42", "approved": True},
            },
        }
        original.status = "failed"
        original.completed_at = datetime.now(timezone.utc)
        original.error_code = "simulated_failure"
        original.error_summary = "operator requested checkpoint recovery"
        session.commit()

    retried = client.post(
        f"{PREFIX}/workflow-runs/{original_run_id}/retry",
        json={"checkpoint_id": checkpoint_id, "reason": "recover from mismatched event checkpoint"},
    )
    assert retried.status_code == 409, retried.text
    assert (
        retried.json()["detail"]
        == "workflow run retry checkpoint event 'ticket.denied' does not match expected event 'ticket.approved'"
    )


def test_worker_waits_for_event_and_resumes_from_checkpoint(client) -> None:
    _enable_runtime_approvals(client)
    workflow_id = "wait-event-worker-wf"
    manifest = _wait_event_manifest(workflow_id)
    _wid, vid = create_and_publish(client, workflow_name="Wait Event Worker", manifest=manifest)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "resume on event"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "waiting_event"
        assert run.error_code == "waiting_event"
        summary = dict(run.summary or {})
        checkpoint_id = summary.get("resume_checkpoint_id")
        assert isinstance(checkpoint_id, str) and checkpoint_id
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None
        assert checkpoint.node_id == "wait_gate"
        assert isinstance(checkpoint.state_blob, dict)
        assert checkpoint.state_blob.get("kind") == "wait_for_event"
        assert checkpoint.state_blob.get("expected_event_name") == "ticket.approved"
        assert checkpoint.state_blob.get("input_by_port") == {"input": "resume on event"}

    resumed = client.post(f"{PREFIX}/workflow-runs/{run_id}/resume")
    assert resumed.status_code == 202
    worker._tick()

    final = client.get(f"{PREFIX}/workflow-runs/{run_id}")
    assert final.status_code == 200
    assert final.json()["data"]["status"] == "completed"


def test_worker_fails_malformed_waiting_event_result_instead_of_persisting_bad_checkpoint(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime_approvals(client)
    workflow_id = "wait-event-worker-malformed-wf"
    manifest = _wait_event_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Wait Event Worker Malformed",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "resume on event"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    def _fake_execute(*args, **kwargs):
        return WorkflowRunResult(
            status="blocked",
            output="",
            error="waiting_event:wait_gate",
            steps=[
                NodeStep(
                    "wait_gate",
                    "wait_for_event",
                    "blocked",
                    output="resume on event",
                    detail="waiting_event:wait_gate",
                )
            ],
        )

    monkeypatch.setattr("caliber.orchestrator.workflow_run_worker.execute", _fake_execute)

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.current_node_id is None
        assert run.error_code == "runtime_error"
        assert run.error_summary == (
            "workflow runtime returned waiting_event without resumable checkpoint context"
        )
        assert not dict(run.summary or {}).get("resume_checkpoint_id")
        assert (
            session.query(CaliberWorkflowRunCheckpoint)
            .filter(CaliberWorkflowRunCheckpoint.workflow_run_id == run_id)
            .count()
            == 0
        )
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.failed"


def test_worker_fails_wait_for_event_result_missing_expected_event_name_instead_of_persisting_bad_checkpoint(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime_approvals(client)
    workflow_id = "wait-event-worker-missing-event-name-wf"
    manifest = _wait_event_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Wait Event Worker Missing Event Name",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "resume on event"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    def _fake_execute(*args, **kwargs):
        return WorkflowRunResult(
            status="blocked",
            output="",
            error="waiting_event:wait_gate",
            steps=[
                NodeStep(
                    "wait_gate",
                    "wait_for_event",
                    "blocked",
                    output="resume on event",
                    detail="waiting_event:wait_gate",
                    checkpoint_state={"input_by_port": {"input": "resume on event"}},
                )
            ],
        )

    monkeypatch.setattr("caliber.orchestrator.workflow_run_worker.execute", _fake_execute)

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.current_node_id is None
        assert run.error_code == "runtime_error"
        assert run.error_summary == (
            "workflow runtime returned waiting_event without resumable checkpoint context"
        )
        assert not dict(run.summary or {}).get("resume_checkpoint_id")
        assert (
            session.query(CaliberWorkflowRunCheckpoint)
            .filter(CaliberWorkflowRunCheckpoint.workflow_run_id == run_id)
            .count()
            == 0
        )
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.failed"


def test_worker_fails_wait_until_result_missing_resume_at_instead_of_persisting_bad_checkpoint(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime_approvals(client)
    workflow_id = "wait-until-worker-missing-resume-at-wf"
    manifest = _wait_until_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Wait Until Worker Missing Resume At",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "resume later"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    def _fake_execute(*args, **kwargs):
        return WorkflowRunResult(
            status="blocked",
            output="",
            error="waiting_event:wait_gate",
            steps=[
                NodeStep(
                    "wait_gate",
                    "wait_until",
                    "blocked",
                    output="resume later",
                    detail="waiting_event:wait_gate",
                    checkpoint_state={
                        "input_by_port": {"input": "resume later"},
                        "wait_until": "2026-06-16T09:00:00",
                        "timezone": "America/Los_Angeles",
                    },
                )
            ],
        )

    monkeypatch.setattr("caliber.orchestrator.workflow_run_worker.execute", _fake_execute)

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.current_node_id is None
        assert run.error_code == "runtime_error"
        assert run.error_summary == (
            "workflow runtime returned waiting_event without resumable checkpoint context"
        )
        assert not dict(run.summary or {}).get("resume_checkpoint_id")
        assert (
            session.query(CaliberWorkflowRunCheckpoint)
            .filter(CaliberWorkflowRunCheckpoint.workflow_run_id == run_id)
            .count()
            == 0
        )
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.failed"


def test_worker_wait_for_event_persists_correlation_and_timeout_metadata(client) -> None:
    _enable_runtime_approvals(client)
    workflow_id = "wait-event-metadata-worker-wf"
    manifest = _wait_event_manifest(
        workflow_id,
        correlation_key="ticket_id",
        timeout_seconds=300,
    )
    _wid, vid = create_and_publish(
        client, workflow_name="Wait Event Metadata Worker", manifest=manifest
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": '{"ticket_id":"T-42","approved":false}'},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        checkpoint_id = dict(run.summary or {}).get("resume_checkpoint_id")
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None
        assert isinstance(checkpoint.state_blob, dict)
        assert checkpoint.state_blob.get("correlation_key") == "ticket_id"
        assert checkpoint.state_blob.get("correlation_value") == "T-42"
        assert checkpoint.state_blob.get("timeout_seconds") == 300.0


def test_worker_completes_join_any_runs_when_parallel_sibling_waits(client) -> None:
    _enable_queue(client)
    workflow_id = "parallel-join-any-wait-worker-wf"
    manifest = _parallel_join_any_wait_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Parallel Join Any Wait Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.completed_at is not None
        assert run.summary is not None
        assert "hello" in str(run.summary["output"])
        summary_steps = run.summary.get("steps")
        assert isinstance(summary_steps, list)
        assert any(
            isinstance(step, dict)
            and step.get("node_id") == "wait_event"
            and step.get("status") == "blocked"
            and "waiting_event:wait_event" in str(step.get("detail", ""))
            for step in summary_steps
        )
        assert any(
            isinstance(step, dict)
            and step.get("node_id") == "join_any"
            and step.get("status") == "ok"
            for step in summary_steps
        )
        assert any(
            isinstance(step, dict) and step.get("node_id") == "final" and step.get("status") == "ok"
            for step in summary_steps
        )
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.completed"


def test_worker_fails_join_any_runs_when_a_parallel_sibling_errors(client) -> None:
    _enable_queue(client)
    workflow_id = "parallel-join-any-failure-worker-wf"
    manifest = _parallel_join_any_failure_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Parallel Join Any Failure Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.error_code == "runtime_error"
        assert run.error_summary is not None
        assert "python_code node 'bad_python' failed" in run.error_summary
        assert run.completed_at is not None
        assert run.summary is not None
        assert "python_code node 'bad_python' failed" in str(run.summary.get("error", ""))
        summary_steps = run.summary.get("steps")
        assert isinstance(summary_steps, list)
        assert [step.get("node_id") for step in summary_steps if isinstance(step, dict)] == [
            "start",
            "parallel",
        ]
        assert not any(
            isinstance(step, dict) and step.get("node_id") == "join_any" for step in summary_steps
        )
        assert not any(
            isinstance(step, dict) and step.get("node_id") == "final" for step in summary_steps
        )
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.failed"
        assert "python_code node 'bad_python' failed" in str(events[-1].payload.get("error", ""))


def test_worker_fails_join_all_runs_when_a_parallel_sibling_errors(client) -> None:
    _enable_queue(client)
    workflow_id = "parallel-join-all-failure-worker-wf"
    manifest = _parallel_join_all_failure_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Parallel Join All Failure Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.error_code == "runtime_error"
        assert run.error_summary is not None
        assert "python_code node 'bad_python' failed" in run.error_summary
        assert run.completed_at is not None
        assert run.summary is not None
        assert "python_code node 'bad_python' failed" in str(run.summary.get("error", ""))
        summary_steps = run.summary.get("steps")
        assert isinstance(summary_steps, list)
        assert [step.get("node_id") for step in summary_steps if isinstance(step, dict)] == [
            "start",
            "parallel",
        ]
        assert not any(
            isinstance(step, dict) and step.get("node_id") == "join_all" for step in summary_steps
        )
        assert not any(
            isinstance(step, dict) and step.get("node_id") == "final" for step in summary_steps
        )
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.failed"
        assert "python_code node 'bad_python' failed" in str(events[-1].payload.get("error", ""))


def test_worker_blocks_join_all_runs_when_parallel_sibling_waits(client) -> None:
    _enable_queue(client)
    workflow_id = "parallel-join-all-wait-worker-wf"
    manifest = _parallel_join_all_wait_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Parallel Join All Wait Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "waiting_event"
        assert run.error_code == "waiting_event"
        assert run.summary is not None
        summary = dict(run.summary or {})
        checkpoint_id = summary.get("resume_checkpoint_id")
        assert isinstance(checkpoint_id, str) and checkpoint_id
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None
        assert checkpoint.node_id == "wait_event"
        assert isinstance(checkpoint.state_blob, dict)
        assert checkpoint.state_blob.get("kind") == "wait_for_event"
        assert checkpoint.state_blob.get("expected_event_name") == "resume_event"
        assert checkpoint.state_blob.get("input_by_port") == {"input": "hello"}
        summary_steps = summary.get("steps")
        assert isinstance(summary_steps, list)
        assert any(
            isinstance(step, dict)
            and step.get("node_id") == "wait_event"
            and step.get("status") == "blocked"
            and "waiting_event:wait_event" in str(step.get("detail", ""))
            for step in summary_steps
        )
        assert not any(
            isinstance(step, dict) and step.get("node_id") == "join_all" for step in summary_steps
        )
        assert not any(
            isinstance(step, dict) and step.get("node_id") == "final" for step in summary_steps
        )
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.waiting_event"
        assert events[-1].payload.get("node_id") == "wait_event"


def test_worker_routes_to_matching_router_branch(client) -> None:
    _enable_queue(client)
    workflow_id = "router-branch-worker-wf"
    manifest = _router_branch_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Router Branch Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "refund request"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.completed_at is not None
        assert run.summary is not None
        assert "refund request" in str(run.summary["output"])
        summary_steps = run.summary.get("steps")
        assert isinstance(summary_steps, list)
        step_ids = {step.get("node_id") for step in summary_steps if isinstance(step, dict)}
        assert "router" in step_ids
        assert "agent" in step_ids
        assert "final" in step_ids
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.completed"


def test_worker_routes_to_router_fallback_branch_when_no_conditions_match(client) -> None:
    _enable_queue(client)
    workflow_id = "router-fallback-worker-wf"
    manifest = _router_branch_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Router Fallback Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "shipping status"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.completed_at is not None
        assert run.summary is not None
        assert run.summary["output"] == "shipping status"
        summary_steps = run.summary.get("steps")
        assert isinstance(summary_steps, list)
        step_ids = {step.get("node_id") for step in summary_steps if isinstance(step, dict)}
        assert "router" in step_ids
        assert "final" in step_ids
        assert "agent" not in step_ids
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.completed"


def test_worker_marks_router_without_branches_failures_as_runtime_errors(client) -> None:
    _enable_queue(client)
    workflow_id = "router-no-branches-worker-wf"
    manifest = _router_branch_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Router No Branches Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "refund request"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        manifest_snapshot = dict(run.manifest_snapshot or {})
        nodes = dict(manifest_snapshot.get("nodes") or {})
        router = dict(nodes.get("router") or {})
        router["branches"] = []
        nodes["router"] = router
        manifest_snapshot["nodes"] = nodes
        run.manifest_snapshot = manifest_snapshot
        session.commit()

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.error_code == "runtime_error"
        assert run.error_summary is not None
        assert "Add at least one branch before this router can run." in run.error_summary
        assert run.completed_at is not None
        assert run.summary is not None
        assert "Add at least one branch before this router can run." in str(
            run.summary.get("error", "")
        )
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.failed"
        assert "Add at least one branch before this router can run." in str(
            events[-1].payload.get("error", "")
        )


def test_worker_marks_knowledge_build_manifest_snapshot_validation_failures_as_runtime_errors(
    client,
) -> None:
    _enable_queue(client)
    workflow_id = "knowledge-build-missing-config-snapshot-worker-wf"
    manifest = _knowledge_build_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Knowledge Build Missing Config Snapshot Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "refresh the knowledge base"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        manifest_snapshot = dict(run.manifest_snapshot or {})
        nodes = dict(manifest_snapshot.get("nodes") or {})
        knowledge_build = dict(nodes.get("knowledge_build") or {})
        knowledge_build["knowledge_base_id"] = ""
        knowledge_build["chunking_strategy"] = ""
        knowledge_build["embedding_model"] = ""
        nodes["knowledge_build"] = knowledge_build
        manifest_snapshot["nodes"] = nodes
        run.manifest_snapshot = manifest_snapshot
        session.commit()

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.error_code == "runtime_error"
        assert run.error_summary is not None
        assert "manifest failed validation; cannot compile" in run.error_summary
        for message in (
            "Select a knowledge base to refresh.",
            "Choose a chunking strategy or map one into the node.",
            "Choose an embedding model or map one into the node.",
        ):
            assert message in run.error_summary
        assert run.completed_at is not None
        assert run.summary is not None
        for message in (
            "Select a knowledge base to refresh.",
            "Choose a chunking strategy or map one into the node.",
            "Choose an embedding model or map one into the node.",
        ):
            assert message in str(run.summary.get("error", ""))
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events
        assert not any(event.event_type == "workflow.run.started" for event in events)
        assert events[-1].event_type == "workflow.run.failed"
        for message in (
            "Select a knowledge base to refresh.",
            "Choose a chunking strategy or map one into the node.",
            "Choose an embedding model or map one into the node.",
        ):
            assert message in str(events[-1].payload.get("error", ""))


def test_worker_marks_parse_invalid_manifest_snapshot_failures_as_runtime_errors(
    client,
) -> None:
    _enable_queue(client)
    workflow_id = "manifest-snapshot-parse-invalid-worker-wf"
    manifest = _knowledge_build_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Manifest Snapshot Parse Invalid Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "refresh the knowledge base"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        manifest_snapshot = dict(run.manifest_snapshot or {})
        manifest_snapshot["schema_version"] = "invalid"
        run.manifest_snapshot = manifest_snapshot
        session.commit()

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.error_code == "runtime_error"
        assert run.error_summary is not None
        assert "manifest is invalid and cannot be parsed" in run.error_summary
        assert "schema_version must be an integer" in run.error_summary
        assert run.completed_at is not None
        assert run.summary is not None
        assert "schema_version must be an integer" in str(run.summary.get("error", ""))
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events
        assert not any(event.event_type == "workflow.run.started" for event in events)
        assert events[-1].event_type == "workflow.run.failed"
        assert "schema_version must be an integer" in str(events[-1].payload.get("error", ""))


def test_worker_marks_executor_startup_failures_as_runtime_errors(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client.app.state.config = client.app.state.config.model_copy(
        update={
            "workflow_run_queue_enabled": True,
            "llm_provider": "openai",
            "llm_api_key_env": "OPENAI_API_KEY",
        }
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "start me"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.current_node_id is None
        assert run.error_code == "runtime_error"
        assert run.error_summary is not None
        assert "workflow runtime startup failed" in run.error_summary
        assert "CALIBER_LLM_PROVIDER=openai requires a secret" in run.error_summary
        assert run.completed_at is not None
        assert run.summary is not None
        assert "CALIBER_LLM_PROVIDER=openai requires a secret" in str(run.summary.get("error", ""))
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events
        assert not any(event.event_type == "workflow.run.started" for event in events)
        assert events[-1].event_type == "workflow.run.failed"
        assert "CALIBER_LLM_PROVIDER=openai requires a secret" in str(
            events[-1].payload.get("error", "")
        )


def test_worker_marks_unexpected_execute_exceptions_as_runtime_errors(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_queue(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "boom"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    def _fake_execute(*args, **kwargs):
        raise RuntimeError("outer execute boom")

    monkeypatch.setattr("caliber.orchestrator.workflow_run_worker.execute", _fake_execute)

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.current_node_id is None
        assert run.error_code == "runtime_error"
        assert run.error_summary == (
            "workflow runtime raised unexpectedly: RuntimeError: outer execute boom"
        )
        assert run.completed_at is not None
        assert run.summary is not None
        assert run.summary.get("error") == run.error_summary
        assert (
            session.query(CaliberWorkflowRunCheckpoint)
            .filter(CaliberWorkflowRunCheckpoint.workflow_run_id == run_id)
            .count()
            == 0
        )
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events
        event_types = [event.event_type for event in events]
        assert event_types.count("workflow.run.started") == 1
        assert not any(event.event_type == "workflow.run.step" for event in events)
        assert events[-1].event_type == "workflow.run.failed"
        assert events[-1].payload.get("error") == run.error_summary


def test_worker_completes_run_when_event_bus_publish_raises(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_queue(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    published_types: list[str | None] = []

    class _FailingEventBus:
        def publish(self, payload: dict[str, object]) -> None:
            published_types.append(payload.get("type") if isinstance(payload, dict) else None)
            raise RuntimeError("event bus offline")

    client.app.state.event_bus = _FailingEventBus()
    captured: dict[str, object] = {}

    def _warning(message: str, *args: object, **kwargs: object) -> None:
        captured["message"] = message % args if args else message
        captured["kwargs"] = dict(kwargs)

    monkeypatch.setattr(workflow_run_worker_module.logger, "warning", _warning)

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.completed_at is not None
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.completed"

    assert "workflow.run.started" in published_types
    assert "workflow.run.completed" in published_types
    assert (
        captured["message"] == "failed to publish workflow-run event type='workflow.run.completed'"
    )
    assert captured["kwargs"] == {"exc_info": True}


def test_worker_marks_failed_run_when_event_bus_publish_raises(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_queue(client)
    workflow_id = "event-bus-failure-parse-invalid-worker-wf"
    manifest = _knowledge_build_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Event Bus Failure Parse Invalid Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "refresh the knowledge base"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        manifest_snapshot = dict(run.manifest_snapshot or {})
        manifest_snapshot["schema_version"] = "invalid"
        run.manifest_snapshot = manifest_snapshot
        session.commit()

    published_types: list[str | None] = []

    class _FailingEventBus:
        def publish(self, payload: dict[str, object]) -> None:
            published_types.append(payload.get("type") if isinstance(payload, dict) else None)
            raise RuntimeError("event bus offline")

    client.app.state.event_bus = _FailingEventBus()
    captured: dict[str, object] = {}

    def _warning(message: str, *args: object, **kwargs: object) -> None:
        captured["message"] = message % args if args else message
        captured["kwargs"] = dict(kwargs)

    monkeypatch.setattr(workflow_run_worker_module.logger, "warning", _warning)

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.error_code == "runtime_error"
        assert run.error_summary is not None
        assert "manifest is invalid and cannot be parsed" in run.error_summary
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.failed"

    assert published_types == ["workflow.run.failed"]
    assert captured["message"] == "failed to publish workflow-run event type='workflow.run.failed'"
    assert captured["kwargs"] == {"exc_info": True}


def test_worker_wait_until_persists_timed_checkpoint(client) -> None:
    _enable_runtime_approvals(client)
    workflow_id = "wait-until-worker-wf"
    manifest = _wait_until_manifest(
        workflow_id, wait_until="2099-01-01T00:00:00", timezone_name="UTC"
    )
    _wid, vid = create_and_publish(client, workflow_name="Wait Until Worker", manifest=manifest)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "resume on schedule"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "waiting_event"
        summary = dict(run.summary or {})
        checkpoint_id = summary.get("resume_checkpoint_id")
        assert isinstance(checkpoint_id, str) and checkpoint_id
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None
        assert checkpoint.node_id == "wait_gate"
        assert isinstance(checkpoint.state_blob, dict)
        assert checkpoint.state_blob.get("kind") == "wait_until"
        assert checkpoint.state_blob.get("input_by_port") == {"input": "resume on schedule"}
        assert checkpoint.state_blob.get("wait_until") == "2099-01-01T00:00:00"
        assert checkpoint.state_blob.get("timezone") == "UTC"
        assert str(checkpoint.state_blob.get("resume_at", "")).startswith("2099-01-01T00:00:00")


def test_worker_marks_wait_until_invalid_timestamp_failures_as_runtime_errors(client) -> None:
    _enable_queue(client)
    workflow_id = "wait-until-invalid-timestamp-worker-wf"
    manifest = _wait_until_manifest(workflow_id, wait_until="not-a-timestamp", timezone_name="UTC")
    _wid, vid = create_and_publish(
        client,
        workflow_name="Wait Until Invalid Timestamp Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "resume on schedule"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.error_code == "runtime_error"
        assert run.error_summary is not None
        assert "wait_until node 'wait_gate' has invalid wait_until" in run.error_summary
        assert run.completed_at is not None
        assert run.summary is not None
        assert "wait_until node 'wait_gate' has invalid wait_until" in str(
            run.summary.get("error", "")
        )
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.failed"
        assert "wait_until node 'wait_gate' has invalid wait_until" in str(
            events[-1].payload.get("error", "")
        )


def test_worker_auto_resumes_wait_until_runs_when_due(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime_approvals(client)
    workflow_id = "wait-until-auto-resume-wf"
    manifest = _wait_until_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client, workflow_name="Wait Until Auto Resume", manifest=manifest
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "resume automatically"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()
    original_queued_at: datetime | None = None

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "waiting_event"
        original_queued_at = run.queued_at
        checkpoint_id = dict(run.summary or {}).get("resume_checkpoint_id")
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None
        assert isinstance(checkpoint.state_blob, dict)
        checkpoint.state_blob = {
            **checkpoint.state_blob,
            "resume_at": (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(),
        }
        session.commit()

    monkeypatch.setattr(
        "caliber.workflows.runtime._wait_until_ready",
        lambda raw, *, timezone_name="UTC": True,
    )

    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.queued_at == original_queued_at
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        event_types = [event.event_type for event in events]
        assert "workflow.run.resumed" in event_types
        resumed_event = next(
            event for event in events if event.event_type == "workflow.run.resumed"
        )
        assert resumed_event.payload["auto"] is True
        assert resumed_event.payload["reason"] == "wait_until_due"


def test_worker_auto_resume_wait_until_fails_closed_when_checkpoint_loses_input_snapshot(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime_approvals(client)
    workflow_id = "wait-until-auto-resume-invalid-snapshot-worker-wf"
    manifest = _wait_until_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Wait Until Auto Resume Invalid Snapshot",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "resume automatically"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "waiting_event"
        checkpoint_id = dict(run.summary or {}).get("resume_checkpoint_id")
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None
        assert isinstance(checkpoint.state_blob, dict)
        checkpoint.state_blob = {
            **checkpoint.state_blob,
            "resume_at": (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(),
            "input_by_port": ["not", "a", "dict"],
        }
        session.commit()

    monkeypatch.setattr(
        "caliber.workflows.runtime._wait_until_ready",
        lambda raw, *, timezone_name="UTC": True,
    )

    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.error_code == "resume_checkpoint_unavailable"
        assert "missing its input snapshot" in (run.error_summary or "")
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        event_types = [event.event_type for event in events]
        assert event_types[-1] == "workflow.run.failed"
        assert "workflow.run.resumed" not in event_types


def test_worker_manual_resume_overrides_wait_until_deadline(client) -> None:
    _enable_runtime_approvals(client)
    workflow_id = "wait-until-manual-resume-wf"
    manifest = _wait_until_manifest(
        workflow_id, wait_until="2099-01-01T00:00:00Z", timezone_name="UTC"
    )
    _wid, vid = create_and_publish(
        client, workflow_name="Wait Until Manual Resume", manifest=manifest
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "resume on schedule"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    waiting = client.get(f"{PREFIX}/workflow-runs/{run_id}")
    assert waiting.status_code == 200
    assert waiting.json()["data"]["status"] == "waiting_event"

    resumed = client.post(f"{PREFIX}/workflow-runs/{run_id}/resume")
    assert resumed.status_code == 202

    worker._tick()

    final = client.get(f"{PREFIX}/workflow-runs/{run_id}")
    assert final.status_code == 200
    data = final.json()["data"]
    assert data["status"] == "completed"
    assert data["summary"]["output"] == "resume on schedule"

    with client.app.state.session_factory() as session:
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .filter(CaliberWorkflowRunEvent.event_type == "workflow.run.step")
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        wait_gate_steps = [event for event in events if event.node_id == "wait_gate"]
        final_steps = [event for event in events if event.node_id == "final"]
        assert len(wait_gate_steps) == 2
        assert dict(wait_gate_steps[0].payload or {}).get("step", {}).get("status") == "blocked"
        assert dict(wait_gate_steps[1].payload or {}).get("step", {}).get("status") == "ok"
        assert len(final_steps) == 1


def test_worker_expires_wait_for_event_runs_when_timeout_passes(client) -> None:
    _enable_runtime_approvals(client)
    workflow_id = "wait-event-timeout-worker-wf"
    manifest = _wait_event_manifest(workflow_id, timeout_seconds=30)
    _wid, vid = create_and_publish(
        client, workflow_name="Wait Event Timeout Worker", manifest=manifest
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "resume on event"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        checkpoint_id = dict(run.summary or {}).get("resume_checkpoint_id")
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None
        checkpoint.created_at = datetime.now(timezone.utc) - timedelta(seconds=45)
        session.commit()

    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "expired"
        assert run.error_code == "wait_for_event_timeout"
        assert "ticket.approved" in (run.error_summary or "")
        assert "30s" in (run.error_summary or "")
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        expired_event = next(
            event for event in events if event.event_type == "workflow.run.expired"
        )
        assert expired_event.payload["reason"] == "wait_for_event_timeout"
        assert expired_event.payload["timeout_seconds"] == 30.0


def test_worker_replays_wait_for_event_with_resume_payload(client) -> None:
    _enable_runtime_approvals(client)
    workflow_id = "wait-event-payload-worker-wf"
    manifest = _wait_event_payload_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client, workflow_name="Wait Event Payload Worker", manifest=manifest
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "resume on event"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    resumed = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/resume",
        json={
            "event_name": "ticket.approved",
            "event_payload": {"ticket_id": "T-42", "approved": True},
        },
    )
    assert resumed.status_code == 202
    worker._tick()

    final = client.get(f"{PREFIX}/workflow-runs/{run_id}")
    assert final.status_code == 200
    data = final.json()["data"]
    assert data["status"] == "completed"
    assert data["summary"]["output"] == "ticket.approved::T-42::True"

    with client.app.state.session_factory() as session:
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .filter(CaliberWorkflowRunEvent.event_type == "workflow.run.step")
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        wait_gate_steps = [event for event in events if event.node_id == "wait_gate"]
        render_steps = [event for event in events if event.node_id == "render_event"]
        final_steps = [event for event in events if event.node_id == "final"]
        assert len(wait_gate_steps) == 2
        assert dict(wait_gate_steps[0].payload or {}).get("step", {}).get("status") == "blocked"
        assert dict(wait_gate_steps[1].payload or {}).get("step", {}).get("status") == "ok"
        assert len(render_steps) == 1
        assert len(final_steps) == 1


def test_worker_resume_by_event_prioritizes_resumed_run_over_newer_queued_sibling(client) -> None:
    _enable_runtime_approvals(client)
    workflow_id = "wait-event-resume-priority-worker-wf"
    manifest = _wait_event_payload_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Wait Event Resume Priority Worker",
        manifest=manifest,
    )
    first_created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "resume first"},
    )
    assert first_created.status_code == 202
    first_run_id = first_created.json()["data"]["workflow_run_id"]
    first_original_queued_at = first_created.json()["data"]["queued_at"]

    worker = _build_worker(client)
    worker._tick()

    first_waiting = client.get(f"{PREFIX}/workflow-runs/{first_run_id}")
    assert first_waiting.status_code == 200
    assert first_waiting.json()["data"]["status"] == "waiting_event"

    with client.app.state.session_factory() as session:
        first_run = session.get(CaliberWorkflowRun, first_run_id)
        assert first_run is not None
        first_original_db_queued_at = _as_utc(first_run.queued_at)

    second_created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "resume second"},
    )
    assert second_created.status_code == 202
    second_run_id = second_created.json()["data"]["workflow_run_id"]

    resumed = client.post(
        f"{PREFIX}/workflow-runs/resume-by-event",
        json={
            "event_name": "ticket.approved",
            "event_payload": {"ticket_id": "T-42", "approved": True},
        },
    )
    assert resumed.status_code == 202
    resumed_data = resumed.json()["data"]
    assert resumed_data["workflow_run_id"] == first_run_id
    assert resumed_data["queued_at"] == first_original_queued_at.removesuffix("Z")

    worker._tick()

    first_final = client.get(f"{PREFIX}/workflow-runs/{first_run_id}")
    assert first_final.status_code == 200
    first_final_data = first_final.json()["data"]
    assert first_final_data["status"] == "completed"
    assert first_final_data["summary"]["output"] == "ticket.approved::T-42::True"

    second_queued = client.get(f"{PREFIX}/workflow-runs/{second_run_id}")
    assert second_queued.status_code == 200
    assert second_queued.json()["data"]["status"] == "queued"

    with client.app.state.session_factory() as session:
        first_run = session.get(CaliberWorkflowRun, first_run_id)
        second_run = session.get(CaliberWorkflowRun, second_run_id)
        assert first_run is not None
        assert second_run is not None
        assert _as_utc(first_run.queued_at) == first_original_db_queued_at
        assert second_run.status == "queued"

    worker._tick()

    second_waiting = client.get(f"{PREFIX}/workflow-runs/{second_run_id}")
    assert second_waiting.status_code == 200
    assert second_waiting.json()["data"]["status"] == "waiting_event"


def test_worker_executes_event_resume_starter_template_via_resume_by_event(client) -> None:
    _enable_runtime_approvals(client)
    workflow_name = "Event Resume Starter Worker"
    workflow_id = create_workflow(client, workflow_name)
    manifest = _starter_manifest(
        "event_resume",
        workflow_id=workflow_id,
        workflow_name=workflow_name,
    )
    version_id, _ = create_draft(client, workflow_id, manifest)
    published = client.post(f"{PREFIX}/workflow-versions/{version_id}/publish")
    assert published.status_code == 200

    run_input = '{"document_id":"DOC-7","request":"summarize the release"}'
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": version_id, "input": run_input},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "waiting_event"
        assert run.error_code == "waiting_event"
        summary = dict(run.summary or {})
        checkpoint_id = summary.get("resume_checkpoint_id")
        assert isinstance(checkpoint_id, str) and checkpoint_id
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None
        assert checkpoint.node_id == "wait_gate"
        assert isinstance(checkpoint.state_blob, dict)
        assert checkpoint.state_blob.get("kind") == "wait_for_event"
        assert checkpoint.state_blob.get("input_by_port") == {"input": run_input}
        assert checkpoint.state_blob.get("expected_event_name") == "documents.ready"
        assert checkpoint.state_blob.get("correlation_key") == "document_id"
        assert checkpoint.state_blob.get("correlation_value") == "DOC-7"
        assert checkpoint.state_blob.get("timeout_seconds") == 3600.0

    resumed = client.post(
        f"{PREFIX}/workflow-runs/resume-by-event",
        json={
            "event_name": "documents.ready",
            "event_payload": {"document_id": "DOC-7", "status": "ready"},
        },
    )
    assert resumed.status_code == 202
    assert resumed.json()["data"]["workflow_run_id"] == run_id

    worker._tick()

    final = client.get(f"{PREFIX}/workflow-runs/{run_id}")
    assert final.status_code == 200
    data = final.json()["data"]
    assert data["status"] == "completed"
    assert data["summary"]["output"] == f"[release-agent] processed: {run_input}"

    with client.app.state.session_factory() as session:
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .filter(CaliberWorkflowRunEvent.event_type == "workflow.run.step")
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        wait_gate_steps = [event for event in events if event.node_id == "wait_gate"]
        agent_steps = [event for event in events if event.node_id == "agent"]
        final_steps = [event for event in events if event.node_id == "final"]
        assert [len(wait_gate_steps), len(agent_steps), len(final_steps)] == [2, 1, 1]
        assert dict(wait_gate_steps[0].payload or {}).get("step", {}).get("status") == "blocked"
        assert dict(wait_gate_steps[1].payload or {}).get("step", {}).get("status") == "ok"
        assert dict(agent_steps[0].payload or {}).get("step", {}).get("input_by_port") == {
            "input": run_input
        }


def test_worker_recovers_expired_running_lease(client, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_queue(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]
    original_queued_at: datetime | None = None

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        original_queued_at = _as_utc(run.queued_at)
        run.status = "running"
        run.claimed_by = "worker-old"
        run.claimed_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        run.lease_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        run.last_heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=2)
        session.commit()

    worker = _build_worker(client)
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        client.app.state.event_bus,
        "publish",
        lambda payload: published.append(dict(payload)),
    )
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        # The recovery pass should have requeued the run before this tick
        # claimed and executed it.
        assert run.error_code in {None, "lease_recovered"}
        assert _as_utc(run.queued_at) == original_queued_at
        recovered_events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .filter(CaliberWorkflowRunEvent.event_type == "workflow.run.recovered")
            .all()
        )
        assert recovered_events
    assert any(
        event.get("type") == "workflow.run.recovered"
        and event.get("workflow_run_id") == run_id
        and event.get("status") == "queued"
        and event.get("reason") == "lease_expired"
        for event in published
    )


def test_worker_recovered_run_stays_ahead_of_newer_queued_sibling(client) -> None:
    _enable_queue(client)
    _wid, vid = create_and_publish(client)
    first_created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "recover me first"},
    )
    assert first_created.status_code == 202
    first_run_id = first_created.json()["data"]["workflow_run_id"]
    first_original_output = "recover me first"
    first_original_queued_at: datetime | None = None

    with client.app.state.session_factory() as session:
        first_run = session.get(CaliberWorkflowRun, first_run_id)
        assert first_run is not None
        first_original_queued_at = _as_utc(first_run.queued_at)
        first_run.status = "running"
        first_run.claimed_by = "worker-old"
        first_run.claimed_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        first_run.lease_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        first_run.last_heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=2)
        session.commit()

    second_created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "newer queued sibling"},
    )
    assert second_created.status_code == 202
    second_run_id = second_created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    first_final = client.get(f"{PREFIX}/workflow-runs/{first_run_id}")
    assert first_final.status_code == 200
    first_final_data = first_final.json()["data"]
    assert first_final_data["status"] == "completed"
    assert first_original_output in (first_final_data["summary"]["output"] or "")

    second_queued = client.get(f"{PREFIX}/workflow-runs/{second_run_id}")
    assert second_queued.status_code == 200
    assert second_queued.json()["data"]["status"] == "queued"

    with client.app.state.session_factory() as session:
        first_run = session.get(CaliberWorkflowRun, first_run_id)
        second_run = session.get(CaliberWorkflowRun, second_run_id)
        assert first_run is not None
        assert second_run is not None
        assert _as_utc(first_run.queued_at) == first_original_queued_at
        assert second_run.status == "queued"
        recovered_events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == first_run_id)
            .filter(CaliberWorkflowRunEvent.event_type == "workflow.run.recovered")
            .all()
        )
        assert recovered_events

    worker._tick()

    second_final = client.get(f"{PREFIX}/workflow-runs/{second_run_id}")
    assert second_final.status_code == 200
    assert second_final.json()["data"]["status"] == "completed"


def _seed_waiting_checkpoint(
    session,
    *,
    run: CaliberWorkflowRun,
    checkpoint_id: str,
    node_id: str,
    state_blob: dict[str, object],
    created_at: datetime | None = None,
) -> None:
    run.status = "waiting_event"
    run.current_node_id = node_id
    run.error_code = "waiting_event"
    run.error_summary = "waiting for resume event"
    run.claimed_by = None
    run.claimed_at = None
    run.lease_expires_at = None
    run.summary = {
        **dict(run.summary or {}),
        "status": "waiting_event",
        "resume_checkpoint_id": checkpoint_id,
        "resume_checkpoint_run_id": run.workflow_run_id,
    }
    checkpoint = CaliberWorkflowRunCheckpoint(
        checkpoint_id=checkpoint_id,
        workflow_run_id=run.workflow_run_id,
        project_id=run.project_id,
        sequence=1,
        node_id=node_id,
        state_blob=state_blob,
    )
    if created_at is not None:
        checkpoint.created_at = created_at
    session.add(checkpoint)


def _mark_run_waiting_for_maintenance(
    run: CaliberWorkflowRun,
    *,
    checkpoint_id: str | None = None,
    checkpoint_run_id: str | None = None,
    node_id: str = "wait_gate",
) -> None:
    run.status = "waiting_event"
    run.current_node_id = node_id
    run.error_code = "waiting_event"
    run.error_summary = "waiting for resume event"
    run.claimed_by = None
    run.claimed_at = None
    run.lease_expires_at = None
    summary = {
        **dict(run.summary or {}),
        "status": "waiting_event",
    }
    if checkpoint_id is not None:
        summary["resume_checkpoint_id"] = checkpoint_id
        summary["resume_checkpoint_run_id"] = checkpoint_run_id or run.workflow_run_id
    run.summary = summary


def _create_operator_churn_runs(client) -> tuple[str, str, str]:
    _recovery_wid, recovery_vid = create_and_publish(
        client,
        workflow_name="Lease Recovery Worker",
    )
    auto_resume_manifest = _wait_until_manifest(
        "wait-until-operator-churn-wf",
        wait_until="2099-01-01T00:00:00",
        timezone_name="UTC",
    )
    _auto_wid, auto_resume_vid = create_and_publish(
        client,
        workflow_name="Wait Until Operator Churn Worker",
        manifest=auto_resume_manifest,
    )
    timeout_manifest = _wait_event_manifest(
        "wait-event-operator-churn-wf",
        timeout_seconds=30,
    )
    _timeout_wid, timeout_vid = create_and_publish(
        client,
        workflow_name="Wait Event Operator Churn Worker",
        manifest=timeout_manifest,
    )

    recovery_created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": recovery_vid, "input": "hello"},
    )
    auto_resume_created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": auto_resume_vid, "input": "resume automatically"},
    )
    timeout_created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": timeout_vid, "input": "resume on event"},
    )
    assert recovery_created.status_code == 202
    assert auto_resume_created.status_code == 202
    assert timeout_created.status_code == 202
    return (
        recovery_created.json()["data"]["workflow_run_id"],
        auto_resume_created.json()["data"]["workflow_run_id"],
        timeout_created.json()["data"]["workflow_run_id"],
    )


def _prime_operator_churn_states(
    client,
    *,
    recovery_run_id: str,
    auto_resume_run_id: str,
    timeout_run_id: str,
) -> None:
    with client.app.state.session_factory() as session:
        recovery_run = session.get(CaliberWorkflowRun, recovery_run_id)
        auto_resume_run = session.get(CaliberWorkflowRun, auto_resume_run_id)
        timeout_run = session.get(CaliberWorkflowRun, timeout_run_id)
        assert recovery_run is not None
        assert auto_resume_run is not None
        assert timeout_run is not None

        recovery_run.status = "running"
        recovery_run.claimed_by = "worker-old"
        recovery_run.claimed_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        recovery_run.lease_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        recovery_run.last_heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=2)

        _seed_waiting_checkpoint(
            session,
            run=auto_resume_run,
            checkpoint_id="WRC-auto-resume",
            node_id="wait_gate",
            state_blob={
                "kind": "wait_until",
                "node_id": "wait_gate",
                "resume_at": (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(),
                "wait_until": "2099-01-01T00:00:00",
                "timezone": "UTC",
                "input_by_port": {"input": "resume automatically"},
            },
        )
        _seed_waiting_checkpoint(
            session,
            run=timeout_run,
            checkpoint_id="WRC-timeout",
            node_id="wait_gate",
            state_blob={
                "kind": "wait_for_event",
                "node_id": "wait_gate",
                "expected_event_name": "ticket.approved",
                "timeout_seconds": 30.0,
                "input_by_port": {"input": "resume on event"},
            },
            created_at=datetime.now(timezone.utc) - timedelta(seconds=45),
        )
        session.commit()


def _assert_operator_churn_db_state(
    client,
    *,
    recovery_run_id: str,
    auto_resume_run_id: str,
    timeout_run_id: str,
) -> str:
    with client.app.state.session_factory() as session:
        recovery_run = session.get(CaliberWorkflowRun, recovery_run_id)
        auto_resume_run = session.get(CaliberWorkflowRun, auto_resume_run_id)
        timeout_run = session.get(CaliberWorkflowRun, timeout_run_id)
        assert recovery_run is not None
        assert auto_resume_run is not None
        assert timeout_run is not None
        assert recovery_run.status == "queued"
        assert recovery_run.error_code == "lease_recovered"
        assert auto_resume_run.status == "queued"
        assert auto_resume_run.error_code is None
        assert timeout_run.status == "expired"
        assert timeout_run.error_code == "wait_for_event_timeout"

        recovery_events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == recovery_run_id)
            .filter(CaliberWorkflowRunEvent.event_type == "workflow.run.recovered")
            .all()
        )
        resumed_events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == auto_resume_run_id)
            .filter(CaliberWorkflowRunEvent.event_type == "workflow.run.resumed")
            .all()
        )
        expired_events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == timeout_run_id)
            .filter(CaliberWorkflowRunEvent.event_type == "workflow.run.expired")
            .all()
        )
        assert recovery_events
        assert resumed_events
        assert expired_events
        assert resumed_events[-1].payload["auto"] is True
        assert resumed_events[-1].payload["reason"] == "wait_until_due"
        assert expired_events[-1].payload["reason"] == "wait_for_event_timeout"
        return str(timeout_run.error_summary)


def _create_batched_operator_churn_runs(
    client,
    *,
    count: int,
    name_suffix: str = "",
) -> tuple[list[str], list[str], list[str]]:
    assert count > 0
    workflow_suffix = f" {name_suffix}" if name_suffix else ""
    manifest_suffix = f"-{name_suffix.replace(' ', '-')}" if name_suffix else ""
    _recovery_wid, recovery_vid = create_and_publish(
        client,
        workflow_name=f"Lease Recovery Worker Batch{workflow_suffix}",
    )
    auto_resume_manifest = _wait_until_manifest(
        f"wait-until-operator-churn-batch{manifest_suffix}-wf",
        wait_until="2099-01-01T00:00:00",
        timezone_name="UTC",
    )
    _auto_wid, auto_resume_vid = create_and_publish(
        client,
        workflow_name=f"Wait Until Operator Churn Worker Batch{workflow_suffix}",
        manifest=auto_resume_manifest,
    )
    timeout_manifest = _wait_event_manifest(
        f"wait-event-operator-churn-batch{manifest_suffix}-wf",
        timeout_seconds=30,
    )
    _timeout_wid, timeout_vid = create_and_publish(
        client,
        workflow_name=f"Wait Event Operator Churn Worker Batch{workflow_suffix}",
        manifest=timeout_manifest,
    )

    recovery_run_ids: list[str] = []
    auto_resume_run_ids: list[str] = []
    timeout_run_ids: list[str] = []
    for index in range(count):
        recovery_created = client.post(
            f"{PREFIX}/workflow-runs",
            json={"workflow_version_id": recovery_vid, "input": f"hello {index}"},
        )
        auto_resume_created = client.post(
            f"{PREFIX}/workflow-runs",
            json={"workflow_version_id": auto_resume_vid, "input": f"resume automatically {index}"},
        )
        timeout_created = client.post(
            f"{PREFIX}/workflow-runs",
            json={"workflow_version_id": timeout_vid, "input": f"resume on event {index}"},
        )
        assert recovery_created.status_code == 202
        assert auto_resume_created.status_code == 202
        assert timeout_created.status_code == 202
        recovery_run_ids.append(recovery_created.json()["data"]["workflow_run_id"])
        auto_resume_run_ids.append(auto_resume_created.json()["data"]["workflow_run_id"])
        timeout_run_ids.append(timeout_created.json()["data"]["workflow_run_id"])
    return recovery_run_ids, auto_resume_run_ids, timeout_run_ids


def _prime_batched_operator_churn_states(
    client,
    *,
    recovery_run_ids: list[str],
    auto_resume_run_ids: list[str],
    timeout_run_ids: list[str],
) -> None:
    assert len(recovery_run_ids) == len(auto_resume_run_ids) == len(timeout_run_ids)
    with client.app.state.session_factory() as session:
        for index, (recovery_run_id, auto_resume_run_id, timeout_run_id) in enumerate(
            zip(recovery_run_ids, auto_resume_run_ids, timeout_run_ids, strict=True)
        ):
            recovery_run = session.get(CaliberWorkflowRun, recovery_run_id)
            auto_resume_run = session.get(CaliberWorkflowRun, auto_resume_run_id)
            timeout_run = session.get(CaliberWorkflowRun, timeout_run_id)
            assert recovery_run is not None
            assert auto_resume_run is not None
            assert timeout_run is not None

            recovery_run.status = "running"
            recovery_run.claimed_by = "worker-old"
            recovery_run.claimed_at = datetime.now(timezone.utc) - timedelta(minutes=5)
            recovery_run.lease_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            recovery_run.last_heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=2)

            _seed_waiting_checkpoint(
                session,
                run=auto_resume_run,
                checkpoint_id=f"WRC-auto-resume-{auto_resume_run_id}",
                node_id="wait_gate",
                state_blob={
                    "kind": "wait_until",
                    "node_id": "wait_gate",
                    "resume_at": (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(),
                    "wait_until": "2099-01-01T00:00:00",
                    "timezone": "UTC",
                    "input_by_port": {"input": f"resume automatically {index}"},
                },
            )
            _seed_waiting_checkpoint(
                session,
                run=timeout_run,
                checkpoint_id=f"WRC-timeout-{timeout_run_id}",
                node_id="wait_gate",
                state_blob={
                    "kind": "wait_for_event",
                    "node_id": "wait_gate",
                    "expected_event_name": "ticket.approved",
                    "timeout_seconds": 30.0,
                    "input_by_port": {"input": f"resume on event {index}"},
                },
                created_at=datetime.now(timezone.utc) - timedelta(seconds=45),
            )
        session.commit()


def _assert_batched_operator_churn_db_state(
    client,
    *,
    recovery_run_ids: list[str],
    auto_resume_run_ids: list[str],
    timeout_run_ids: list[str],
) -> dict[str, str]:
    assert len(recovery_run_ids) == len(auto_resume_run_ids) == len(timeout_run_ids)
    timeout_summaries: dict[str, str] = {}
    with client.app.state.session_factory() as session:
        recovery_runs = (
            session.query(CaliberWorkflowRun)
            .filter(CaliberWorkflowRun.workflow_run_id.in_(recovery_run_ids))
            .all()
        )
        auto_resume_runs = (
            session.query(CaliberWorkflowRun)
            .filter(CaliberWorkflowRun.workflow_run_id.in_(auto_resume_run_ids))
            .all()
        )
        timeout_runs = (
            session.query(CaliberWorkflowRun)
            .filter(CaliberWorkflowRun.workflow_run_id.in_(timeout_run_ids))
            .all()
        )
        assert {run.workflow_run_id for run in recovery_runs} == set(recovery_run_ids)
        assert {run.workflow_run_id for run in auto_resume_runs} == set(auto_resume_run_ids)
        assert {run.workflow_run_id for run in timeout_runs} == set(timeout_run_ids)
        assert all(
            run.status == "queued" and run.error_code == "lease_recovered" for run in recovery_runs
        )
        assert all(run.status == "queued" and run.error_code is None for run in auto_resume_runs)
        assert all(
            run.status == "expired" and run.error_code == "wait_for_event_timeout"
            for run in timeout_runs
        )

        recovery_event_rows = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id.in_(recovery_run_ids))
            .filter(CaliberWorkflowRunEvent.event_type == "workflow.run.recovered")
            .all()
        )
        resumed_event_rows = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id.in_(auto_resume_run_ids))
            .filter(CaliberWorkflowRunEvent.event_type == "workflow.run.resumed")
            .all()
        )
        expired_event_rows = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id.in_(timeout_run_ids))
            .filter(CaliberWorkflowRunEvent.event_type == "workflow.run.expired")
            .all()
        )
        assert len(recovery_event_rows) == len(recovery_run_ids)
        assert len(resumed_event_rows) == len(auto_resume_run_ids)
        assert len(expired_event_rows) == len(timeout_run_ids)
        assert all(event.payload["reason"] == "wait_until_due" for event in resumed_event_rows)
        assert all(event.payload["auto"] is True for event in resumed_event_rows)
        assert all(
            event.payload["reason"] == "wait_for_event_timeout" for event in expired_event_rows
        )
        for run in timeout_runs:
            timeout_summaries[run.workflow_run_id] = str(run.error_summary)
    return timeout_summaries


def _assert_batched_operator_churn_published_events(
    published: list[dict[str, object]],
    *,
    recovery_run_ids: list[str],
    auto_resume_run_ids: list[str],
    timeout_summaries: dict[str, str],
) -> None:
    timeout_run_ids = list(timeout_summaries)
    assert len(published) == len(recovery_run_ids) + len(auto_resume_run_ids) + len(timeout_run_ids)

    published_types = [event.get("type") for event in published]
    assert published_types.count("workflow.run.recovered") == len(recovery_run_ids)
    assert published_types.count("workflow.run.resumed") == len(auto_resume_run_ids)
    assert published_types.count("workflow.run.expired") == len(timeout_run_ids)
    assert {
        event.get("workflow_run_id")
        for event in published
        if event.get("type") == "workflow.run.recovered"
    } == set(recovery_run_ids)
    assert {
        event.get("workflow_run_id")
        for event in published
        if event.get("type") == "workflow.run.resumed"
    } == set(auto_resume_run_ids)
    assert {
        event.get("workflow_run_id")
        for event in published
        if event.get("type") == "workflow.run.expired"
    } == set(timeout_run_ids)
    assert all(
        event.get("reason") == "lease_expired"
        for event in published
        if event.get("type") == "workflow.run.recovered"
    )
    assert all(
        event.get("status") == "queued"
        for event in published
        if event.get("type") == "workflow.run.resumed"
    )
    for event in published:
        if event.get("type") != "workflow.run.expired":
            continue
        run_id = event.get("workflow_run_id")
        assert isinstance(run_id, str)
        assert event.get("error") == timeout_summaries[run_id]


def _exercise_operator_churn_load_profile(
    client,
    *,
    worker,
    published: list[dict[str, object]],
    batch_sizes: tuple[int, ...],
    name_prefix: str,
) -> list[tuple[list[str], list[str], list[str], dict[str, str]]]:
    processed_batches: list[tuple[list[str], list[str], list[str], dict[str, str]]] = []
    for cycle_index, batch_size in enumerate(batch_sizes, start=1):
        recovery_run_ids, auto_resume_run_ids, timeout_run_ids = (
            _create_batched_operator_churn_runs(
                client,
                count=batch_size,
                name_suffix=f"{name_prefix}-cycle-{cycle_index}",
            )
        )
        _prime_batched_operator_churn_states(
            client,
            recovery_run_ids=recovery_run_ids,
            auto_resume_run_ids=auto_resume_run_ids,
            timeout_run_ids=timeout_run_ids,
        )

        pre_tick_event_count = len(published)
        worker._tick()
        timeout_summaries = _assert_batched_operator_churn_db_state(
            client,
            recovery_run_ids=recovery_run_ids,
            auto_resume_run_ids=auto_resume_run_ids,
            timeout_run_ids=timeout_run_ids,
        )
        cycle_events = published[pre_tick_event_count:]
        _assert_batched_operator_churn_published_events(
            cycle_events,
            recovery_run_ids=recovery_run_ids,
            auto_resume_run_ids=auto_resume_run_ids,
            timeout_summaries=timeout_summaries,
        )
        processed_batches.append(
            (
                recovery_run_ids,
                auto_resume_run_ids,
                timeout_run_ids,
                timeout_summaries,
            )
        )

        worker._tick()
        assert len(published) == pre_tick_event_count + len(cycle_events)
        for (
            prior_recovery_run_ids,
            prior_auto_resume_run_ids,
            prior_timeout_run_ids,
            _prior_timeout_summaries,
        ) in processed_batches:
            _assert_batched_operator_churn_db_state(
                client,
                recovery_run_ids=prior_recovery_run_ids,
                auto_resume_run_ids=prior_auto_resume_run_ids,
                timeout_run_ids=prior_timeout_run_ids,
            )
    return processed_batches


def _assert_operator_churn_load_profile_aggregate(
    client,
    *,
    published: list[dict[str, object]],
    processed_batches: list[tuple[list[str], list[str], list[str], dict[str, str]]],
) -> None:
    maintenance_types = {
        "workflow.run.recovered",
        "workflow.run.resumed",
        "workflow.run.expired",
    }
    maintenance_events = [event for event in published if event.get("type") in maintenance_types]

    all_recovery_run_ids: list[str] = []
    all_auto_resume_run_ids: list[str] = []
    all_timeout_summaries: dict[str, str] = {}
    expected_event_count = 0
    for (
        recovery_run_ids,
        auto_resume_run_ids,
        timeout_run_ids,
        timeout_summaries,
    ) in processed_batches:
        _assert_batched_operator_churn_db_state(
            client,
            recovery_run_ids=recovery_run_ids,
            auto_resume_run_ids=auto_resume_run_ids,
            timeout_run_ids=timeout_run_ids,
        )
        all_recovery_run_ids.extend(recovery_run_ids)
        all_auto_resume_run_ids.extend(auto_resume_run_ids)
        all_timeout_summaries.update(timeout_summaries)
        expected_event_count += (
            len(recovery_run_ids) + len(auto_resume_run_ids) + len(timeout_summaries)
        )

    assert len(maintenance_events) == expected_event_count
    unique_event_keys = {
        (str(event.get("type")), str(event.get("workflow_run_id"))) for event in maintenance_events
    }
    assert len(unique_event_keys) == expected_event_count
    _assert_batched_operator_churn_published_events(
        maintenance_events,
        recovery_run_ids=all_recovery_run_ids,
        auto_resume_run_ids=all_auto_resume_run_ids,
        timeout_summaries=all_timeout_summaries,
    )


def test_worker_tick_emits_operator_visible_recovery_resume_and_timeout_events_in_one_pass(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime_approvals(client)
    recovery_run_id, auto_resume_run_id, timeout_run_id = _create_operator_churn_runs(client)
    _prime_operator_churn_states(
        client,
        recovery_run_id=recovery_run_id,
        auto_resume_run_id=auto_resume_run_id,
        timeout_run_id=timeout_run_id,
    )

    worker = _build_worker(client)
    monkeypatch.setattr(worker, "_claim_next_run", lambda: None)
    monkeypatch.setattr(
        "caliber.workflows.runtime._wait_until_ready",
        lambda raw, *, timezone_name="UTC": True,
    )
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        client.app.state.event_bus,
        "publish",
        lambda payload: published.append(dict(payload)),
    )

    worker._tick()
    timeout_error_summary = _assert_operator_churn_db_state(
        client,
        recovery_run_id=recovery_run_id,
        auto_resume_run_id=auto_resume_run_id,
        timeout_run_id=timeout_run_id,
    )

    published_types = [event.get("type") for event in published]
    assert published_types.count("workflow.run.recovered") == 1
    assert published_types.count("workflow.run.resumed") == 1
    assert published_types.count("workflow.run.expired") == 1
    assert any(
        event.get("type") == "workflow.run.recovered"
        and event.get("workflow_run_id") == recovery_run_id
        and event.get("reason") == "lease_expired"
        for event in published
    )
    assert any(
        event.get("type") == "workflow.run.resumed"
        and event.get("workflow_run_id") == auto_resume_run_id
        and event.get("status") == "queued"
        for event in published
    )
    assert any(
        event.get("type") == "workflow.run.expired"
        and event.get("workflow_run_id") == timeout_run_id
        and event.get("error") == timeout_error_summary
        for event in published
    )


def test_worker_tick_processes_batched_operator_churn_runs_in_one_pass(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime_approvals(client)
    recovery_run_ids, auto_resume_run_ids, timeout_run_ids = _create_batched_operator_churn_runs(
        client,
        count=8,
    )
    _prime_batched_operator_churn_states(
        client,
        recovery_run_ids=recovery_run_ids,
        auto_resume_run_ids=auto_resume_run_ids,
        timeout_run_ids=timeout_run_ids,
    )

    worker = _build_worker(client)
    monkeypatch.setattr(worker, "_claim_next_run", lambda: None)
    monkeypatch.setattr(
        "caliber.workflows.runtime._wait_until_ready",
        lambda raw, *, timezone_name="UTC": True,
    )
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        client.app.state.event_bus,
        "publish",
        lambda payload: published.append(dict(payload)),
    )

    worker._tick()
    timeout_summaries = _assert_batched_operator_churn_db_state(
        client,
        recovery_run_ids=recovery_run_ids,
        auto_resume_run_ids=auto_resume_run_ids,
        timeout_run_ids=timeout_run_ids,
    )
    _assert_batched_operator_churn_published_events(
        published,
        recovery_run_ids=recovery_run_ids,
        auto_resume_run_ids=auto_resume_run_ids,
        timeout_summaries=timeout_summaries,
    )


def test_worker_tick_processes_large_operator_churn_burst_batch_in_one_pass(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime_approvals(client)
    recovery_run_ids, auto_resume_run_ids, timeout_run_ids = _create_batched_operator_churn_runs(
        client,
        count=20,
        name_suffix="burst",
    )
    _prime_batched_operator_churn_states(
        client,
        recovery_run_ids=recovery_run_ids,
        auto_resume_run_ids=auto_resume_run_ids,
        timeout_run_ids=timeout_run_ids,
    )

    worker = _build_worker(client)
    monkeypatch.setattr(worker, "_claim_next_run", lambda: None)
    monkeypatch.setattr(
        "caliber.workflows.runtime._wait_until_ready",
        lambda raw, *, timezone_name="UTC": True,
    )
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        client.app.state.event_bus,
        "publish",
        lambda payload: published.append(dict(payload)),
    )

    worker._tick()
    timeout_summaries = _assert_batched_operator_churn_db_state(
        client,
        recovery_run_ids=recovery_run_ids,
        auto_resume_run_ids=auto_resume_run_ids,
        timeout_run_ids=timeout_run_ids,
    )
    _assert_batched_operator_churn_published_events(
        published,
        recovery_run_ids=recovery_run_ids,
        auto_resume_run_ids=auto_resume_run_ids,
        timeout_summaries=timeout_summaries,
    )
    assert len(published) == 60

    worker._tick()
    _assert_batched_operator_churn_db_state(
        client,
        recovery_run_ids=recovery_run_ids,
        auto_resume_run_ids=auto_resume_run_ids,
        timeout_run_ids=timeout_run_ids,
    )
    assert len(published) == 60


def test_worker_tick_sustains_repeated_batched_operator_churn_without_duplicate_events(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime_approvals(client)
    first_recovery_run_ids, first_auto_resume_run_ids, first_timeout_run_ids = (
        _create_batched_operator_churn_runs(
            client,
            count=5,
            name_suffix="first",
        )
    )
    _prime_batched_operator_churn_states(
        client,
        recovery_run_ids=first_recovery_run_ids,
        auto_resume_run_ids=first_auto_resume_run_ids,
        timeout_run_ids=first_timeout_run_ids,
    )

    worker = _build_worker(client)
    monkeypatch.setattr(worker, "_claim_next_run", lambda: None)
    monkeypatch.setattr(
        "caliber.workflows.runtime._wait_until_ready",
        lambda raw, *, timezone_name="UTC": True,
    )
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        client.app.state.event_bus,
        "publish",
        lambda payload: published.append(dict(payload)),
    )

    worker._tick()
    first_timeout_summaries = _assert_batched_operator_churn_db_state(
        client,
        recovery_run_ids=first_recovery_run_ids,
        auto_resume_run_ids=first_auto_resume_run_ids,
        timeout_run_ids=first_timeout_run_ids,
    )
    first_event_count = len(published)
    assert first_event_count == len(first_recovery_run_ids) * 3

    worker._tick()
    _assert_batched_operator_churn_db_state(
        client,
        recovery_run_ids=first_recovery_run_ids,
        auto_resume_run_ids=first_auto_resume_run_ids,
        timeout_run_ids=first_timeout_run_ids,
    )
    assert len(published) == first_event_count

    second_recovery_run_ids, second_auto_resume_run_ids, second_timeout_run_ids = (
        _create_batched_operator_churn_runs(
            client,
            count=3,
            name_suffix="second",
        )
    )
    _prime_batched_operator_churn_states(
        client,
        recovery_run_ids=second_recovery_run_ids,
        auto_resume_run_ids=second_auto_resume_run_ids,
        timeout_run_ids=second_timeout_run_ids,
    )
    pre_second_tick_event_count = len(published)

    worker._tick()
    second_timeout_summaries = _assert_batched_operator_churn_db_state(
        client,
        recovery_run_ids=second_recovery_run_ids,
        auto_resume_run_ids=second_auto_resume_run_ids,
        timeout_run_ids=second_timeout_run_ids,
    )
    _assert_batched_operator_churn_db_state(
        client,
        recovery_run_ids=first_recovery_run_ids,
        auto_resume_run_ids=first_auto_resume_run_ids,
        timeout_run_ids=first_timeout_run_ids,
    )
    second_batch_events = published[pre_second_tick_event_count:]
    _assert_batched_operator_churn_published_events(
        second_batch_events,
        recovery_run_ids=second_recovery_run_ids,
        auto_resume_run_ids=second_auto_resume_run_ids,
        timeout_summaries=second_timeout_summaries,
    )

    worker._tick()
    _assert_batched_operator_churn_db_state(
        client,
        recovery_run_ids=second_recovery_run_ids,
        auto_resume_run_ids=second_auto_resume_run_ids,
        timeout_run_ids=second_timeout_run_ids,
    )
    assert len(published) == pre_second_tick_event_count + len(second_recovery_run_ids) * 3

    all_timeout_summaries = {**first_timeout_summaries, **second_timeout_summaries}
    for event in published:
        if event.get("type") != "workflow.run.expired":
            continue
        run_id = event.get("workflow_run_id")
        assert isinstance(run_id, str)
        assert event.get("error") == all_timeout_summaries[run_id]


def test_worker_tick_sustains_multicycle_operator_churn_across_varied_batches_without_stale_events(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime_approvals(client)

    worker = _build_worker(client)
    monkeypatch.setattr(worker, "_claim_next_run", lambda: None)
    monkeypatch.setattr(
        "caliber.workflows.runtime._wait_until_ready",
        lambda raw, *, timezone_name="UTC": True,
    )
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        client.app.state.event_bus,
        "publish",
        lambda payload: published.append(dict(payload)),
    )

    # Approximate a fast soak by repeating fresh mixed-churn batches with idle ticks in between.
    batch_sizes = (2, 4, 6, 8)
    processed_batches: list[tuple[list[str], list[str], list[str], dict[str, str]]] = []
    for cycle_index, batch_size in enumerate(batch_sizes, start=1):
        recovery_run_ids, auto_resume_run_ids, timeout_run_ids = (
            _create_batched_operator_churn_runs(
                client,
                count=batch_size,
                name_suffix=f"cycle-{cycle_index}",
            )
        )
        _prime_batched_operator_churn_states(
            client,
            recovery_run_ids=recovery_run_ids,
            auto_resume_run_ids=auto_resume_run_ids,
            timeout_run_ids=timeout_run_ids,
        )

        pre_tick_event_count = len(published)
        worker._tick()
        timeout_summaries = _assert_batched_operator_churn_db_state(
            client,
            recovery_run_ids=recovery_run_ids,
            auto_resume_run_ids=auto_resume_run_ids,
            timeout_run_ids=timeout_run_ids,
        )
        cycle_events = published[pre_tick_event_count:]
        _assert_batched_operator_churn_published_events(
            cycle_events,
            recovery_run_ids=recovery_run_ids,
            auto_resume_run_ids=auto_resume_run_ids,
            timeout_summaries=timeout_summaries,
        )
        processed_batches.append(
            (
                recovery_run_ids,
                auto_resume_run_ids,
                timeout_run_ids,
                timeout_summaries,
            )
        )

        worker._tick()
        assert len(published) == pre_tick_event_count + len(cycle_events)
        for (
            prior_recovery_run_ids,
            prior_auto_resume_run_ids,
            prior_timeout_run_ids,
            _prior_timeout_summaries,
        ) in processed_batches:
            _assert_batched_operator_churn_db_state(
                client,
                recovery_run_ids=prior_recovery_run_ids,
                auto_resume_run_ids=prior_auto_resume_run_ids,
                timeout_run_ids=prior_timeout_run_ids,
            )

    maintenance_types = {
        "workflow.run.recovered",
        "workflow.run.resumed",
        "workflow.run.expired",
    }
    maintenance_events = [event for event in published if event.get("type") in maintenance_types]
    assert len(maintenance_events) == sum(batch_size * 3 for batch_size in batch_sizes)

    all_recovery_run_ids: list[str] = []
    all_auto_resume_run_ids: list[str] = []
    all_timeout_summaries: dict[str, str] = {}
    for (
        recovery_run_ids,
        auto_resume_run_ids,
        timeout_run_ids,
        timeout_summaries,
    ) in processed_batches:
        _assert_batched_operator_churn_db_state(
            client,
            recovery_run_ids=recovery_run_ids,
            auto_resume_run_ids=auto_resume_run_ids,
            timeout_run_ids=timeout_run_ids,
        )
        all_recovery_run_ids.extend(recovery_run_ids)
        all_auto_resume_run_ids.extend(auto_resume_run_ids)
        all_timeout_summaries.update(timeout_summaries)
    _assert_batched_operator_churn_published_events(
        maintenance_events,
        recovery_run_ids=all_recovery_run_ids,
        auto_resume_run_ids=all_auto_resume_run_ids,
        timeout_summaries=all_timeout_summaries,
    )


def test_worker_tick_sustains_extended_operator_churn_load_profile_without_stale_events(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime_approvals(client)

    worker = _build_worker(client)
    monkeypatch.setattr(worker, "_claim_next_run", lambda: None)
    monkeypatch.setattr(
        "caliber.workflows.runtime._wait_until_ready",
        lambda raw, *, timezone_name="UTC": True,
    )
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        client.app.state.event_bus,
        "publish",
        lambda payload: published.append(dict(payload)),
    )

    processed_batches = _exercise_operator_churn_load_profile(
        client,
        worker=worker,
        published=published,
        batch_sizes=(3, 6, 9, 12, 15, 18),
        name_prefix="extended-load",
    )

    _assert_operator_churn_load_profile_aggregate(
        client,
        published=published,
        processed_batches=processed_batches,
    )


def test_worker_tick_drains_older_normal_backlog_while_processing_operator_maintenance_cycles(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_queue(client)
    _enable_runtime_approvals(client)

    _wid, vid = create_and_publish(
        client,
        workflow_name="Normal Queue Backlog Under Maintenance Worker",
    )
    normal_run_ids: list[str] = []
    for index in range(4):
        created = client.post(
            f"{PREFIX}/workflow-runs",
            json={"workflow_version_id": vid, "input": f"normal backlog {index}"},
        )
        assert created.status_code == 202
        normal_run_ids.append(created.json()["data"]["workflow_run_id"])

    worker = _build_worker(client)
    monkeypatch.setattr(
        "caliber.workflows.runtime._wait_until_ready",
        lambda raw, *, timezone_name="UTC": True,
    )
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        client.app.state.event_bus,
        "publish",
        lambda payload: published.append(dict(payload)),
    )

    maintenance_types = {
        "workflow.run.recovered",
        "workflow.run.resumed",
        "workflow.run.expired",
    }
    processed_batches: list[tuple[list[str], list[str], dict[str, str]]] = []
    batch_size = 3

    for cycle_index, expected_normal_run_id in enumerate(normal_run_ids, start=1):
        recovery_run_ids, auto_resume_run_ids, timeout_run_ids = (
            _create_batched_operator_churn_runs(
                client,
                count=batch_size,
                name_suffix=f"normal-backlog-cycle-{cycle_index}",
            )
        )
        _prime_batched_operator_churn_states(
            client,
            recovery_run_ids=recovery_run_ids,
            auto_resume_run_ids=auto_resume_run_ids,
            timeout_run_ids=timeout_run_ids,
        )

        pre_tick_event_count = len(published)
        worker._tick()
        cycle_events = published[pre_tick_event_count:]
        timeout_summaries = _assert_batched_operator_churn_db_state(
            client,
            recovery_run_ids=recovery_run_ids,
            auto_resume_run_ids=auto_resume_run_ids,
            timeout_run_ids=timeout_run_ids,
        )
        maintenance_events = [
            event for event in cycle_events if event.get("type") in maintenance_types
        ]
        _assert_batched_operator_churn_published_events(
            maintenance_events,
            recovery_run_ids=recovery_run_ids,
            auto_resume_run_ids=auto_resume_run_ids,
            timeout_summaries=timeout_summaries,
        )
        processed_batches.append((recovery_run_ids, auto_resume_run_ids, timeout_summaries))

        completed_run_ids = {
            str(event.get("workflow_run_id"))
            for event in cycle_events
            if event.get("type") == "workflow.run.completed"
        }
        assert completed_run_ids == {expected_normal_run_id}

        with client.app.state.session_factory() as session:
            for completed_run_id in normal_run_ids[:cycle_index]:
                completed_run = session.get(CaliberWorkflowRun, completed_run_id)
                assert completed_run is not None
                assert completed_run.status == "completed"

            for queued_run_id in normal_run_ids[cycle_index:]:
                queued_run = session.get(CaliberWorkflowRun, queued_run_id)
                assert queued_run is not None
                assert queued_run.status == "queued"

    all_maintenance_events = [
        event for event in published if event.get("type") in maintenance_types
    ]
    assert len(all_maintenance_events) == len(normal_run_ids) * batch_size * 3
    assert len(
        {
            (str(event.get("type")), str(event.get("workflow_run_id")))
            for event in all_maintenance_events
        }
    ) == len(all_maintenance_events)

    all_recovery_run_ids: list[str] = []
    all_auto_resume_run_ids: list[str] = []
    all_timeout_summaries: dict[str, str] = {}
    for recovery_run_ids, auto_resume_run_ids, timeout_summaries in processed_batches:
        all_recovery_run_ids.extend(recovery_run_ids)
        all_auto_resume_run_ids.extend(auto_resume_run_ids)
        all_timeout_summaries.update(timeout_summaries)
    _assert_batched_operator_churn_published_events(
        all_maintenance_events,
        recovery_run_ids=all_recovery_run_ids,
        auto_resume_run_ids=all_auto_resume_run_ids,
        timeout_summaries=all_timeout_summaries,
    )


def _exercise_mixed_valid_invalid_maintenance_backlog_profile(
    client,
    *,
    worker,
    published: list[dict[str, object]],
    normal_run_ids: list[str],
    batch_sizes: tuple[int, ...],
    name_prefix: str,
) -> list[dict[str, object]]:
    assert len(normal_run_ids) == len(batch_sizes)
    maintenance_types = {
        "workflow.run.recovered",
        "workflow.run.resumed",
        "workflow.run.expired",
        "workflow.run.failed",
    }
    processed_cycles: list[dict[str, object]] = []

    for cycle_index, (expected_normal_run_id, batch_size) in enumerate(
        zip(normal_run_ids, batch_sizes, strict=True),
        start=1,
    ):
        cycle_suffix = f"{name_prefix}-cycle-{cycle_index}"
        recovery_run_ids, auto_resume_run_ids, timeout_run_ids = (
            _create_batched_operator_churn_runs(
                client,
                count=batch_size,
                name_suffix=cycle_suffix,
            )
        )
        _prime_batched_operator_churn_states(
            client,
            recovery_run_ids=recovery_run_ids,
            auto_resume_run_ids=auto_resume_run_ids,
            timeout_run_ids=timeout_run_ids,
        )
        invalid_wait_run_id, foreign_wait_run_id, source_wait_run_id = (
            _create_invalid_maintenance_runs(
                client,
                name_suffix=cycle_suffix,
            )
        )
        _prime_invalid_maintenance_states(
            client,
            invalid_wait_run_id=invalid_wait_run_id,
            foreign_wait_run_id=foreign_wait_run_id,
            source_wait_run_id=source_wait_run_id,
            name_suffix=cycle_suffix,
        )

        cycle_start = len(published)
        worker._tick()
        cycle_events = published[cycle_start:]

        timeout_summaries = _assert_batched_operator_churn_db_state(
            client,
            recovery_run_ids=recovery_run_ids,
            auto_resume_run_ids=auto_resume_run_ids,
            timeout_run_ids=timeout_run_ids,
        )
        _assert_invalid_maintenance_runs_fail_closed(
            client,
            invalid_wait_run_id=invalid_wait_run_id,
            foreign_wait_run_id=foreign_wait_run_id,
            source_wait_run_id=source_wait_run_id,
            published=cycle_events,
        )

        maintenance_events = [
            event for event in cycle_events if event.get("type") in maintenance_types
        ]
        assert len(maintenance_events) == (batch_size * 3) + 3

        completed_run_ids = {
            str(event.get("workflow_run_id"))
            for event in cycle_events
            if event.get("type") == "workflow.run.completed"
        }
        assert completed_run_ids == {expected_normal_run_id}

        cycle_data = {
            "recovery_run_ids": recovery_run_ids,
            "auto_resume_run_ids": auto_resume_run_ids,
            "timeout_run_ids": timeout_run_ids,
            "timeout_summaries": timeout_summaries,
            "invalid_wait_run_id": invalid_wait_run_id,
            "foreign_wait_run_id": foreign_wait_run_id,
            "source_wait_run_id": source_wait_run_id,
        }
        processed_cycles.append(cycle_data)
        _assert_mixed_valid_invalid_maintenance_cycle(
            client,
            cycle_data=cycle_data,
            published=maintenance_events,
        )

        with client.app.state.session_factory() as session:
            for completed_run_id in normal_run_ids[:cycle_index]:
                completed_run = session.get(CaliberWorkflowRun, completed_run_id)
                assert completed_run is not None
                assert completed_run.status == "completed"

            for queued_run_id in normal_run_ids[cycle_index:]:
                queued_run = session.get(CaliberWorkflowRun, queued_run_id)
                assert queued_run is not None
                assert queued_run.status == "queued"

    return processed_cycles


def _assert_mixed_valid_invalid_maintenance_backlog_profile_aggregate(
    *,
    published: list[dict[str, object]],
    processed_cycles: list[dict[str, object]],
) -> None:
    maintenance_types = {
        "workflow.run.recovered",
        "workflow.run.resumed",
        "workflow.run.expired",
        "workflow.run.failed",
    }
    maintenance_events = [event for event in published if event.get("type") in maintenance_types]
    (
        all_recovery_run_ids,
        all_auto_resume_run_ids,
        all_timeout_summaries,
        all_source_wait_run_ids,
        all_failed_expected_errors,
        expected_event_count,
    ) = _collect_mixed_valid_invalid_maintenance_expectations(processed_cycles)
    assert len(maintenance_events) == expected_event_count
    assert (
        len(
            {
                (str(event.get("type")), str(event.get("workflow_run_id")))
                for event in maintenance_events
            }
        )
        == expected_event_count
    )
    _assert_mixed_valid_invalid_maintenance_published_events(
        maintenance_events,
        recovery_run_ids=all_recovery_run_ids,
        auto_resume_run_ids=all_auto_resume_run_ids,
        timeout_summaries=all_timeout_summaries,
        source_wait_run_ids=all_source_wait_run_ids,
        failed_expected_errors=all_failed_expected_errors,
    )


def test_worker_tick_drains_older_normal_backlog_while_processing_mixed_valid_invalid_maintenance_load(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_queue(client)
    _enable_runtime_approvals(client)

    _wid, vid = create_and_publish(
        client,
        workflow_name="Normal Queue Backlog Under Mixed Maintenance Worker",
    )
    normal_run_ids: list[str] = []
    for index in range(4):
        created = client.post(
            f"{PREFIX}/workflow-runs",
            json={"workflow_version_id": vid, "input": f"normal mixed backlog {index}"},
        )
        assert created.status_code == 202
        normal_run_ids.append(created.json()["data"]["workflow_run_id"])

    worker = _build_worker(client)
    monkeypatch.setattr(
        "caliber.workflows.runtime._wait_until_ready",
        lambda raw, *, timezone_name="UTC": True,
    )
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        client.app.state.event_bus,
        "publish",
        lambda payload: published.append(dict(payload)),
    )

    processed_cycles = _exercise_mixed_valid_invalid_maintenance_backlog_profile(
        client,
        worker=worker,
        published=published,
        normal_run_ids=normal_run_ids,
        batch_sizes=(2, 4, 6, 8),
        name_prefix="normal-mixed-backlog",
    )

    _assert_mixed_valid_invalid_maintenance_backlog_profile_aggregate(
        published=published,
        processed_cycles=processed_cycles,
    )


def test_worker_tick_sustains_steady_state_mixed_valid_invalid_maintenance_load_while_draining_older_normal_backlog(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_queue(client)
    _enable_runtime_approvals(client)

    _wid, vid = create_and_publish(
        client,
        workflow_name="Steady State Normal Queue Backlog Under Mixed Maintenance Worker",
    )
    normal_run_ids: list[str] = []
    for index in range(6):
        created = client.post(
            f"{PREFIX}/workflow-runs",
            json={"workflow_version_id": vid, "input": f"steady mixed backlog {index}"},
        )
        assert created.status_code == 202
        normal_run_ids.append(created.json()["data"]["workflow_run_id"])

    worker = _build_worker(client)
    monkeypatch.setattr(
        "caliber.workflows.runtime._wait_until_ready",
        lambda raw, *, timezone_name="UTC": True,
    )
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        client.app.state.event_bus,
        "publish",
        lambda payload: published.append(dict(payload)),
    )

    processed_cycles = _exercise_mixed_valid_invalid_maintenance_backlog_profile(
        client,
        worker=worker,
        published=published,
        normal_run_ids=normal_run_ids,
        batch_sizes=(6, 6, 6, 6, 6, 6),
        name_prefix="steady-state-normal-mixed-backlog",
    )

    _assert_mixed_valid_invalid_maintenance_backlog_profile_aggregate(
        published=published,
        processed_cycles=processed_cycles,
    )


def _exercise_rolling_mixed_valid_invalid_maintenance_cycle(
    client,
    *,
    worker,
    published: list[dict[str, object]],
    pending_normal_run_ids: list[str],
    drained_normal_run_ids: list[str],
    vid: str,
    cycle_index: int,
    batch_size: int,
    queued_at_base: datetime,
) -> dict[str, object]:
    arriving = client.post(
        f"{PREFIX}/workflow-runs",
        json={
            "workflow_version_id": vid,
            "input": f"rolling mixed backlog arriving {cycle_index}",
        },
    )
    assert arriving.status_code == 202
    arriving_run_id = arriving.json()["data"]["workflow_run_id"]
    _set_run_queued_at(
        client,
        arriving_run_id,
        queued_at_base + timedelta(seconds=100 + cycle_index),
    )
    pending_normal_run_ids.append(arriving_run_id)

    cycle_suffix = f"rolling-mixed-backlog-cycle-{cycle_index}"
    recovery_run_ids, auto_resume_run_ids, timeout_run_ids = _create_batched_operator_churn_runs(
        client,
        count=batch_size,
        name_suffix=cycle_suffix,
    )
    _prime_batched_operator_churn_states(
        client,
        recovery_run_ids=recovery_run_ids,
        auto_resume_run_ids=auto_resume_run_ids,
        timeout_run_ids=timeout_run_ids,
    )
    invalid_wait_run_id, foreign_wait_run_id, source_wait_run_id = _create_invalid_maintenance_runs(
        client,
        name_suffix=cycle_suffix,
    )
    _prime_invalid_maintenance_states(
        client,
        invalid_wait_run_id=invalid_wait_run_id,
        foreign_wait_run_id=foreign_wait_run_id,
        source_wait_run_id=source_wait_run_id,
        name_suffix=cycle_suffix,
    )

    cycle_start = len(published)
    worker._tick()
    cycle_events = published[cycle_start:]

    timeout_summaries = _assert_batched_operator_churn_db_state(
        client,
        recovery_run_ids=recovery_run_ids,
        auto_resume_run_ids=auto_resume_run_ids,
        timeout_run_ids=timeout_run_ids,
    )
    _assert_invalid_maintenance_runs_fail_closed(
        client,
        invalid_wait_run_id=invalid_wait_run_id,
        foreign_wait_run_id=foreign_wait_run_id,
        source_wait_run_id=source_wait_run_id,
        published=cycle_events,
    )

    maintenance_events = [
        event
        for event in cycle_events
        if event.get("type")
        in {
            "workflow.run.recovered",
            "workflow.run.resumed",
            "workflow.run.expired",
            "workflow.run.failed",
        }
    ]
    assert len(maintenance_events) == (batch_size * 3) + 3

    expected_normal_run_id = pending_normal_run_ids.pop(0)
    drained_normal_run_ids.append(expected_normal_run_id)
    completed_run_ids = {
        str(event.get("workflow_run_id"))
        for event in cycle_events
        if event.get("type") == "workflow.run.completed"
    }
    assert completed_run_ids == {expected_normal_run_id}

    cycle_data = {
        "recovery_run_ids": recovery_run_ids,
        "auto_resume_run_ids": auto_resume_run_ids,
        "timeout_run_ids": timeout_run_ids,
        "timeout_summaries": timeout_summaries,
        "invalid_wait_run_id": invalid_wait_run_id,
        "foreign_wait_run_id": foreign_wait_run_id,
        "source_wait_run_id": source_wait_run_id,
    }
    _assert_mixed_valid_invalid_maintenance_cycle(
        client,
        cycle_data=cycle_data,
        published=maintenance_events,
    )

    with client.app.state.session_factory() as session:
        for completed_run_id in drained_normal_run_ids:
            completed_run = session.get(CaliberWorkflowRun, completed_run_id)
            assert completed_run is not None
            assert completed_run.status == "completed"
        for queued_run_id in pending_normal_run_ids:
            queued_run = session.get(CaliberWorkflowRun, queued_run_id)
            assert queued_run is not None
            assert queued_run.status == "queued"

    return cycle_data


def _assert_pending_normal_backlog_queue_order(
    client,
    *,
    pending_normal_run_ids: list[str],
) -> None:
    assert len(set(pending_normal_run_ids)) == len(pending_normal_run_ids)
    with client.app.state.session_factory() as session:
        queued_runs = []
        for run_id in pending_normal_run_ids:
            queued_run = session.get(CaliberWorkflowRun, run_id)
            assert queued_run is not None
            assert queued_run.status == "queued"
            assert queued_run.queued_at is not None
            queued_runs.append(queued_run)

    queued_order = [
        run.workflow_run_id for run in sorted(queued_runs, key=lambda run: _as_utc(run.queued_at))
    ]
    assert queued_order == pending_normal_run_ids


def _seed_bounded_normal_backlog(
    client,
    *,
    vid: str,
    queued_at_base: datetime,
    steady_backlog_depth: int,
    input_prefix: str,
) -> list[str]:
    pending_normal_run_ids: list[str] = []
    for index in range(steady_backlog_depth):
        created = client.post(
            f"{PREFIX}/workflow-runs",
            json={
                "workflow_version_id": vid,
                "input": f"{input_prefix} seed {index}",
            },
        )
        assert created.status_code == 202
        run_id = created.json()["data"]["workflow_run_id"]
        _set_run_queued_at(client, run_id, queued_at_base + timedelta(seconds=index))
        pending_normal_run_ids.append(run_id)
    return pending_normal_run_ids


def _install_worker_warning_collector(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, dict[str, object]]]:
    warning_calls: list[tuple[str, dict[str, object]]] = []

    def _warning(message: str, *args: object, **kwargs: object) -> None:
        warning_calls.append((message % args if args else message, dict(kwargs)))

    monkeypatch.setattr(workflow_run_worker_module.logger, "warning", _warning)
    return warning_calls


def test_worker_tick_sustains_rolling_normal_backlog_under_steady_state_mixed_valid_invalid_maintenance_load(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_queue(client)
    _enable_runtime_approvals(client)

    _wid, vid = create_and_publish(
        client,
        workflow_name="Rolling Normal Queue Backlog Under Mixed Maintenance Worker",
    )
    pending_normal_run_ids: list[str] = []
    queued_at_base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index in range(4):
        created = client.post(
            f"{PREFIX}/workflow-runs",
            json={"workflow_version_id": vid, "input": f"rolling mixed backlog seed {index}"},
        )
        assert created.status_code == 202
        run_id = created.json()["data"]["workflow_run_id"]
        _set_run_queued_at(client, run_id, queued_at_base + timedelta(seconds=index))
        pending_normal_run_ids.append(run_id)

    worker = _build_worker(client)
    monkeypatch.setattr(
        "caliber.workflows.runtime._wait_until_ready",
        lambda raw, *, timezone_name="UTC": True,
    )
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        client.app.state.event_bus,
        "publish",
        lambda payload: published.append(dict(payload)),
    )

    processed_cycles: list[dict[str, object]] = []
    drained_normal_run_ids: list[str] = []
    batch_size = 5

    for cycle_index in range(1, 7):
        processed_cycles.append(
            _exercise_rolling_mixed_valid_invalid_maintenance_cycle(
                client,
                worker=worker,
                published=published,
                pending_normal_run_ids=pending_normal_run_ids,
                drained_normal_run_ids=drained_normal_run_ids,
                vid=vid,
                cycle_index=cycle_index,
                batch_size=batch_size,
                queued_at_base=queued_at_base,
            )
        )

    _assert_mixed_valid_invalid_maintenance_backlog_profile_aggregate(
        published=published,
        processed_cycles=processed_cycles,
    )

    while pending_normal_run_ids:
        cycle_start = len(published)
        worker._tick()
        cycle_events = published[cycle_start:]
        expected_normal_run_id = pending_normal_run_ids.pop(0)
        drained_normal_run_ids.append(expected_normal_run_id)
        completed_run_ids = {
            str(event.get("workflow_run_id"))
            for event in cycle_events
            if event.get("type") == "workflow.run.completed"
        }
        assert completed_run_ids == {expected_normal_run_id}
        with client.app.state.session_factory() as session:
            completed_run = session.get(CaliberWorkflowRun, expected_normal_run_id)
            assert completed_run is not None
            assert completed_run.status == "completed"
            for queued_run_id in pending_normal_run_ids:
                queued_run = session.get(CaliberWorkflowRun, queued_run_id)
                assert queued_run is not None
                assert queued_run.status == "queued"


def test_worker_tick_sustains_longer_running_rolling_normal_backlog_under_steady_state_mixed_valid_invalid_maintenance_load(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_queue(client)
    _enable_runtime_approvals(client)

    _wid, vid = create_and_publish(
        client,
        workflow_name="Longer Running Rolling Normal Queue Backlog Under Mixed Maintenance Worker",
    )
    pending_normal_run_ids: list[str] = []
    drained_normal_run_ids: list[str] = []
    queued_at_base = datetime(2026, 2, 1, tzinfo=timezone.utc)
    for index in range(5):
        created = client.post(
            f"{PREFIX}/workflow-runs",
            json={"workflow_version_id": vid, "input": f"long rolling mixed backlog seed {index}"},
        )
        assert created.status_code == 202
        run_id = created.json()["data"]["workflow_run_id"]
        _set_run_queued_at(client, run_id, queued_at_base + timedelta(seconds=index))
        pending_normal_run_ids.append(run_id)

    worker = _build_worker(client)
    monkeypatch.setattr(
        "caliber.workflows.runtime._wait_until_ready",
        lambda raw, *, timezone_name="UTC": True,
    )
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        client.app.state.event_bus,
        "publish",
        lambda payload: published.append(dict(payload)),
    )

    processed_cycles: list[dict[str, object]] = []
    for cycle_index in range(1, 11):
        processed_cycles.append(
            _exercise_rolling_mixed_valid_invalid_maintenance_cycle(
                client,
                worker=worker,
                published=published,
                pending_normal_run_ids=pending_normal_run_ids,
                drained_normal_run_ids=drained_normal_run_ids,
                vid=vid,
                cycle_index=cycle_index,
                batch_size=6,
                queued_at_base=queued_at_base,
            )
        )

    _assert_mixed_valid_invalid_maintenance_backlog_profile_aggregate(
        published=published,
        processed_cycles=processed_cycles,
    )
    assert len(drained_normal_run_ids) == 10
    assert len(pending_normal_run_ids) == 5

    with client.app.state.session_factory() as session:
        for completed_run_id in drained_normal_run_ids:
            completed_run = session.get(CaliberWorkflowRun, completed_run_id)
            assert completed_run is not None
            assert completed_run.status == "completed"
        for queued_run_id in pending_normal_run_ids:
            queued_run = session.get(CaliberWorkflowRun, queued_run_id)
            assert queued_run is not None
            assert queued_run.status == "queued"


def _exercise_mixed_maintenance_backlog_publish_failure_profile(
    client,
    *,
    worker,
    normal_run_ids: list[str],
    batch_sizes: tuple[int, ...],
    attempted_types: list[str | None],
    warning_calls: list[tuple[str, dict[str, object]]],
) -> None:
    assert len(normal_run_ids) == len(batch_sizes)

    class _FailingEventBus:
        def publish(self, payload: dict[str, object]) -> None:
            attempted_types.append(payload.get("type") if isinstance(payload, dict) else None)
            raise RuntimeError("event bus offline")

    original_event_bus = worker._event_bus

    for cycle_index, (expected_normal_run_id, batch_size) in enumerate(
        zip(normal_run_ids, batch_sizes, strict=True),
        start=1,
    ):
        cycle_suffix = f"event-bus-mixed-backlog-cycle-{cycle_index}"
        recovery_run_ids, auto_resume_run_ids, timeout_run_ids = (
            _create_batched_operator_churn_runs(
                client,
                count=batch_size,
                name_suffix=cycle_suffix,
            )
        )
        _prime_batched_operator_churn_states(
            client,
            recovery_run_ids=recovery_run_ids,
            auto_resume_run_ids=auto_resume_run_ids,
            timeout_run_ids=timeout_run_ids,
        )
        invalid_wait_run_id, foreign_wait_run_id, source_wait_run_id = (
            _create_invalid_maintenance_runs(
                client,
                name_suffix=cycle_suffix,
            )
        )
        _prime_invalid_maintenance_states(
            client,
            invalid_wait_run_id=invalid_wait_run_id,
            foreign_wait_run_id=foreign_wait_run_id,
            source_wait_run_id=source_wait_run_id,
            name_suffix=cycle_suffix,
        )

        worker._event_bus = _FailingEventBus()
        worker._tick()
        worker._event_bus = original_event_bus
        _assert_batched_operator_churn_db_state(
            client,
            recovery_run_ids=recovery_run_ids,
            auto_resume_run_ids=auto_resume_run_ids,
            timeout_run_ids=timeout_run_ids,
        )

        with client.app.state.session_factory() as session:
            invalid_wait_run = session.get(CaliberWorkflowRun, invalid_wait_run_id)
            foreign_wait_run = session.get(CaliberWorkflowRun, foreign_wait_run_id)
            source_wait_run = session.get(CaliberWorkflowRun, source_wait_run_id)
            completed_run = session.get(CaliberWorkflowRun, expected_normal_run_id)
            assert invalid_wait_run is not None
            assert foreign_wait_run is not None
            assert source_wait_run is not None
            assert completed_run is not None
            assert invalid_wait_run.status == "failed"
            assert invalid_wait_run.error_code == "resume_checkpoint_unavailable"
            assert foreign_wait_run.status == "failed"
            assert foreign_wait_run.error_code == "resume_checkpoint_unavailable"
            assert source_wait_run.status == "expired"
            assert source_wait_run.error_code == "wait_for_event_timeout"
            assert completed_run.status == "completed"

            for prior_completed_run_id in normal_run_ids[:cycle_index]:
                prior_completed_run = session.get(CaliberWorkflowRun, prior_completed_run_id)
                assert prior_completed_run is not None
                assert prior_completed_run.status == "completed"
            for queued_run_id in normal_run_ids[cycle_index:]:
                queued_run = session.get(CaliberWorkflowRun, queued_run_id)
                assert queued_run is not None
                assert queued_run.status == "queued"

    expected_recovered = sum(batch_sizes)
    expected_resumed = sum(batch_sizes)
    expected_expired = sum(batch_sizes) + len(batch_sizes)
    expected_failed = len(batch_sizes) * 2
    expected_started = len(batch_sizes)
    expected_completed = len(batch_sizes)

    assert attempted_types.count("workflow.run.recovered") == expected_recovered
    assert attempted_types.count("workflow.run.resumed") == expected_resumed
    assert attempted_types.count("workflow.run.expired") == expected_expired
    assert attempted_types.count("workflow.run.failed") == expected_failed
    assert attempted_types.count("workflow.run.started") == expected_started
    assert attempted_types.count("workflow.run.completed") == expected_completed
    assert len(warning_calls) == len(attempted_types)
    assert warning_calls
    assert all(
        message.startswith("failed to publish workflow-run event type=")
        and kwargs == {"exc_info": True}
        for message, kwargs in warning_calls
    )


def _exercise_rolling_mixed_maintenance_backlog_publish_failure_profile(  # noqa: PLR0915
    client,
    *,
    worker,
    pending_normal_run_ids: list[str],
    drained_normal_run_ids: list[str],
    vid: str,
    queued_at_base: datetime,
    cycle_count: int,
    batch_size: int,
    attempted_types: list[str | None],
    warning_calls: list[tuple[str, dict[str, object]]],
    expected_pending_depth: int | None = None,
    drain_remaining_backlog: bool = True,
) -> None:
    class _FailingEventBus:
        def publish(self, payload: dict[str, object]) -> None:
            attempted_types.append(payload.get("type") if isinstance(payload, dict) else None)
            raise RuntimeError("event bus offline")

    original_event_bus = worker._event_bus

    for cycle_index in range(1, cycle_count + 1):
        arriving = client.post(
            f"{PREFIX}/workflow-runs",
            json={
                "workflow_version_id": vid,
                "input": f"rolling event-bus mixed backlog arriving {cycle_index}",
            },
        )
        assert arriving.status_code == 202
        arriving_run_id = arriving.json()["data"]["workflow_run_id"]
        _set_run_queued_at(
            client,
            arriving_run_id,
            queued_at_base + timedelta(seconds=100 + cycle_index),
        )
        pending_normal_run_ids.append(arriving_run_id)

        cycle_suffix = f"event-bus-rolling-mixed-backlog-cycle-{cycle_index}"
        recovery_run_ids, auto_resume_run_ids, timeout_run_ids = (
            _create_batched_operator_churn_runs(
                client,
                count=batch_size,
                name_suffix=cycle_suffix,
            )
        )
        _prime_batched_operator_churn_states(
            client,
            recovery_run_ids=recovery_run_ids,
            auto_resume_run_ids=auto_resume_run_ids,
            timeout_run_ids=timeout_run_ids,
        )
        invalid_wait_run_id, foreign_wait_run_id, source_wait_run_id = (
            _create_invalid_maintenance_runs(
                client,
                name_suffix=cycle_suffix,
            )
        )
        _prime_invalid_maintenance_states(
            client,
            invalid_wait_run_id=invalid_wait_run_id,
            foreign_wait_run_id=foreign_wait_run_id,
            source_wait_run_id=source_wait_run_id,
            name_suffix=cycle_suffix,
        )

        worker._event_bus = _FailingEventBus()
        worker._tick()
        worker._event_bus = original_event_bus

        _assert_batched_operator_churn_db_state(
            client,
            recovery_run_ids=recovery_run_ids,
            auto_resume_run_ids=auto_resume_run_ids,
            timeout_run_ids=timeout_run_ids,
        )

        with client.app.state.session_factory() as session:
            invalid_wait_run = session.get(CaliberWorkflowRun, invalid_wait_run_id)
            foreign_wait_run = session.get(CaliberWorkflowRun, foreign_wait_run_id)
            source_wait_run = session.get(CaliberWorkflowRun, source_wait_run_id)
            assert invalid_wait_run is not None
            assert foreign_wait_run is not None
            assert source_wait_run is not None
            assert invalid_wait_run.status == "failed"
            assert invalid_wait_run.error_code == "resume_checkpoint_unavailable"
            assert "invalid resume_at" in str(invalid_wait_run.error_summary)
            assert foreign_wait_run.status == "failed"
            assert foreign_wait_run.error_code == "resume_checkpoint_unavailable"
            assert "foreign checkpoint" in str(foreign_wait_run.error_summary)
            assert source_wait_run.status == "expired"
            assert source_wait_run.error_code == "wait_for_event_timeout"

        expected_normal_run_id = pending_normal_run_ids.pop(0)
        drained_normal_run_ids.append(expected_normal_run_id)

        with client.app.state.session_factory() as session:
            completed_run = session.get(CaliberWorkflowRun, expected_normal_run_id)
            assert completed_run is not None
            assert completed_run.status == "completed"
            for completed_run_id in drained_normal_run_ids:
                prior_completed_run = session.get(CaliberWorkflowRun, completed_run_id)
                assert prior_completed_run is not None
                assert prior_completed_run.status == "completed"
            for queued_run_id in pending_normal_run_ids:
                queued_run = session.get(CaliberWorkflowRun, queued_run_id)
                assert queued_run is not None
                assert queued_run.status == "queued"
        if expected_pending_depth is not None:
            assert len(pending_normal_run_ids) == expected_pending_depth
            _assert_pending_normal_backlog_queue_order(
                client,
                pending_normal_run_ids=pending_normal_run_ids,
            )

    if drain_remaining_backlog:
        while pending_normal_run_ids:
            worker._event_bus = _FailingEventBus()
            worker._tick()
            worker._event_bus = original_event_bus

            expected_normal_run_id = pending_normal_run_ids.pop(0)
            drained_normal_run_ids.append(expected_normal_run_id)

            with client.app.state.session_factory() as session:
                completed_run = session.get(CaliberWorkflowRun, expected_normal_run_id)
                assert completed_run is not None
                assert completed_run.status == "completed"
                for queued_run_id in pending_normal_run_ids:
                    queued_run = session.get(CaliberWorkflowRun, queued_run_id)
                    assert queued_run is not None
                    assert queued_run.status == "queued"

    expected_recovered = cycle_count * batch_size
    expected_resumed = cycle_count * batch_size
    expected_expired = cycle_count * (batch_size + 1)
    expected_failed = cycle_count * 2
    expected_started = len(drained_normal_run_ids)
    expected_completed = len(drained_normal_run_ids)

    assert attempted_types.count("workflow.run.recovered") == expected_recovered
    assert attempted_types.count("workflow.run.resumed") == expected_resumed
    assert attempted_types.count("workflow.run.expired") == expected_expired
    assert attempted_types.count("workflow.run.failed") == expected_failed
    assert attempted_types.count("workflow.run.started") == expected_started
    assert attempted_types.count("workflow.run.completed") == expected_completed
    assert len(warning_calls) == len(attempted_types)
    assert warning_calls
    assert all(
        message.startswith("failed to publish workflow-run event type=")
        and kwargs == {"exc_info": True}
        for message, kwargs in warning_calls
    )


def test_worker_tick_keeps_steady_state_rolling_normal_backlog_depth_and_fifo_order_bounded_when_event_bus_publish_raises(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_queue(client)
    _enable_runtime_approvals(client)

    _wid, vid = create_and_publish(
        client,
        workflow_name="Bounded Steady State Event Bus Failure Rolling Normal Queue Backlog Worker",
    )
    queued_at_base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    steady_backlog_depth = 6
    pending_normal_run_ids = _seed_bounded_normal_backlog(
        client,
        vid=vid,
        queued_at_base=queued_at_base,
        steady_backlog_depth=steady_backlog_depth,
        input_prefix="bounded rolling event-bus mixed backlog",
    )
    drained_normal_run_ids: list[str] = []

    attempted_types: list[str | None] = []
    warning_calls = _install_worker_warning_collector(monkeypatch)

    worker = _build_worker(client)
    monkeypatch.setattr(
        "caliber.workflows.runtime._wait_until_ready",
        lambda raw, *, timezone_name="UTC": True,
    )

    _exercise_rolling_mixed_maintenance_backlog_publish_failure_profile(
        client,
        worker=worker,
        pending_normal_run_ids=pending_normal_run_ids,
        drained_normal_run_ids=drained_normal_run_ids,
        vid=vid,
        queued_at_base=queued_at_base,
        cycle_count=15,
        batch_size=8,
        attempted_types=attempted_types,
        warning_calls=warning_calls,
        expected_pending_depth=steady_backlog_depth,
        drain_remaining_backlog=False,
    )

    assert len(drained_normal_run_ids) == 15
    assert len(pending_normal_run_ids) == steady_backlog_depth
    _assert_pending_normal_backlog_queue_order(
        client,
        pending_normal_run_ids=pending_normal_run_ids,
    )


def test_worker_tick_keeps_steady_state_rolling_normal_backlog_depth_and_fifo_order_bounded_under_mixed_maintenance_load(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_queue(client)
    _enable_runtime_approvals(client)

    _wid, vid = create_and_publish(
        client,
        workflow_name="Bounded Steady State Rolling Normal Queue Backlog Under Mixed Maintenance Worker",
    )
    pending_normal_run_ids: list[str] = []
    drained_normal_run_ids: list[str] = []
    queued_at_base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    steady_backlog_depth = 6
    for index in range(steady_backlog_depth):
        created = client.post(
            f"{PREFIX}/workflow-runs",
            json={
                "workflow_version_id": vid,
                "input": f"bounded rolling mixed backlog seed {index}",
            },
        )
        assert created.status_code == 202
        run_id = created.json()["data"]["workflow_run_id"]
        _set_run_queued_at(client, run_id, queued_at_base + timedelta(seconds=index))
        pending_normal_run_ids.append(run_id)

    worker = _build_worker(client)
    monkeypatch.setattr(
        "caliber.workflows.runtime._wait_until_ready",
        lambda raw, *, timezone_name="UTC": True,
    )
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        client.app.state.event_bus,
        "publish",
        lambda payload: published.append(dict(payload)),
    )

    processed_cycles: list[dict[str, object]] = []
    for cycle_index in range(1, 16):
        processed_cycles.append(
            _exercise_rolling_mixed_valid_invalid_maintenance_cycle(
                client,
                worker=worker,
                published=published,
                pending_normal_run_ids=pending_normal_run_ids,
                drained_normal_run_ids=drained_normal_run_ids,
                vid=vid,
                cycle_index=cycle_index,
                batch_size=8,
                queued_at_base=queued_at_base,
            )
        )
        assert len(pending_normal_run_ids) == steady_backlog_depth
        _assert_pending_normal_backlog_queue_order(
            client,
            pending_normal_run_ids=pending_normal_run_ids,
        )

    _assert_mixed_valid_invalid_maintenance_backlog_profile_aggregate(
        published=published,
        processed_cycles=processed_cycles,
    )
    assert len(drained_normal_run_ids) == 15
    assert len(pending_normal_run_ids) == steady_backlog_depth
    _assert_pending_normal_backlog_queue_order(
        client,
        pending_normal_run_ids=pending_normal_run_ids,
    )


def test_worker_tick_persists_mixed_maintenance_and_backlog_progress_when_event_bus_publish_raises(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_queue(client)
    _enable_runtime_approvals(client)

    _wid, vid = create_and_publish(
        client,
        workflow_name="Event Bus Failure Mixed Maintenance Backlog Worker",
    )
    normal_run_ids: list[str] = []
    for index in range(3):
        created = client.post(
            f"{PREFIX}/workflow-runs",
            json={"workflow_version_id": vid, "input": f"event-bus mixed backlog {index}"},
        )
        assert created.status_code == 202
        normal_run_ids.append(created.json()["data"]["workflow_run_id"])

    attempted_types: list[str | None] = []
    warning_calls: list[tuple[str, dict[str, object]]] = []

    def _warning(message: str, *args: object, **kwargs: object) -> None:
        warning_calls.append((message % args if args else message, dict(kwargs)))

    monkeypatch.setattr(workflow_run_worker_module.logger, "warning", _warning)

    worker = _build_worker(client)
    original_event_bus = worker._event_bus
    monkeypatch.setattr(
        "caliber.workflows.runtime._wait_until_ready",
        lambda raw, *, timezone_name="UTC": True,
    )

    _exercise_mixed_maintenance_backlog_publish_failure_profile(
        client,
        worker=worker,
        normal_run_ids=normal_run_ids,
        batch_sizes=(2, 2, 2),
        attempted_types=attempted_types,
        warning_calls=warning_calls,
    )


def test_worker_tick_sustains_steady_state_mixed_maintenance_backlog_progress_when_event_bus_publish_raises(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_queue(client)
    _enable_runtime_approvals(client)

    _wid, vid = create_and_publish(
        client,
        workflow_name="Steady State Event Bus Failure Mixed Maintenance Backlog Worker",
    )
    normal_run_ids: list[str] = []
    for index in range(5):
        created = client.post(
            f"{PREFIX}/workflow-runs",
            json={"workflow_version_id": vid, "input": f"steady event-bus mixed backlog {index}"},
        )
        assert created.status_code == 202
        normal_run_ids.append(created.json()["data"]["workflow_run_id"])

    attempted_types: list[str | None] = []
    warning_calls: list[tuple[str, dict[str, object]]] = []

    def _warning(message: str, *args: object, **kwargs: object) -> None:
        warning_calls.append((message % args if args else message, dict(kwargs)))

    monkeypatch.setattr(workflow_run_worker_module.logger, "warning", _warning)

    worker = _build_worker(client)
    monkeypatch.setattr(
        "caliber.workflows.runtime._wait_until_ready",
        lambda raw, *, timezone_name="UTC": True,
    )

    _exercise_mixed_maintenance_backlog_publish_failure_profile(
        client,
        worker=worker,
        normal_run_ids=normal_run_ids,
        batch_sizes=(4, 4, 4, 4, 4),
        attempted_types=attempted_types,
        warning_calls=warning_calls,
    )


def test_worker_tick_sustains_rolling_mixed_maintenance_backlog_progress_when_event_bus_publish_raises(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_queue(client)
    _enable_runtime_approvals(client)

    _wid, vid = create_and_publish(
        client,
        workflow_name="Rolling Event Bus Failure Mixed Maintenance Backlog Worker",
    )
    pending_normal_run_ids: list[str] = []
    drained_normal_run_ids: list[str] = []
    queued_at_base = datetime(2026, 3, 1, tzinfo=timezone.utc)
    for index in range(4):
        created = client.post(
            f"{PREFIX}/workflow-runs",
            json={
                "workflow_version_id": vid,
                "input": f"rolling event-bus mixed backlog seed {index}",
            },
        )
        assert created.status_code == 202
        run_id = created.json()["data"]["workflow_run_id"]
        _set_run_queued_at(client, run_id, queued_at_base + timedelta(seconds=index))
        pending_normal_run_ids.append(run_id)

    attempted_types: list[str | None] = []
    warning_calls: list[tuple[str, dict[str, object]]] = []

    def _warning(message: str, *args: object, **kwargs: object) -> None:
        warning_calls.append((message % args if args else message, dict(kwargs)))

    monkeypatch.setattr(workflow_run_worker_module.logger, "warning", _warning)

    worker = _build_worker(client)
    monkeypatch.setattr(
        "caliber.workflows.runtime._wait_until_ready",
        lambda raw, *, timezone_name="UTC": True,
    )

    _exercise_rolling_mixed_maintenance_backlog_publish_failure_profile(
        client,
        worker=worker,
        pending_normal_run_ids=pending_normal_run_ids,
        drained_normal_run_ids=drained_normal_run_ids,
        vid=vid,
        queued_at_base=queued_at_base,
        cycle_count=6,
        batch_size=4,
        attempted_types=attempted_types,
        warning_calls=warning_calls,
    )

    assert len(drained_normal_run_ids) == 10
    assert not pending_normal_run_ids


def test_worker_tick_sustains_longer_running_rolling_mixed_maintenance_backlog_progress_when_event_bus_publish_raises(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_queue(client)
    _enable_runtime_approvals(client)

    _wid, vid = create_and_publish(
        client,
        workflow_name="Longer Running Rolling Event Bus Failure Mixed Maintenance Backlog Worker",
    )
    pending_normal_run_ids: list[str] = []
    drained_normal_run_ids: list[str] = []
    queued_at_base = datetime(2026, 4, 1, tzinfo=timezone.utc)
    for index in range(5):
        created = client.post(
            f"{PREFIX}/workflow-runs",
            json={
                "workflow_version_id": vid,
                "input": f"long rolling event-bus mixed backlog seed {index}",
            },
        )
        assert created.status_code == 202
        run_id = created.json()["data"]["workflow_run_id"]
        _set_run_queued_at(client, run_id, queued_at_base + timedelta(seconds=index))
        pending_normal_run_ids.append(run_id)

    attempted_types: list[str | None] = []
    warning_calls: list[tuple[str, dict[str, object]]] = []

    def _warning(message: str, *args: object, **kwargs: object) -> None:
        warning_calls.append((message % args if args else message, dict(kwargs)))

    monkeypatch.setattr(workflow_run_worker_module.logger, "warning", _warning)

    worker = _build_worker(client)
    monkeypatch.setattr(
        "caliber.workflows.runtime._wait_until_ready",
        lambda raw, *, timezone_name="UTC": True,
    )

    _exercise_rolling_mixed_maintenance_backlog_publish_failure_profile(
        client,
        worker=worker,
        pending_normal_run_ids=pending_normal_run_ids,
        drained_normal_run_ids=drained_normal_run_ids,
        vid=vid,
        queued_at_base=queued_at_base,
        cycle_count=10,
        batch_size=6,
        attempted_types=attempted_types,
        warning_calls=warning_calls,
    )

    assert len(drained_normal_run_ids) == 15
    assert not pending_normal_run_ids


def _create_invalid_maintenance_runs(
    client,
    *,
    name_suffix: str = "",
) -> tuple[str, str, str]:
    workflow_suffix = f" {name_suffix}" if name_suffix else ""
    manifest_suffix = f"-{name_suffix.replace(' ', '-')}" if name_suffix else ""
    invalid_wait_until_manifest = _wait_until_manifest(
        f"wait-until-invalid-maintenance{manifest_suffix}-worker-wf",
        wait_until="2099-01-01T00:00:00",
        timezone_name="UTC",
    )
    _invalid_wait_wid, invalid_wait_vid = create_and_publish(
        client,
        workflow_name=f"Wait Until Invalid Maintenance Worker{workflow_suffix}",
        manifest=invalid_wait_until_manifest,
    )
    foreign_wait_manifest = _wait_event_manifest(
        f"wait-event-foreign-maintenance{manifest_suffix}-worker-wf",
        timeout_seconds=30,
    )
    _foreign_wait_wid, foreign_wait_vid = create_and_publish(
        client,
        workflow_name=f"Wait Event Foreign Maintenance Worker{workflow_suffix}",
        manifest=foreign_wait_manifest,
    )
    source_wait_manifest = _wait_event_manifest(
        f"wait-event-source-maintenance{manifest_suffix}-worker-wf",
        timeout_seconds=30,
    )
    _source_wait_wid, source_wait_vid = create_and_publish(
        client,
        workflow_name=f"Wait Event Source Maintenance Worker{workflow_suffix}",
        manifest=source_wait_manifest,
    )

    invalid_wait_created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": invalid_wait_vid, "input": "stay blocked"},
    )
    foreign_wait_created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": foreign_wait_vid, "input": "stay foreign"},
    )
    source_wait_created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": source_wait_vid, "input": "source checkpoint"},
    )
    assert invalid_wait_created.status_code == 202
    assert foreign_wait_created.status_code == 202
    assert source_wait_created.status_code == 202
    return (
        invalid_wait_created.json()["data"]["workflow_run_id"],
        foreign_wait_created.json()["data"]["workflow_run_id"],
        source_wait_created.json()["data"]["workflow_run_id"],
    )


def _create_missing_checkpoint_maintenance_run(client) -> str:
    missing_wait_manifest = _wait_event_manifest(
        "wait-event-missing-maintenance-worker-wf",
        timeout_seconds=30,
    )
    _missing_wait_wid, missing_wait_vid = create_and_publish(
        client,
        workflow_name="Wait Event Missing Maintenance Worker",
        manifest=missing_wait_manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": missing_wait_vid, "input": "missing checkpoint"},
    )
    assert created.status_code == 202
    return created.json()["data"]["workflow_run_id"]


def _create_corrupt_checkpoint_maintenance_run(client) -> str:
    corrupt_wait_manifest = _wait_event_manifest(
        "wait-event-corrupt-maintenance-worker-wf",
        timeout_seconds=30,
    )
    _corrupt_wait_wid, corrupt_wait_vid = create_and_publish(
        client,
        workflow_name="Wait Event Corrupt Maintenance Worker",
        manifest=corrupt_wait_manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": corrupt_wait_vid, "input": "corrupt checkpoint"},
    )
    assert created.status_code == 202
    return created.json()["data"]["workflow_run_id"]


def _prime_invalid_maintenance_states(
    client,
    *,
    invalid_wait_run_id: str,
    foreign_wait_run_id: str,
    source_wait_run_id: str,
    name_suffix: str = "",
) -> None:
    checkpoint_suffix = f"-{name_suffix.replace(' ', '-')}" if name_suffix else ""
    source_checkpoint_id = f"WRC-source-maintenance{checkpoint_suffix}"
    invalid_checkpoint_id = f"WRC-invalid-maintenance{checkpoint_suffix}"
    with client.app.state.session_factory() as session:
        invalid_wait_run = session.get(CaliberWorkflowRun, invalid_wait_run_id)
        foreign_wait_run = session.get(CaliberWorkflowRun, foreign_wait_run_id)
        source_wait_run = session.get(CaliberWorkflowRun, source_wait_run_id)
        assert invalid_wait_run is not None
        assert foreign_wait_run is not None
        assert source_wait_run is not None

        _seed_waiting_checkpoint(
            session,
            run=source_wait_run,
            checkpoint_id=source_checkpoint_id,
            node_id="wait_gate",
            state_blob={
                "kind": "wait_for_event",
                "node_id": "wait_gate",
                "expected_event_name": "ticket.approved",
                "timeout_seconds": 30.0,
                "input_by_port": {"input": "source checkpoint"},
            },
            created_at=datetime.now(timezone.utc) - timedelta(seconds=45),
        )

        invalid_wait_run.summary = {
            **dict(invalid_wait_run.summary or {}),
            "status": "waiting_event",
            "resume_checkpoint_id": invalid_checkpoint_id,
            "resume_checkpoint_run_id": invalid_wait_run.workflow_run_id,
        }
        invalid_wait_run.status = "waiting_event"
        invalid_wait_run.current_node_id = "wait_gate"
        invalid_wait_run.error_code = "waiting_event"
        invalid_wait_run.error_summary = "waiting for resume event"
        session.add(
            CaliberWorkflowRunCheckpoint(
                checkpoint_id=invalid_checkpoint_id,
                workflow_run_id=invalid_wait_run.workflow_run_id,
                project_id=invalid_wait_run.project_id,
                sequence=1,
                node_id="wait_gate",
                state_blob={
                    "kind": "wait_until",
                    "node_id": "wait_gate",
                    "resume_at": "not-a-date",
                },
            )
        )

        foreign_wait_run.summary = {
            **dict(foreign_wait_run.summary or {}),
            "status": "waiting_event",
            "resume_checkpoint_id": source_checkpoint_id,
            "resume_checkpoint_run_id": source_wait_run.workflow_run_id,
        }
        foreign_wait_run.status = "waiting_event"
        foreign_wait_run.current_node_id = "wait_gate"
        foreign_wait_run.error_code = "waiting_event"
        foreign_wait_run.error_summary = "waiting for resume event"
        session.commit()


def _assert_invalid_maintenance_runs_fail_closed(
    client,
    *,
    invalid_wait_run_id: str,
    foreign_wait_run_id: str,
    source_wait_run_id: str,
    published: list[dict[str, object]],
) -> None:
    with client.app.state.session_factory() as session:
        invalid_wait_run = session.get(CaliberWorkflowRun, invalid_wait_run_id)
        foreign_wait_run = session.get(CaliberWorkflowRun, foreign_wait_run_id)
        source_wait_run = session.get(CaliberWorkflowRun, source_wait_run_id)
        assert invalid_wait_run is not None
        assert foreign_wait_run is not None
        assert source_wait_run is not None

        assert invalid_wait_run.status == "failed"
        assert invalid_wait_run.error_code == "resume_checkpoint_unavailable"
        assert "invalid resume_at" in str(invalid_wait_run.error_summary)
        assert foreign_wait_run.status == "failed"
        assert foreign_wait_run.error_code == "resume_checkpoint_unavailable"
        assert "foreign checkpoint" in str(foreign_wait_run.error_summary)
        assert source_wait_run.status == "expired"
        assert source_wait_run.error_code == "wait_for_event_timeout"

        invalid_events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == invalid_wait_run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        foreign_events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == foreign_wait_run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        source_events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == source_wait_run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert not any(
            event.event_type in {"workflow.run.resumed", "workflow.run.expired"}
            for event in invalid_events
        )
        assert not any(
            event.event_type in {"workflow.run.resumed", "workflow.run.expired"}
            for event in foreign_events
        )
        assert any(event.event_type == "workflow.run.failed" for event in invalid_events)
        assert any(event.event_type == "workflow.run.failed" for event in foreign_events)
        assert any(event.event_type == "workflow.run.expired" for event in source_events)

    assert not any(
        event.get("workflow_run_id") == invalid_wait_run_id
        and event.get("type") in {"workflow.run.resumed", "workflow.run.expired"}
        for event in published
    )
    assert not any(
        event.get("workflow_run_id") == foreign_wait_run_id
        and event.get("type") in {"workflow.run.resumed", "workflow.run.expired"}
        for event in published
    )
    assert not any(
        event.get("workflow_run_id") == source_wait_run_id
        and event.get("type") in {"workflow.run.resumed", "workflow.run.failed"}
        for event in published
    )
    assert any(
        event.get("workflow_run_id") == invalid_wait_run_id
        and event.get("type") == "workflow.run.failed"
        and "invalid resume_at" in str(event.get("error"))
        for event in published
    )
    assert any(
        event.get("workflow_run_id") == foreign_wait_run_id
        and event.get("type") == "workflow.run.failed"
        and "foreign checkpoint" in str(event.get("error"))
        for event in published
    )
    assert any(
        event.get("workflow_run_id") == source_wait_run_id
        and event.get("type") == "workflow.run.expired"
        for event in published
    )


def _assert_invalid_maintenance_state(
    client,
    *,
    invalid_wait_run_id: str,
    foreign_wait_run_id: str,
    source_wait_run_id: str,
    auto_resume_run_id: str,
    timeout_run_id: str,
    published: list[dict[str, object]],
) -> None:
    _assert_invalid_maintenance_runs_fail_closed(
        client,
        invalid_wait_run_id=invalid_wait_run_id,
        foreign_wait_run_id=foreign_wait_run_id,
        source_wait_run_id=source_wait_run_id,
        published=published,
    )

    with client.app.state.session_factory() as session:
        auto_resume_run = session.get(CaliberWorkflowRun, auto_resume_run_id)
        timeout_run = session.get(CaliberWorkflowRun, timeout_run_id)
        assert auto_resume_run is not None
        assert timeout_run is not None
        assert auto_resume_run.status == "queued"
        assert timeout_run.status == "expired"

    published_types = [event.get("type") for event in published]
    assert published_types.count("workflow.run.resumed") == 1
    assert published_types.count("workflow.run.expired") == 2
    assert published_types.count("workflow.run.failed") == 2


def test_worker_tick_sustains_multicycle_mixed_valid_and_invalid_maintenance_without_stale_events(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime_approvals(client)

    worker = _build_worker(client)
    monkeypatch.setattr(worker, "_claim_next_run", lambda: None)
    monkeypatch.setattr(
        "caliber.workflows.runtime._wait_until_ready",
        lambda raw, *, timezone_name="UTC": True,
    )
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        client.app.state.event_bus,
        "publish",
        lambda payload: published.append(dict(payload)),
    )

    processed_cycles: list[dict[str, object]] = []
    cycle_suffixes = ("mixed-cycle-1", "mixed-cycle-2", "mixed-cycle-3")
    for cycle_suffix in cycle_suffixes:
        recovery_run_ids, auto_resume_run_ids, timeout_run_ids = (
            _create_batched_operator_churn_runs(
                client,
                count=1,
                name_suffix=cycle_suffix,
            )
        )
        _prime_batched_operator_churn_states(
            client,
            recovery_run_ids=recovery_run_ids,
            auto_resume_run_ids=auto_resume_run_ids,
            timeout_run_ids=timeout_run_ids,
        )
        invalid_wait_run_id, foreign_wait_run_id, source_wait_run_id = (
            _create_invalid_maintenance_runs(
                client,
                name_suffix=cycle_suffix,
            )
        )
        _prime_invalid_maintenance_states(
            client,
            invalid_wait_run_id=invalid_wait_run_id,
            foreign_wait_run_id=foreign_wait_run_id,
            source_wait_run_id=source_wait_run_id,
            name_suffix=cycle_suffix,
        )

        cycle_start = len(published)
        worker._tick()
        cycle_events = published[cycle_start:]

        _assert_batched_operator_churn_db_state(
            client,
            recovery_run_ids=recovery_run_ids,
            auto_resume_run_ids=auto_resume_run_ids,
            timeout_run_ids=timeout_run_ids,
        )
        _assert_invalid_maintenance_runs_fail_closed(
            client,
            invalid_wait_run_id=invalid_wait_run_id,
            foreign_wait_run_id=foreign_wait_run_id,
            source_wait_run_id=source_wait_run_id,
            published=cycle_events,
        )
        cycle_types = [event.get("type") for event in cycle_events]
        assert cycle_types.count("workflow.run.recovered") == 1
        assert cycle_types.count("workflow.run.resumed") == 1
        assert cycle_types.count("workflow.run.expired") == 2
        assert cycle_types.count("workflow.run.failed") == 2
        assert len(cycle_events) == 6
        assert any(
            event.get("type") == "workflow.run.recovered"
            and event.get("workflow_run_id") == recovery_run_ids[0]
            and event.get("reason") == "lease_expired"
            for event in cycle_events
        )

        worker._tick()
        assert len(published) == cycle_start + len(cycle_events)

        processed_cycles.append(
            {
                "recovery_run_id": recovery_run_ids[0],
                "auto_resume_run_id": auto_resume_run_ids[0],
                "timeout_run_id": timeout_run_ids[0],
                "invalid_wait_run_id": invalid_wait_run_id,
                "foreign_wait_run_id": foreign_wait_run_id,
                "source_wait_run_id": source_wait_run_id,
            }
        )

    maintenance_types = {
        "workflow.run.recovered",
        "workflow.run.resumed",
        "workflow.run.expired",
        "workflow.run.failed",
    }
    maintenance_events = [event for event in published if event.get("type") in maintenance_types]
    assert len(maintenance_events) == len(cycle_suffixes) * 6

    for cycle in processed_cycles:
        cycle_run_ids = {
            str(cycle["recovery_run_id"]),
            str(cycle["auto_resume_run_id"]),
            str(cycle["timeout_run_id"]),
            str(cycle["invalid_wait_run_id"]),
            str(cycle["foreign_wait_run_id"]),
            str(cycle["source_wait_run_id"]),
        }
        cycle_events = [
            event for event in maintenance_events if event.get("workflow_run_id") in cycle_run_ids
        ]
        assert len(cycle_events) == 6
        _assert_invalid_maintenance_state(
            client,
            invalid_wait_run_id=str(cycle["invalid_wait_run_id"]),
            foreign_wait_run_id=str(cycle["foreign_wait_run_id"]),
            source_wait_run_id=str(cycle["source_wait_run_id"]),
            auto_resume_run_id=str(cycle["auto_resume_run_id"]),
            timeout_run_id=str(cycle["timeout_run_id"]),
            published=cycle_events,
        )


def _exercise_mixed_valid_invalid_maintenance_load_profile(
    client,
    *,
    worker,
    published: list[dict[str, object]],
    batch_sizes: tuple[int, ...],
    name_prefix: str,
) -> list[dict[str, object]]:
    processed_cycles: list[dict[str, object]] = []
    for cycle_index, batch_size in enumerate(batch_sizes, start=1):
        cycle_suffix = f"{name_prefix}-cycle-{cycle_index}"
        recovery_run_ids, auto_resume_run_ids, timeout_run_ids = (
            _create_batched_operator_churn_runs(
                client,
                count=batch_size,
                name_suffix=cycle_suffix,
            )
        )
        _prime_batched_operator_churn_states(
            client,
            recovery_run_ids=recovery_run_ids,
            auto_resume_run_ids=auto_resume_run_ids,
            timeout_run_ids=timeout_run_ids,
        )
        invalid_wait_run_id, foreign_wait_run_id, source_wait_run_id = (
            _create_invalid_maintenance_runs(
                client,
                name_suffix=cycle_suffix,
            )
        )
        _prime_invalid_maintenance_states(
            client,
            invalid_wait_run_id=invalid_wait_run_id,
            foreign_wait_run_id=foreign_wait_run_id,
            source_wait_run_id=source_wait_run_id,
            name_suffix=cycle_suffix,
        )

        cycle_start = len(published)
        worker._tick()
        cycle_events = published[cycle_start:]

        timeout_summaries = _assert_batched_operator_churn_db_state(
            client,
            recovery_run_ids=recovery_run_ids,
            auto_resume_run_ids=auto_resume_run_ids,
            timeout_run_ids=timeout_run_ids,
        )
        _assert_invalid_maintenance_runs_fail_closed(
            client,
            invalid_wait_run_id=invalid_wait_run_id,
            foreign_wait_run_id=foreign_wait_run_id,
            source_wait_run_id=source_wait_run_id,
            published=cycle_events,
        )
        cycle_types = [event.get("type") for event in cycle_events]
        assert cycle_types.count("workflow.run.recovered") == batch_size
        assert cycle_types.count("workflow.run.resumed") == batch_size
        assert cycle_types.count("workflow.run.expired") == batch_size + 1
        assert cycle_types.count("workflow.run.failed") == 2
        assert len(cycle_events) == (batch_size * 3) + 3

        worker._tick()
        assert len(published) == cycle_start + len(cycle_events)

        cycle_data = {
            "recovery_run_ids": recovery_run_ids,
            "auto_resume_run_ids": auto_resume_run_ids,
            "timeout_run_ids": timeout_run_ids,
            "timeout_summaries": timeout_summaries,
            "invalid_wait_run_id": invalid_wait_run_id,
            "foreign_wait_run_id": foreign_wait_run_id,
            "source_wait_run_id": source_wait_run_id,
        }
        processed_cycles.append(cycle_data)
        _assert_mixed_valid_invalid_maintenance_cycle(
            client,
            cycle_data=cycle_data,
            published=cycle_events,
        )

    return processed_cycles


def _assert_mixed_valid_invalid_maintenance_cycle(
    client,
    *,
    cycle_data: dict[str, object],
    published: list[dict[str, object]],
) -> None:
    recovery_run_ids = list(cycle_data["recovery_run_ids"])
    auto_resume_run_ids = list(cycle_data["auto_resume_run_ids"])
    timeout_run_ids = list(cycle_data["timeout_run_ids"])
    _assert_batched_operator_churn_db_state(
        client,
        recovery_run_ids=recovery_run_ids,
        auto_resume_run_ids=auto_resume_run_ids,
        timeout_run_ids=timeout_run_ids,
    )
    _assert_invalid_maintenance_runs_fail_closed(
        client,
        invalid_wait_run_id=str(cycle_data["invalid_wait_run_id"]),
        foreign_wait_run_id=str(cycle_data["foreign_wait_run_id"]),
        source_wait_run_id=str(cycle_data["source_wait_run_id"]),
        published=published,
    )


def _collect_mixed_valid_invalid_maintenance_expectations(
    processed_cycles: list[dict[str, object]],
) -> tuple[list[str], list[str], dict[str, str], list[str], dict[str, str], int]:
    all_recovery_run_ids: list[str] = []
    all_auto_resume_run_ids: list[str] = []
    all_timeout_summaries: dict[str, str] = {}
    all_source_wait_run_ids: list[str] = []
    all_failed_expected_errors: dict[str, str] = {}
    expected_event_count = 0

    for cycle_data in processed_cycles:
        recovery_run_ids = list(cycle_data["recovery_run_ids"])
        auto_resume_run_ids = list(cycle_data["auto_resume_run_ids"])
        timeout_summaries = dict(cycle_data["timeout_summaries"])
        all_recovery_run_ids.extend(recovery_run_ids)
        all_auto_resume_run_ids.extend(auto_resume_run_ids)
        all_timeout_summaries.update(timeout_summaries)
        all_source_wait_run_ids.append(str(cycle_data["source_wait_run_id"]))
        all_failed_expected_errors[str(cycle_data["invalid_wait_run_id"])] = "invalid resume_at"
        all_failed_expected_errors[str(cycle_data["foreign_wait_run_id"])] = "foreign checkpoint"
        expected_event_count += (len(recovery_run_ids) * 3) + 3

    return (
        all_recovery_run_ids,
        all_auto_resume_run_ids,
        all_timeout_summaries,
        all_source_wait_run_ids,
        all_failed_expected_errors,
        expected_event_count,
    )


def _assert_mixed_valid_invalid_maintenance_published_events(
    maintenance_events: list[dict[str, object]],
    *,
    recovery_run_ids: list[str],
    auto_resume_run_ids: list[str],
    timeout_summaries: dict[str, str],
    source_wait_run_ids: list[str],
    failed_expected_errors: dict[str, str],
) -> None:
    expected_expired_run_ids = set(timeout_summaries) | set(source_wait_run_ids)

    published_types = [event.get("type") for event in maintenance_events]
    assert published_types.count("workflow.run.recovered") == len(recovery_run_ids)
    assert published_types.count("workflow.run.resumed") == len(auto_resume_run_ids)
    assert published_types.count("workflow.run.expired") == len(expected_expired_run_ids)
    assert published_types.count("workflow.run.failed") == len(failed_expected_errors)
    assert {
        event.get("workflow_run_id")
        for event in maintenance_events
        if event.get("type") == "workflow.run.recovered"
    } == set(recovery_run_ids)
    assert {
        event.get("workflow_run_id")
        for event in maintenance_events
        if event.get("type") == "workflow.run.resumed"
    } == set(auto_resume_run_ids)
    assert {
        event.get("workflow_run_id")
        for event in maintenance_events
        if event.get("type") == "workflow.run.expired"
    } == expected_expired_run_ids
    assert {
        event.get("workflow_run_id")
        for event in maintenance_events
        if event.get("type") == "workflow.run.failed"
    } == set(failed_expected_errors)

    for event in maintenance_events:
        event_type = event.get("type")
        run_id = event.get("workflow_run_id")
        assert isinstance(run_id, str)
        if event_type == "workflow.run.recovered":
            assert event.get("reason") == "lease_expired"
            continue
        if event_type == "workflow.run.resumed":
            assert event.get("status") == "queued"
            continue
        if event_type == "workflow.run.expired":
            if run_id in timeout_summaries:
                assert event.get("error") == timeout_summaries[run_id]
            else:
                assert run_id in source_wait_run_ids
            continue
        if event_type == "workflow.run.failed":
            assert failed_expected_errors[run_id] in str(event.get("error"))


def _assert_mixed_valid_invalid_maintenance_load_profile_aggregate(
    client,
    *,
    published: list[dict[str, object]],
    processed_cycles: list[dict[str, object]],
) -> None:
    maintenance_types = {
        "workflow.run.recovered",
        "workflow.run.resumed",
        "workflow.run.expired",
        "workflow.run.failed",
    }
    maintenance_events = [event for event in published if event.get("type") in maintenance_types]

    for cycle_data in processed_cycles:
        recovery_run_ids = list(cycle_data["recovery_run_ids"])
        auto_resume_run_ids = list(cycle_data["auto_resume_run_ids"])
        timeout_run_ids = list(cycle_data["timeout_run_ids"])
        timeout_summaries = dict(cycle_data["timeout_summaries"])
        cycle_run_ids = {
            *recovery_run_ids,
            *auto_resume_run_ids,
            *timeout_run_ids,
            str(cycle_data["invalid_wait_run_id"]),
            str(cycle_data["foreign_wait_run_id"]),
            str(cycle_data["source_wait_run_id"]),
        }
        cycle_events = [
            event for event in maintenance_events if event.get("workflow_run_id") in cycle_run_ids
        ]
        assert len(cycle_events) == (len(recovery_run_ids) * 3) + 3
        _assert_mixed_valid_invalid_maintenance_cycle(
            client,
            cycle_data=cycle_data,
            published=cycle_events,
        )

    (
        all_recovery_run_ids,
        all_auto_resume_run_ids,
        all_timeout_summaries,
        all_source_wait_run_ids,
        all_failed_expected_errors,
        expected_event_count,
    ) = _collect_mixed_valid_invalid_maintenance_expectations(processed_cycles)

    assert len(maintenance_events) == expected_event_count
    unique_event_keys = {
        (str(event.get("type")), str(event.get("workflow_run_id"))) for event in maintenance_events
    }
    assert len(unique_event_keys) == expected_event_count
    _assert_mixed_valid_invalid_maintenance_published_events(
        maintenance_events,
        recovery_run_ids=all_recovery_run_ids,
        auto_resume_run_ids=all_auto_resume_run_ids,
        timeout_summaries=all_timeout_summaries,
        source_wait_run_ids=all_source_wait_run_ids,
        failed_expected_errors=all_failed_expected_errors,
    )


def test_worker_tick_sustains_extended_mixed_valid_and_invalid_maintenance_load_profile_without_stale_events(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime_approvals(client)

    worker = _build_worker(client)
    monkeypatch.setattr(worker, "_claim_next_run", lambda: None)
    monkeypatch.setattr(
        "caliber.workflows.runtime._wait_until_ready",
        lambda raw, *, timezone_name="UTC": True,
    )
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        client.app.state.event_bus,
        "publish",
        lambda payload: published.append(dict(payload)),
    )

    processed_cycles = _exercise_mixed_valid_invalid_maintenance_load_profile(
        client,
        worker=worker,
        published=published,
        batch_sizes=(2, 4, 6, 8, 10),
        name_prefix="extended-mixed-load",
    )

    _assert_mixed_valid_invalid_maintenance_load_profile_aggregate(
        client,
        published=published,
        processed_cycles=processed_cycles,
    )


def test_worker_tick_sustains_steady_state_mixed_valid_and_invalid_maintenance_churn_without_stale_events(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime_approvals(client)

    worker = _build_worker(client)
    monkeypatch.setattr(worker, "_claim_next_run", lambda: None)
    monkeypatch.setattr(
        "caliber.workflows.runtime._wait_until_ready",
        lambda raw, *, timezone_name="UTC": True,
    )
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        client.app.state.event_bus,
        "publish",
        lambda payload: published.append(dict(payload)),
    )

    processed_cycles = _exercise_mixed_valid_invalid_maintenance_load_profile(
        client,
        worker=worker,
        published=published,
        batch_sizes=(6, 6, 6, 6, 6, 6, 6, 6, 6, 6),
        name_prefix="steady-state-mixed",
    )

    _assert_mixed_valid_invalid_maintenance_load_profile_aggregate(
        client,
        published=published,
        processed_cycles=processed_cycles,
    )


def test_worker_tick_fails_missing_waiting_checkpoint_rows_while_processing_valid_maintenance_rows(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime_approvals(client)
    recovery_run_id, auto_resume_run_id, timeout_run_id = _create_operator_churn_runs(client)
    _prime_operator_churn_states(
        client,
        recovery_run_id=recovery_run_id,
        auto_resume_run_id=auto_resume_run_id,
        timeout_run_id=timeout_run_id,
    )
    missing_wait_run_id = _create_missing_checkpoint_maintenance_run(client)

    with client.app.state.session_factory() as session:
        missing_wait_run = session.get(CaliberWorkflowRun, missing_wait_run_id)
        assert missing_wait_run is not None
        missing_wait_run.status = "waiting_event"
        missing_wait_run.current_node_id = "wait_gate"
        missing_wait_run.error_code = "waiting_event"
        missing_wait_run.error_summary = "waiting for resume event"
        missing_wait_run.claimed_by = None
        missing_wait_run.claimed_at = None
        missing_wait_run.lease_expires_at = None
        missing_wait_run.summary = {
            **dict(missing_wait_run.summary or {}),
            "status": "waiting_event",
            "resume_checkpoint_id": "WRC-missing-maintenance",
            "resume_checkpoint_run_id": missing_wait_run.workflow_run_id,
        }
        session.commit()

    worker = _build_worker(client)
    monkeypatch.setattr(worker, "_claim_next_run", lambda: None)
    monkeypatch.setattr(
        "caliber.workflows.runtime._wait_until_ready",
        lambda raw, *, timezone_name="UTC": True,
    )
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        client.app.state.event_bus,
        "publish",
        lambda payload: published.append(dict(payload)),
    )

    worker._tick()
    timeout_error_summary = _assert_operator_churn_db_state(
        client,
        recovery_run_id=recovery_run_id,
        auto_resume_run_id=auto_resume_run_id,
        timeout_run_id=timeout_run_id,
    )

    with client.app.state.session_factory() as session:
        missing_wait_run = session.get(CaliberWorkflowRun, missing_wait_run_id)
        assert missing_wait_run is not None
        assert missing_wait_run.status == "failed"
        assert missing_wait_run.error_code == "resume_checkpoint_unavailable"
        assert "missing checkpoint" in str(missing_wait_run.error_summary)
        missing_events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == missing_wait_run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert any(event.event_type == "workflow.run.failed" for event in missing_events)

    published_types = [event.get("type") for event in published]
    assert published_types.count("workflow.run.recovered") == 1
    assert published_types.count("workflow.run.resumed") == 1
    assert published_types.count("workflow.run.expired") == 1
    assert published_types.count("workflow.run.failed") == 1
    assert any(
        event.get("type") == "workflow.run.recovered"
        and event.get("workflow_run_id") == recovery_run_id
        and event.get("reason") == "lease_expired"
        for event in published
    )
    assert any(
        event.get("type") == "workflow.run.resumed"
        and event.get("workflow_run_id") == auto_resume_run_id
        and event.get("status") == "queued"
        for event in published
    )
    assert any(
        event.get("type") == "workflow.run.expired"
        and event.get("workflow_run_id") == timeout_run_id
        and event.get("error") == timeout_error_summary
        for event in published
    )
    assert any(
        event.get("type") == "workflow.run.failed"
        and event.get("workflow_run_id") == missing_wait_run_id
        and "missing checkpoint" in str(event.get("error"))
        for event in published
    )


def test_worker_tick_fails_waiting_event_runs_without_checkpoint_reference_while_processing_valid_maintenance_rows(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime_approvals(client)
    recovery_run_id, auto_resume_run_id, timeout_run_id = _create_operator_churn_runs(client)
    _prime_operator_churn_states(
        client,
        recovery_run_id=recovery_run_id,
        auto_resume_run_id=auto_resume_run_id,
        timeout_run_id=timeout_run_id,
    )
    missing_ref_run_id = _create_missing_checkpoint_maintenance_run(client)

    with client.app.state.session_factory() as session:
        missing_ref_run = session.get(CaliberWorkflowRun, missing_ref_run_id)
        assert missing_ref_run is not None
        missing_ref_run.status = "waiting_event"
        missing_ref_run.current_node_id = "wait_gate"
        missing_ref_run.error_code = "waiting_event"
        missing_ref_run.error_summary = "waiting for resume event"
        missing_ref_run.claimed_by = None
        missing_ref_run.claimed_at = None
        missing_ref_run.lease_expires_at = None
        missing_ref_run.summary = {"status": "waiting_event"}
        session.commit()

    worker = _build_worker(client)
    monkeypatch.setattr(worker, "_claim_next_run", lambda: None)
    monkeypatch.setattr(
        "caliber.workflows.runtime._wait_until_ready",
        lambda raw, *, timezone_name="UTC": True,
    )
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        client.app.state.event_bus,
        "publish",
        lambda payload: published.append(dict(payload)),
    )

    worker._tick()
    timeout_error_summary = _assert_operator_churn_db_state(
        client,
        recovery_run_id=recovery_run_id,
        auto_resume_run_id=auto_resume_run_id,
        timeout_run_id=timeout_run_id,
    )

    with client.app.state.session_factory() as session:
        missing_ref_run = session.get(CaliberWorkflowRun, missing_ref_run_id)
        assert missing_ref_run is not None
        assert missing_ref_run.status == "failed"
        assert missing_ref_run.error_code == "resume_checkpoint_unavailable"
        assert "missing resume checkpoint reference" in str(missing_ref_run.error_summary)
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == missing_ref_run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert any(event.event_type == "workflow.run.failed" for event in events)

    published_types = [event.get("type") for event in published]
    assert published_types.count("workflow.run.recovered") == 1
    assert published_types.count("workflow.run.resumed") == 1
    assert published_types.count("workflow.run.expired") == 1
    assert published_types.count("workflow.run.failed") == 1
    assert any(
        event.get("type") == "workflow.run.expired"
        and event.get("workflow_run_id") == timeout_run_id
        and event.get("error") == timeout_error_summary
        for event in published
    )
    assert any(
        event.get("type") == "workflow.run.failed"
        and event.get("workflow_run_id") == missing_ref_run_id
        and "missing resume checkpoint reference" in str(event.get("error"))
        for event in published
    )


def test_worker_tick_fails_invalid_wait_until_checkpoints_while_processing_valid_maintenance_rows(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime_approvals(client)
    recovery_run_id, auto_resume_run_id, timeout_run_id = _create_operator_churn_runs(client)
    _prime_operator_churn_states(
        client,
        recovery_run_id=recovery_run_id,
        auto_resume_run_id=auto_resume_run_id,
        timeout_run_id=timeout_run_id,
    )
    invalid_wait_run_id, foreign_wait_run_id, source_wait_run_id = _create_invalid_maintenance_runs(
        client
    )
    _prime_invalid_maintenance_states(
        client,
        invalid_wait_run_id=invalid_wait_run_id,
        foreign_wait_run_id=foreign_wait_run_id,
        source_wait_run_id=source_wait_run_id,
    )

    worker = _build_worker(client)
    monkeypatch.setattr(worker, "_claim_next_run", lambda: None)
    monkeypatch.setattr(
        "caliber.workflows.runtime._wait_until_ready",
        lambda raw, *, timezone_name="UTC": True,
    )
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        client.app.state.event_bus,
        "publish",
        lambda payload: published.append(dict(payload)),
    )

    worker._tick()
    _assert_invalid_maintenance_state(
        client,
        invalid_wait_run_id=invalid_wait_run_id,
        foreign_wait_run_id=foreign_wait_run_id,
        source_wait_run_id=source_wait_run_id,
        auto_resume_run_id=auto_resume_run_id,
        timeout_run_id=timeout_run_id,
        published=published,
    )


def test_worker_tick_fails_invalid_wait_for_event_timeout_checkpoints_while_processing_valid_maintenance_rows(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime_approvals(client)
    recovery_run_id, auto_resume_run_id, timeout_run_id = _create_operator_churn_runs(client)
    _prime_operator_churn_states(
        client,
        recovery_run_id=recovery_run_id,
        auto_resume_run_id=auto_resume_run_id,
        timeout_run_id=timeout_run_id,
    )
    invalid_timeout_manifest = _wait_event_manifest(
        "wait-event-invalid-timeout-maintenance-worker-wf",
        timeout_seconds=30,
    )
    _invalid_timeout_wid, invalid_timeout_vid = create_and_publish(
        client,
        workflow_name="Wait Event Invalid Timeout Maintenance Worker",
        manifest=invalid_timeout_manifest,
    )
    invalid_timeout_created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": invalid_timeout_vid, "input": "bad timeout"},
    )
    assert invalid_timeout_created.status_code == 202
    invalid_timeout_run_id = invalid_timeout_created.json()["data"]["workflow_run_id"]

    with client.app.state.session_factory() as session:
        invalid_timeout_run = session.get(CaliberWorkflowRun, invalid_timeout_run_id)
        assert invalid_timeout_run is not None
        _seed_waiting_checkpoint(
            session,
            run=invalid_timeout_run,
            checkpoint_id="WRC-invalid-timeout-maintenance",
            node_id="wait_gate",
            state_blob={
                "kind": "wait_for_event",
                "node_id": "wait_gate",
                "expected_event_name": "ticket.approved",
                "timeout_seconds": "not-a-number",
                "input_by_port": {"input": "bad timeout"},
            },
            created_at=datetime.now(timezone.utc) - timedelta(seconds=45),
        )
        session.commit()

    worker = _build_worker(client)
    monkeypatch.setattr(worker, "_claim_next_run", lambda: None)
    monkeypatch.setattr(
        "caliber.workflows.runtime._wait_until_ready",
        lambda raw, *, timezone_name="UTC": True,
    )
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        client.app.state.event_bus,
        "publish",
        lambda payload: published.append(dict(payload)),
    )

    worker._tick()
    timeout_error_summary = _assert_operator_churn_db_state(
        client,
        recovery_run_id=recovery_run_id,
        auto_resume_run_id=auto_resume_run_id,
        timeout_run_id=timeout_run_id,
    )

    with client.app.state.session_factory() as session:
        invalid_timeout_run = session.get(CaliberWorkflowRun, invalid_timeout_run_id)
        assert invalid_timeout_run is not None
        assert invalid_timeout_run.status == "failed"
        assert invalid_timeout_run.error_code == "resume_checkpoint_unavailable"
        assert "invalid timeout_seconds" in str(invalid_timeout_run.error_summary)
        invalid_events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == invalid_timeout_run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert any(event.event_type == "workflow.run.failed" for event in invalid_events)

    published_types = [event.get("type") for event in published]
    assert published_types.count("workflow.run.recovered") == 1
    assert published_types.count("workflow.run.resumed") == 1
    assert published_types.count("workflow.run.expired") == 1
    assert published_types.count("workflow.run.failed") == 1
    assert any(
        event.get("type") == "workflow.run.expired"
        and event.get("workflow_run_id") == timeout_run_id
        and event.get("error") == timeout_error_summary
        for event in published
    )
    assert any(
        event.get("type") == "workflow.run.failed"
        and event.get("workflow_run_id") == invalid_timeout_run_id
        and "invalid timeout_seconds" in str(event.get("error"))
        for event in published
    )


def test_worker_tick_fails_invalid_waiting_checkpoint_kinds_while_processing_valid_maintenance_rows(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime_approvals(client)
    recovery_run_id, auto_resume_run_id, timeout_run_id = _create_operator_churn_runs(client)
    _prime_operator_churn_states(
        client,
        recovery_run_id=recovery_run_id,
        auto_resume_run_id=auto_resume_run_id,
        timeout_run_id=timeout_run_id,
    )
    invalid_kind_manifest = _wait_event_manifest(
        "wait-event-invalid-kind-maintenance-worker-wf",
        timeout_seconds=30,
    )
    _invalid_kind_wid, invalid_kind_vid = create_and_publish(
        client,
        workflow_name="Wait Event Invalid Kind Maintenance Worker",
        manifest=invalid_kind_manifest,
    )
    invalid_kind_created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": invalid_kind_vid, "input": "bad kind"},
    )
    assert invalid_kind_created.status_code == 202
    invalid_kind_run_id = invalid_kind_created.json()["data"]["workflow_run_id"]

    with client.app.state.session_factory() as session:
        invalid_kind_run = session.get(CaliberWorkflowRun, invalid_kind_run_id)
        assert invalid_kind_run is not None
        _seed_waiting_checkpoint(
            session,
            run=invalid_kind_run,
            checkpoint_id="WRC-invalid-kind-maintenance",
            node_id="wait_gate",
            state_blob={
                "kind": "runtime_approval",
                "node_id": "wait_gate",
                "output": "bad kind",
                "input_by_port": {"input": "bad kind"},
            },
            created_at=datetime.now(timezone.utc) - timedelta(seconds=45),
        )
        session.commit()

    worker = _build_worker(client)
    monkeypatch.setattr(worker, "_claim_next_run", lambda: None)
    monkeypatch.setattr(
        "caliber.workflows.runtime._wait_until_ready",
        lambda raw, *, timezone_name="UTC": True,
    )
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        client.app.state.event_bus,
        "publish",
        lambda payload: published.append(dict(payload)),
    )

    worker._tick()
    timeout_error_summary = _assert_operator_churn_db_state(
        client,
        recovery_run_id=recovery_run_id,
        auto_resume_run_id=auto_resume_run_id,
        timeout_run_id=timeout_run_id,
    )

    with client.app.state.session_factory() as session:
        invalid_kind_run = session.get(CaliberWorkflowRun, invalid_kind_run_id)
        assert invalid_kind_run is not None
        assert invalid_kind_run.status == "failed"
        assert invalid_kind_run.error_code == "resume_checkpoint_unavailable"
        assert "invalid kind" in str(invalid_kind_run.error_summary)
        invalid_events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == invalid_kind_run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert any(event.event_type == "workflow.run.failed" for event in invalid_events)

    published_types = [event.get("type") for event in published]
    assert published_types.count("workflow.run.recovered") == 1
    assert published_types.count("workflow.run.resumed") == 1
    assert published_types.count("workflow.run.expired") == 1
    assert published_types.count("workflow.run.failed") == 1
    assert any(
        event.get("type") == "workflow.run.expired"
        and event.get("workflow_run_id") == timeout_run_id
        and event.get("error") == timeout_error_summary
        for event in published
    )
    assert any(
        event.get("type") == "workflow.run.failed"
        and event.get("workflow_run_id") == invalid_kind_run_id
        and "invalid kind" in str(event.get("error"))
        for event in published
    )


def test_worker_tick_fails_waiting_checkpoints_missing_node_id_while_processing_valid_maintenance_rows(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime_approvals(client)
    recovery_run_id, auto_resume_run_id, timeout_run_id = _create_operator_churn_runs(client)
    _prime_operator_churn_states(
        client,
        recovery_run_id=recovery_run_id,
        auto_resume_run_id=auto_resume_run_id,
        timeout_run_id=timeout_run_id,
    )
    missing_node_manifest = _wait_event_manifest(
        "wait-event-missing-node-maintenance-worker-wf",
        timeout_seconds=30,
    )
    _missing_node_wid, missing_node_vid = create_and_publish(
        client,
        workflow_name="Wait Event Missing Node Maintenance Worker",
        manifest=missing_node_manifest,
    )
    missing_node_created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": missing_node_vid, "input": "missing node"},
    )
    assert missing_node_created.status_code == 202
    missing_node_run_id = missing_node_created.json()["data"]["workflow_run_id"]

    with client.app.state.session_factory() as session:
        missing_node_run = session.get(CaliberWorkflowRun, missing_node_run_id)
        assert missing_node_run is not None
        _seed_waiting_checkpoint(
            session,
            run=missing_node_run,
            checkpoint_id="WRC-missing-node-maintenance",
            node_id="wait_gate",
            state_blob={
                "kind": "wait_for_event",
                "node_id": "",
                "expected_event_name": "ticket.approved",
                "timeout_seconds": 30.0,
                "input_by_port": {"input": "missing node"},
            },
            created_at=datetime.now(timezone.utc) - timedelta(seconds=45),
        )
        session.commit()

    worker = _build_worker(client)
    monkeypatch.setattr(worker, "_claim_next_run", lambda: None)
    monkeypatch.setattr(
        "caliber.workflows.runtime._wait_until_ready",
        lambda raw, *, timezone_name="UTC": True,
    )
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        client.app.state.event_bus,
        "publish",
        lambda payload: published.append(dict(payload)),
    )

    worker._tick()
    timeout_error_summary = _assert_operator_churn_db_state(
        client,
        recovery_run_id=recovery_run_id,
        auto_resume_run_id=auto_resume_run_id,
        timeout_run_id=timeout_run_id,
    )

    with client.app.state.session_factory() as session:
        missing_node_run = session.get(CaliberWorkflowRun, missing_node_run_id)
        assert missing_node_run is not None
        assert missing_node_run.status == "failed"
        assert missing_node_run.error_code == "resume_checkpoint_unavailable"
        assert "missing node_id" in str(missing_node_run.error_summary)
        invalid_events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == missing_node_run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert any(event.event_type == "workflow.run.failed" for event in invalid_events)

    published_types = [event.get("type") for event in published]
    assert published_types.count("workflow.run.recovered") == 1
    assert published_types.count("workflow.run.resumed") == 1
    assert published_types.count("workflow.run.expired") == 1
    assert published_types.count("workflow.run.failed") == 1
    assert any(
        event.get("type") == "workflow.run.expired"
        and event.get("workflow_run_id") == timeout_run_id
        and event.get("error") == timeout_error_summary
        for event in published
    )
    assert any(
        event.get("type") == "workflow.run.failed"
        and event.get("workflow_run_id") == missing_node_run_id
        and "missing node_id" in str(event.get("error"))
        for event in published
    )


@pytest.mark.parametrize(
    ("checkpoint_node_id", "state_node_id"),
    [
        ("other_gate", "other_gate"),
        ("wait_gate", "other_gate"),
    ],
)
def test_worker_tick_fails_waiting_checkpoints_with_mismatched_node_identity_while_processing_valid_maintenance_rows(
    client,
    monkeypatch: pytest.MonkeyPatch,
    checkpoint_node_id: str,
    state_node_id: str,
) -> None:
    _enable_runtime_approvals(client)
    recovery_run_id, auto_resume_run_id, timeout_run_id = _create_operator_churn_runs(client)
    _prime_operator_churn_states(
        client,
        recovery_run_id=recovery_run_id,
        auto_resume_run_id=auto_resume_run_id,
        timeout_run_id=timeout_run_id,
    )
    mismatched_node_manifest = _wait_event_manifest(
        "wait-event-mismatched-node-maintenance-worker-wf",
        timeout_seconds=30,
    )
    _mismatched_node_wid, mismatched_node_vid = create_and_publish(
        client,
        workflow_name="Wait Event Mismatched Node Maintenance Worker",
        manifest=mismatched_node_manifest,
    )
    mismatched_node_created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": mismatched_node_vid, "input": "mismatched node"},
    )
    assert mismatched_node_created.status_code == 202
    mismatched_node_run_id = mismatched_node_created.json()["data"]["workflow_run_id"]

    with client.app.state.session_factory() as session:
        mismatched_node_run = session.get(CaliberWorkflowRun, mismatched_node_run_id)
        assert mismatched_node_run is not None
        _seed_waiting_checkpoint(
            session,
            run=mismatched_node_run,
            checkpoint_id="WRC-mismatched-node-maintenance",
            node_id=checkpoint_node_id,
            state_blob={
                "kind": "wait_for_event",
                "node_id": state_node_id,
                "expected_event_name": "ticket.approved",
                "timeout_seconds": 30.0,
                "input_by_port": {"input": "mismatched node"},
            },
            created_at=datetime.now(timezone.utc) - timedelta(seconds=45),
        )
        mismatched_node_run.current_node_id = "wait_gate"
        session.commit()

    worker = _build_worker(client)
    monkeypatch.setattr(worker, "_claim_next_run", lambda: None)
    monkeypatch.setattr(
        "caliber.workflows.runtime._wait_until_ready",
        lambda raw, *, timezone_name="UTC": True,
    )
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        client.app.state.event_bus,
        "publish",
        lambda payload: published.append(dict(payload)),
    )

    worker._tick()
    timeout_error_summary = _assert_operator_churn_db_state(
        client,
        recovery_run_id=recovery_run_id,
        auto_resume_run_id=auto_resume_run_id,
        timeout_run_id=timeout_run_id,
    )

    with client.app.state.session_factory() as session:
        mismatched_node_run = session.get(CaliberWorkflowRun, mismatched_node_run_id)
        assert mismatched_node_run is not None
        assert mismatched_node_run.status == "failed"
        assert mismatched_node_run.error_code == "resume_checkpoint_unavailable"
        assert "does not match waiting run node" in str(mismatched_node_run.error_summary)
        invalid_events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == mismatched_node_run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert any(event.event_type == "workflow.run.failed" for event in invalid_events)

    published_types = [event.get("type") for event in published]
    assert published_types.count("workflow.run.recovered") == 1
    assert published_types.count("workflow.run.resumed") == 1
    assert published_types.count("workflow.run.expired") == 1
    assert published_types.count("workflow.run.failed") == 1
    assert any(
        event.get("type") == "workflow.run.expired"
        and event.get("workflow_run_id") == timeout_run_id
        and event.get("error") == timeout_error_summary
        for event in published
    )
    assert any(
        event.get("type") == "workflow.run.failed"
        and event.get("workflow_run_id") == mismatched_node_run_id
        and "does not match waiting run node" in str(event.get("error"))
        for event in published
    )


def test_worker_tick_fails_corrupt_waiting_checkpoint_payloads_while_processing_valid_maintenance_rows(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime_approvals(client)
    recovery_run_id, auto_resume_run_id, timeout_run_id = _create_operator_churn_runs(client)
    _prime_operator_churn_states(
        client,
        recovery_run_id=recovery_run_id,
        auto_resume_run_id=auto_resume_run_id,
        timeout_run_id=timeout_run_id,
    )
    corrupt_wait_run_id = _create_corrupt_checkpoint_maintenance_run(client)

    with client.app.state.session_factory() as session:
        corrupt_wait_run = session.get(CaliberWorkflowRun, corrupt_wait_run_id)
        assert corrupt_wait_run is not None
        corrupt_wait_run.status = "waiting_event"
        corrupt_wait_run.current_node_id = "wait_gate"
        corrupt_wait_run.error_code = "waiting_event"
        corrupt_wait_run.error_summary = "waiting for resume event"
        corrupt_wait_run.claimed_by = None
        corrupt_wait_run.claimed_at = None
        corrupt_wait_run.lease_expires_at = None
        corrupt_wait_run.summary = {
            **dict(corrupt_wait_run.summary or {}),
            "status": "waiting_event",
            "resume_checkpoint_id": "WRC-corrupt-maintenance",
            "resume_checkpoint_run_id": corrupt_wait_run.workflow_run_id,
        }
        session.add(
            CaliberWorkflowRunCheckpoint(
                checkpoint_id="WRC-corrupt-maintenance",
                workflow_run_id=corrupt_wait_run.workflow_run_id,
                project_id=corrupt_wait_run.project_id,
                sequence=1,
                node_id="wait_gate",
                state_blob=["corrupt-checkpoint-payload"],
            )
        )
        session.commit()

    worker = _build_worker(client)
    monkeypatch.setattr(worker, "_claim_next_run", lambda: None)
    monkeypatch.setattr(
        "caliber.workflows.runtime._wait_until_ready",
        lambda raw, *, timezone_name="UTC": True,
    )
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        client.app.state.event_bus,
        "publish",
        lambda payload: published.append(dict(payload)),
    )

    worker._tick()
    timeout_error_summary = _assert_operator_churn_db_state(
        client,
        recovery_run_id=recovery_run_id,
        auto_resume_run_id=auto_resume_run_id,
        timeout_run_id=timeout_run_id,
    )

    with client.app.state.session_factory() as session:
        corrupt_wait_run = session.get(CaliberWorkflowRun, corrupt_wait_run_id)
        assert corrupt_wait_run is not None
        assert corrupt_wait_run.status == "failed"
        assert corrupt_wait_run.error_code == "resume_checkpoint_unavailable"
        assert "corrupt state" in str(corrupt_wait_run.error_summary)
        corrupt_events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == corrupt_wait_run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert any(event.event_type == "workflow.run.failed" for event in corrupt_events)

    published_types = [event.get("type") for event in published]
    assert published_types.count("workflow.run.recovered") == 1
    assert published_types.count("workflow.run.resumed") == 1
    assert published_types.count("workflow.run.expired") == 1
    assert published_types.count("workflow.run.failed") == 1
    assert any(
        event.get("type") == "workflow.run.recovered"
        and event.get("workflow_run_id") == recovery_run_id
        and event.get("reason") == "lease_expired"
        for event in published
    )
    assert any(
        event.get("type") == "workflow.run.resumed"
        and event.get("workflow_run_id") == auto_resume_run_id
        and event.get("status") == "queued"
        for event in published
    )
    assert any(
        event.get("type") == "workflow.run.expired"
        and event.get("workflow_run_id") == timeout_run_id
        and event.get("error") == timeout_error_summary
        for event in published
    )
    assert any(
        event.get("type") == "workflow.run.failed"
        and event.get("workflow_run_id") == corrupt_wait_run_id
        and "corrupt state" in str(event.get("error"))
        for event in published
    )


def _create_large_mixed_invalid_maintenance_run_ids(client) -> dict[str, str]:
    wait_event_manifest = _wait_event_manifest(
        "wait-event-mixed-invalid-maintenance-worker-wf",
        timeout_seconds=30,
    )
    _invalid_event_wid, invalid_event_vid = create_and_publish(
        client,
        workflow_name="Wait Event Mixed Invalid Maintenance Worker",
        manifest=wait_event_manifest,
    )
    wait_until_manifest = _wait_until_manifest(
        "wait-until-mixed-invalid-maintenance-worker-wf",
        wait_until="2099-01-01T00:00:00",
        timezone_name="UTC",
    )
    _invalid_wait_wid, invalid_wait_vid = create_and_publish(
        client,
        workflow_name="Wait Until Mixed Invalid Maintenance Worker",
        manifest=wait_until_manifest,
    )

    def _create_run(version_id: str, text: str) -> str:
        created = client.post(
            f"{PREFIX}/workflow-runs",
            json={"workflow_version_id": version_id, "input": text},
        )
        assert created.status_code == 202
        return created.json()["data"]["workflow_run_id"]

    return {
        "source_wait": _create_run(invalid_event_vid, "source checkpoint"),
        "missing_wait": _create_run(invalid_event_vid, "missing checkpoint row"),
        "missing_ref": _create_run(invalid_event_vid, "missing reference"),
        "foreign_wait": _create_run(invalid_event_vid, "foreign checkpoint"),
        "invalid_wait": _create_run(invalid_wait_vid, "invalid resume_at"),
        "invalid_timeout": _create_run(invalid_event_vid, "invalid timeout"),
        "invalid_kind": _create_run(invalid_event_vid, "invalid kind"),
        "missing_node": _create_run(invalid_event_vid, "missing node"),
        "mismatched_node": _create_run(invalid_event_vid, "mismatched node"),
        "corrupt_wait": _create_run(invalid_event_vid, "corrupt state"),
    }


def _prime_large_mixed_invalid_maintenance_states(
    client,
    *,
    run_ids: dict[str, str],
) -> dict[str, str]:
    invalid_expected_errors = {
        run_ids["missing_wait"]: "missing checkpoint",
        run_ids["missing_ref"]: "missing resume checkpoint reference",
        run_ids["foreign_wait"]: "foreign checkpoint",
        run_ids["invalid_wait"]: "invalid resume_at",
        run_ids["invalid_timeout"]: "invalid timeout_seconds",
        run_ids["invalid_kind"]: "invalid kind",
        run_ids["missing_node"]: "missing node_id",
        run_ids["mismatched_node"]: "does not match waiting run node",
        run_ids["corrupt_wait"]: "corrupt state",
    }

    with client.app.state.session_factory() as session:
        source_wait_run = session.get(CaliberWorkflowRun, run_ids["source_wait"])
        missing_wait_run = session.get(CaliberWorkflowRun, run_ids["missing_wait"])
        missing_ref_run = session.get(CaliberWorkflowRun, run_ids["missing_ref"])
        foreign_wait_run = session.get(CaliberWorkflowRun, run_ids["foreign_wait"])
        invalid_wait_run = session.get(CaliberWorkflowRun, run_ids["invalid_wait"])
        invalid_timeout_run = session.get(CaliberWorkflowRun, run_ids["invalid_timeout"])
        invalid_kind_run = session.get(CaliberWorkflowRun, run_ids["invalid_kind"])
        missing_node_run = session.get(CaliberWorkflowRun, run_ids["missing_node"])
        mismatched_node_run = session.get(CaliberWorkflowRun, run_ids["mismatched_node"])
        corrupt_wait_run = session.get(CaliberWorkflowRun, run_ids["corrupt_wait"])
        assert source_wait_run is not None
        assert missing_wait_run is not None
        assert missing_ref_run is not None
        assert foreign_wait_run is not None
        assert invalid_wait_run is not None
        assert invalid_timeout_run is not None
        assert invalid_kind_run is not None
        assert missing_node_run is not None
        assert mismatched_node_run is not None
        assert corrupt_wait_run is not None

        session.add(
            CaliberWorkflowRunCheckpoint(
                checkpoint_id="WRC-foreign-source-mixed-maintenance",
                workflow_run_id=source_wait_run.workflow_run_id,
                project_id=source_wait_run.project_id,
                sequence=1,
                node_id="wait_gate",
                state_blob={
                    "kind": "wait_for_event",
                    "node_id": "wait_gate",
                    "expected_event_name": "ticket.approved",
                    "timeout_seconds": 30.0,
                    "input_by_port": {"input": "source checkpoint"},
                },
            )
        )
        _mark_run_waiting_for_maintenance(
            missing_wait_run,
            checkpoint_id="WRC-missing-batch-maintenance",
        )
        _mark_run_waiting_for_maintenance(missing_ref_run)
        _mark_run_waiting_for_maintenance(
            foreign_wait_run,
            checkpoint_id="WRC-foreign-source-mixed-maintenance",
            checkpoint_run_id=source_wait_run.workflow_run_id,
        )
        _seed_waiting_checkpoint(
            session,
            run=invalid_wait_run,
            checkpoint_id="WRC-invalid-wait-until-batch-maintenance",
            node_id="wait_gate",
            state_blob={"kind": "wait_until", "node_id": "wait_gate", "resume_at": "not-a-date"},
        )
        _seed_waiting_checkpoint(
            session,
            run=invalid_timeout_run,
            checkpoint_id="WRC-invalid-timeout-batch-maintenance",
            node_id="wait_gate",
            state_blob={
                "kind": "wait_for_event",
                "node_id": "wait_gate",
                "expected_event_name": "ticket.approved",
                "timeout_seconds": "not-a-number",
                "input_by_port": {"input": "invalid timeout"},
            },
        )
        _seed_waiting_checkpoint(
            session,
            run=invalid_kind_run,
            checkpoint_id="WRC-invalid-kind-batch-maintenance",
            node_id="wait_gate",
            state_blob={
                "kind": "runtime_approval",
                "node_id": "wait_gate",
                "output": "invalid kind",
                "input_by_port": {"input": "invalid kind"},
            },
        )
        _seed_waiting_checkpoint(
            session,
            run=missing_node_run,
            checkpoint_id="WRC-missing-node-batch-maintenance",
            node_id="wait_gate",
            state_blob={
                "kind": "wait_for_event",
                "node_id": "",
                "expected_event_name": "ticket.approved",
                "timeout_seconds": 30.0,
                "input_by_port": {"input": "missing node"},
            },
        )
        _seed_waiting_checkpoint(
            session,
            run=mismatched_node_run,
            checkpoint_id="WRC-mismatched-node-batch-maintenance",
            node_id="other_gate",
            state_blob={
                "kind": "wait_for_event",
                "node_id": "other_gate",
                "expected_event_name": "ticket.approved",
                "timeout_seconds": 30.0,
                "input_by_port": {"input": "mismatched node"},
            },
        )
        mismatched_node_run.current_node_id = "wait_gate"
        _mark_run_waiting_for_maintenance(
            corrupt_wait_run,
            checkpoint_id="WRC-corrupt-batch-maintenance",
        )
        session.add(
            CaliberWorkflowRunCheckpoint(
                checkpoint_id="WRC-corrupt-batch-maintenance",
                workflow_run_id=corrupt_wait_run.workflow_run_id,
                project_id=corrupt_wait_run.project_id,
                sequence=1,
                node_id="wait_gate",
                state_blob=["corrupt-checkpoint-payload"],
            )
        )
        session.commit()
    return invalid_expected_errors


def _assert_large_mixed_invalid_maintenance_states(
    client,
    *,
    run_ids: dict[str, str],
    invalid_expected_errors: dict[str, str],
) -> None:
    with client.app.state.session_factory() as session:
        source_wait_run = session.get(CaliberWorkflowRun, run_ids["source_wait"])
        assert source_wait_run is not None
        assert source_wait_run.status == "queued"
        source_events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_ids["source_wait"])
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert not any(
            event.event_type
            in {"workflow.run.failed", "workflow.run.resumed", "workflow.run.expired"}
            for event in source_events
        )

        for run_id, expected_error in invalid_expected_errors.items():
            run = session.get(CaliberWorkflowRun, run_id)
            assert run is not None
            assert run.status == "failed"
            assert run.error_code == "resume_checkpoint_unavailable"
            assert expected_error in str(run.error_summary)
            events = (
                session.query(CaliberWorkflowRunEvent)
                .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
                .order_by(CaliberWorkflowRunEvent.sequence.asc())
                .all()
            )
            assert any(
                event.event_type == "workflow.run.failed"
                and expected_error in str((event.payload or {}).get("error", ""))
                for event in events
            )


def test_worker_tick_processes_large_mixed_valid_and_invalid_maintenance_rows_without_blocking_siblings(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime_approvals(client)
    recovery_run_ids, auto_resume_run_ids, timeout_run_ids = _create_batched_operator_churn_runs(
        client,
        count=10,
        name_suffix="mixed-invalid-load",
    )
    _prime_batched_operator_churn_states(
        client,
        recovery_run_ids=recovery_run_ids,
        auto_resume_run_ids=auto_resume_run_ids,
        timeout_run_ids=timeout_run_ids,
    )
    mixed_invalid_run_ids = _create_large_mixed_invalid_maintenance_run_ids(client)
    invalid_expected_errors = _prime_large_mixed_invalid_maintenance_states(
        client,
        run_ids=mixed_invalid_run_ids,
    )

    worker = _build_worker(client)
    monkeypatch.setattr(worker, "_claim_next_run", lambda: None)
    monkeypatch.setattr(
        "caliber.workflows.runtime._wait_until_ready",
        lambda raw, *, timezone_name="UTC": True,
    )
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        client.app.state.event_bus,
        "publish",
        lambda payload: published.append(dict(payload)),
    )

    worker._tick()
    timeout_summaries = _assert_batched_operator_churn_db_state(
        client,
        recovery_run_ids=recovery_run_ids,
        auto_resume_run_ids=auto_resume_run_ids,
        timeout_run_ids=timeout_run_ids,
    )
    _assert_large_mixed_invalid_maintenance_states(
        client,
        run_ids=mixed_invalid_run_ids,
        invalid_expected_errors=invalid_expected_errors,
    )

    maintenance_lifecycle_types = {
        "workflow.run.recovered",
        "workflow.run.resumed",
        "workflow.run.expired",
    }
    maintenance_lifecycle_events = [
        event for event in published if event.get("type") in maintenance_lifecycle_types
    ]
    _assert_batched_operator_churn_published_events(
        maintenance_lifecycle_events,
        recovery_run_ids=recovery_run_ids,
        auto_resume_run_ids=auto_resume_run_ids,
        timeout_summaries=timeout_summaries,
    )
    failed_events = [event for event in published if event.get("type") == "workflow.run.failed"]
    assert len(failed_events) == len(invalid_expected_errors)
    assert {event.get("workflow_run_id") for event in failed_events} == set(invalid_expected_errors)
    for event in failed_events:
        run_id = event.get("workflow_run_id")
        assert isinstance(run_id, str)
        assert invalid_expected_errors[run_id] in str(event.get("error"))

    published_event_count = len(published)
    worker._tick()
    assert len(published) == published_event_count
    _assert_large_mixed_invalid_maintenance_states(
        client,
        run_ids=mixed_invalid_run_ids,
        invalid_expected_errors=invalid_expected_errors,
    )


def test_renew_lease_extends_lease_when_held(client) -> None:
    """The heartbeat's single-renewal extends the lease for a run we still hold —
    so a long node can't let the lease lapse and trip duplicate recovery."""
    _enable_queue(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    worker = _build_worker(client)

    stale = datetime.now(timezone.utc) - timedelta(minutes=1)
    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        run.status = "running"
        run.claimed_by = worker._worker_id
        run.lease_expires_at = stale
        run.last_heartbeat_at = stale
        session.commit()

    assert worker._renew_lease(run_id) == 1

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert _as_utc(run.lease_expires_at) > stale
        assert _as_utc(run.last_heartbeat_at) > stale
        # Heartbeat only touches lease columns — never ownership or status.
        assert run.status == "running"
        assert run.claimed_by == worker._worker_id


def test_renew_lease_noop_when_reclaimed_by_other_worker(client) -> None:
    """Once another worker holds the run, our heartbeat is a no-op (0 rows) — a
    stale heartbeat can never resurrect or steal back a reclaimed run."""
    _enable_queue(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    worker = _build_worker(client)

    stale = datetime.now(timezone.utc) - timedelta(minutes=1)
    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        run.status = "running"
        run.claimed_by = "someone-else"
        run.lease_expires_at = stale
        session.commit()

    assert worker._renew_lease(run_id) == 0

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        # Lease left in the past → recovery still owns reclaiming it.
        assert _as_utc(run.lease_expires_at) < datetime.now(timezone.utc)


def test_queued_run_stores_full_input_payload(client) -> None:
    """A queued run keeps the full input in input_payload (worker replay source)
    while the summary holds only a bounded preview — so async runs no longer
    truncate large inputs to 1000 chars."""
    _enable_queue(client)
    _wid, vid = create_and_publish(client)
    big = "X" * 5000
    created = client.post(
        f"{PREFIX}/workflow-runs", json={"workflow_version_id": vid, "input": big}
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]
    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.input_payload == big
        assert len(run.summary["input"]) == 1000


def test_worker_executes_manifest_snapshot_for_editor_draft_runs(client) -> None:
    _enable_queue(client)
    workflow_id = create_workflow(client, "Snapshot Worker")
    published_manifest = make_manifest(workflow_id)
    version_id, _ = create_draft(client, workflow_id, published_manifest)
    published = client.post(f"{PREFIX}/workflow-versions/{version_id}/publish")
    assert published.status_code == 200

    snapshot_manifest = make_manifest(workflow_id)
    nodes = snapshot_manifest["nodes"]
    assert isinstance(nodes, dict)
    nodes["rewrite"] = {
        "id": "rewrite",
        "type": "python_code",
        "code": 'return {"text": "snapshot::draft-run", "result": {"mode": "snapshot"}, "metadata": {}}',
        "inputs": {"input": {"type": "string"}},
        "outputs": {
            "text": {"type": "string"},
            "result": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    snapshot_manifest["edges"] = [
        {"id": "e1", "from": "start", "to": "agent", "map": {"msg": "input"}},
        {"id": "e2", "from": "agent", "to": "rewrite", "map": {"final_output": "input"}},
        {"id": "e3", "from": "rewrite", "to": "final", "map": {"text": "response"}},
    ]

    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={
            "workflow_version_id": version_id,
            "workflow_id": workflow_id,
            "input": "hello",
            "manifest": snapshot_manifest,
        },
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.summary is not None
        assert run.summary["output"] == "snapshot::draft-run"
        summary_steps = run.summary.get("steps")
        assert isinstance(summary_steps, list)
        assert any(
            isinstance(step, dict)
            and step.get("node_id") == "rewrite"
            and step.get("node_type") == "python_code"
            for step in summary_steps
        )


def test_worker_executes_saved_version_run_from_persisted_manifest_copy(client) -> None:
    _enable_queue(client)
    workflow_id = create_workflow(client, "Saved Version Snapshot Worker")
    manifest = make_manifest(workflow_id)
    nodes = manifest["nodes"]
    assert isinstance(nodes, dict)
    nodes["rewrite"] = {
        "id": "rewrite",
        "type": "python_code",
        "code": 'return {"text": "saved-version::replayable", "result": {"mode": "saved_version"}, "metadata": {}}',
        "inputs": {"input": {"type": "string"}},
        "outputs": {
            "text": {"type": "string"},
            "result": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    manifest["edges"] = [
        {"id": "e1", "from": "start", "to": "agent", "map": {"msg": "input"}},
        {"id": "e2", "from": "agent", "to": "rewrite", "map": {"final_output": "input"}},
        {"id": "e3", "from": "rewrite", "to": "final", "map": {"text": "response"}},
    ]
    version_id, _ = create_draft(client, workflow_id, manifest)
    published = client.post(f"{PREFIX}/workflow-versions/{version_id}/publish")
    assert published.status_code == 200

    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": version_id, "workflow_id": workflow_id, "input": "hello"},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["data"]["workflow_run_id"]

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        version = session.get(CaliberWorkflowVersion, version_id)
        assert run is not None
        assert version is not None
        assert run.manifest_snapshot == version.manifest
        session.delete(version)
        session.commit()

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.summary is not None
        assert run.summary["output"] == "saved-version::replayable"
        summary_steps = run.summary.get("steps")
        assert isinstance(summary_steps, list)
        assert any(
            isinstance(step, dict)
            and step.get("node_id") == "rewrite"
            and step.get("node_type") == "python_code"
            for step in summary_steps
        )


def _artifact_emitting_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"] = {
        "start": {
            "id": "start",
            "type": "start",
            "outputs": {"msg": {"type": "string"}},
        },
        "emit_artifacts": {
            "id": "emit_artifacts",
            "type": "python_code",
            "code": (
                'return {"text": "artifact-ready", '
                '"result": {"artifacts": {"kg.json": {"business_rules": [1, 2, 3]}, '
                '"report.html": "<html>x</html>"}}, '
                '"metadata": {"kind": "artifact-emitter"}}'
            ),
            "inputs": {"input": {"type": "string"}},
            "outputs": {
                "text": {"type": "string"},
                "result": {"type": "structured"},
                "metadata": {"type": "structured"},
            },
        },
        "final": {
            "id": "final",
            "type": "output",
            "inputs": {"response": {"type": "string"}},
        },
    }
    manifest["edges"] = [
        {"id": "e_start_emit", "from": "start", "to": "emit_artifacts", "map": {"msg": "input"}},
        {
            "id": "e_emit_final",
            "from": "emit_artifacts",
            "to": "final",
            "map": {"text": "response"},
        },
    ]
    return manifest


def _artifact_result() -> WorkflowRunResult:
    return WorkflowRunResult(
        status="completed",
        output="kg: 3 rules / 2 entities",
        steps=[
            NodeStep(
                "visualize",
                "python_code",
                "ok",
                detail="kg",
                duration_ms=5,
                tokens=42,
                prompt_tokens=18,
                completion_tokens=24,
                cached_prompt_tokens=12,
                cost_usd=0.000042,
                model="gpt-4.1-mini",
                prompt_version="openai_responses",
            )
        ],
        tokens=42,
        artifacts={"kg.json": '{"business_rules": [1, 2, 3]}', "report.html": "<html>x</html>"},
        mlflow_run_id="MR-1",
    )


def test_artifact_objects_keys_and_content_types(client) -> None:
    """A completed run's artifacts + log map to pipeline/<run>/* and logs/<run>.json
    with correct content types when a bucket is configured."""
    worker = _build_worker(client)
    worker._config = worker._config.model_copy(
        update={
            "workflow_run_artifact_bucket": "caliber-suite",
            "workflow_run_artifact_prefix": "pipeline",
            "workflow_run_log_prefix": "logs",
        }
    )
    run = SimpleNamespace(workflow_run_id="WR-1", workflow_id="WF-1", workflow_version_id="WFV-1")
    objs = worker._artifact_objects(run, _artifact_result())
    by_key = {key: (body, ctype) for key, body, ctype in objs}
    assert by_key["pipeline/WR-1/kg.json"][1] == "application/json"
    assert by_key["pipeline/WR-1/report.html"][1] == "text/html"
    assert "logs/WR-1.json" in by_key
    log = json.loads(by_key["logs/WR-1.json"][0])
    assert log["status"] == "completed"
    assert log["tokens"] == 42
    assert log["node_path"] == ["visualize"]
    assert log["artifacts"] == ["kg.json", "report.html"]
    assert isinstance(log["steps"], list)
    assert log["steps"][0]["tokens"] == 42
    assert log["steps"][0]["prompt_tokens"] == 18
    assert log["steps"][0]["completion_tokens"] == 24
    assert log["steps"][0]["cached_prompt_tokens"] == 12
    assert log["steps"][0]["cost_usd"] == 0.000042
    assert log["steps"][0]["model"] == "gpt-4.1-mini"
    assert log["steps"][0]["prompt_version"] == "openai_responses"


def test_artifact_objects_empty_when_no_bucket(client) -> None:
    """Persistence is opt-in: with no configured bucket the worker emits nothing."""
    worker = _build_worker(client)  # default config → workflow_run_artifact_bucket == ""
    run = SimpleNamespace(workflow_run_id="WR-2", workflow_id="WF-2", workflow_version_id="WFV-2")
    assert worker._artifact_objects(run, _artifact_result()) == []


def test_object_store_client_resolves_secrets_and_caches_boto_client(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _build_worker(client)
    worker._config = worker._config.model_copy(
        update={
            "object_store_endpoint_url": "http://minio.internal:9000",
            "object_store_region": "us-test-1",
            "object_store_force_path_style": True,
            "object_store_access_key_source": "secret://object-store/access",
            "object_store_secret_key_source": "secret://object-store/secret",
        }
    )
    resolved_sources: list[str] = []
    boto_calls: list[tuple[str, dict[str, object]]] = []
    sentinel = object()

    def _resolve_secret(source: str) -> str:
        resolved_sources.append(source)
        return {
            "secret://object-store/access": "ACCESS-123",
            "secret://object-store/secret": "SECRET-456",
        }[source]

    class _FakeConfig:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    fake_boto3 = types.ModuleType("boto3")

    def _client(service_name: str, **kwargs):
        boto_calls.append((service_name, dict(kwargs)))
        return sentinel

    fake_boto3.client = _client
    fake_botocore = types.ModuleType("botocore")
    fake_botocore_config = types.ModuleType("botocore.config")
    fake_botocore_config.Config = _FakeConfig
    fake_botocore.config = fake_botocore_config

    monkeypatch.setattr("caliber.orchestrator.workflow_run_worker.resolve_secret", _resolve_secret)
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setitem(sys.modules, "botocore", fake_botocore)
    monkeypatch.setitem(sys.modules, "botocore.config", fake_botocore_config)

    first = worker._object_store_client()
    second = worker._object_store_client()

    assert first is sentinel
    assert second is sentinel
    assert resolved_sources == [
        "secret://object-store/access",
        "secret://object-store/secret",
    ]
    assert len(boto_calls) == 1
    service_name, kwargs = boto_calls[0]
    assert service_name == "s3"
    assert kwargs["endpoint_url"] == "http://minio.internal:9000"
    assert kwargs["region_name"] == "us-test-1"
    assert kwargs["aws_access_key_id"] == "ACCESS-123"
    assert kwargs["aws_secret_access_key"] == "SECRET-456"
    config = kwargs["config"]
    assert isinstance(config, _FakeConfig)
    assert config.kwargs == {"s3": {"addressing_style": "path"}}


def test_persist_run_artifacts_uploads_all_objects_to_configured_bucket(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _build_worker(client)
    worker._config = worker._config.model_copy(
        update={
            "workflow_run_artifact_bucket": "caliber-suite",
            "workflow_run_artifact_prefix": "pipeline",
            "workflow_run_log_prefix": "logs",
        }
    )
    uploaded: list[dict[str, object]] = []

    class _FakeClient:
        def put_object(self, **kwargs) -> None:
            uploaded.append(dict(kwargs))

    monkeypatch.setattr(worker, "_object_store_client", lambda: _FakeClient())

    run = SimpleNamespace(workflow_run_id="WR-3", workflow_id="WF-3", workflow_version_id="WFV-3")
    worker._persist_run_artifacts(run, _artifact_result())

    assert [item["Key"] for item in uploaded] == [
        "pipeline/WR-3/kg.json",
        "pipeline/WR-3/report.html",
        "logs/WR-3.json",
    ]
    assert all(item["Bucket"] == "caliber-suite" for item in uploaded)
    assert uploaded[0]["ContentType"] == "application/json"
    assert uploaded[1]["ContentType"] == "text/html"
    assert uploaded[2]["ContentType"] == "application/json"


def test_persist_run_artifacts_logs_warning_when_upload_fails(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _build_worker(client)
    worker._config = worker._config.model_copy(
        update={
            "workflow_run_artifact_bucket": "caliber-suite",
            "workflow_run_artifact_prefix": "pipeline",
            "workflow_run_log_prefix": "logs",
        }
    )

    class _FailingClient:
        def put_object(self, **kwargs) -> None:
            del kwargs
            raise RuntimeError("object store offline")

    monkeypatch.setattr(worker, "_object_store_client", lambda: _FailingClient())
    captured: dict[str, object] = {}

    def _warning(message: str, *args: object, **kwargs: object) -> None:
        rendered = message % args if args else message
        captured["message"] = rendered
        captured["kwargs"] = dict(kwargs)

    monkeypatch.setattr(workflow_run_worker_module.logger, "warning", _warning)

    run = SimpleNamespace(workflow_run_id="WR-4", workflow_id="WF-4", workflow_version_id="WFV-4")
    worker._persist_run_artifacts(run, _artifact_result())

    assert captured["message"] == (
        "failed to persist run artifacts for WR-4 before storing any of the 3 planned "
        "object(s) while uploading pipeline/WR-4/kg.json"
    )
    assert captured["kwargs"] == {"exc_info": True}


def test_worker_records_successful_artifact_persistence_in_run_summary(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_queue(client)
    client.app.state.config = client.app.state.config.model_copy(
        update={
            "workflow_run_artifact_bucket": "caliber-suite",
            "workflow_run_artifact_prefix": "pipeline",
            "workflow_run_log_prefix": "logs",
        }
    )
    workflow_id = "artifact-persistence-success-worker-wf"
    manifest = _artifact_emitting_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Artifact Persistence Success Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "emit artifacts"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    uploaded: list[dict[str, object]] = []

    class _CapturingClient:
        def put_object(self, **kwargs) -> None:
            uploaded.append(dict(kwargs))

    worker = _build_worker(client)
    monkeypatch.setattr(worker, "_object_store_client", lambda: _CapturingClient())
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.summary is not None
        assert run.summary["output"] == "artifact-ready"
        artifact_persistence = dict(run.summary.get("artifact_persistence") or {})
        assert artifact_persistence == {
            "status": "persisted",
            "bucket": "caliber-suite",
            "object_count": 3,
            "persisted_object_count": 3,
            "recent_persisted_keys": [
                f"pipeline/{run_id}/kg.json",
                f"pipeline/{run_id}/report.html",
                f"logs/{run_id}.json",
            ],
            "artifact_names": ["kg.json", "report.html"],
        }
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.completed"

    assert {item["Key"] for item in uploaded} == {
        f"pipeline/{run_id}/kg.json",
        f"pipeline/{run_id}/report.html",
        f"logs/{run_id}.json",
    }


def test_worker_records_failed_artifact_persistence_in_run_summary(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_queue(client)
    client.app.state.config = client.app.state.config.model_copy(
        update={
            "workflow_run_artifact_bucket": "caliber-suite",
            "workflow_run_artifact_prefix": "pipeline",
            "workflow_run_log_prefix": "logs",
        }
    )
    workflow_id = "artifact-persistence-failure-worker-wf"
    manifest = _artifact_emitting_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Artifact Persistence Failure Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "emit artifacts"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    class _FailingClient:
        def put_object(self, **kwargs) -> None:
            del kwargs
            raise RuntimeError("object store offline")

    captured: dict[str, object] = {}

    def _warning(message: str, *args: object, **kwargs: object) -> None:
        rendered = message % args if args else message
        captured["message"] = rendered
        captured["kwargs"] = dict(kwargs)

    worker = _build_worker(client)
    monkeypatch.setattr(worker, "_object_store_client", lambda: _FailingClient())
    monkeypatch.setattr(workflow_run_worker_module.logger, "warning", _warning)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.summary is not None
        assert run.summary["output"] == "artifact-ready"
        artifact_persistence = dict(run.summary.get("artifact_persistence") or {})
        assert artifact_persistence["status"] == "failed"
        assert artifact_persistence["bucket"] == "caliber-suite"
        assert artifact_persistence["object_count"] == 3
        assert artifact_persistence["persisted_object_count"] == 0
        assert artifact_persistence["recent_persisted_keys"] == []
        assert artifact_persistence["artifact_names"] == ["kg.json", "report.html"]
        assert artifact_persistence["failed_object_key"] == f"pipeline/{run_id}/kg.json"
        assert artifact_persistence["error"] == (
            "RuntimeError: object store offline while uploading "
            f"pipeline/{run_id}/kg.json before storing any of the 3 planned object(s)"
        )
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.completed"

    assert captured["message"] == (
        f"failed to persist run artifacts for {run_id} before storing any of the 3 planned "
        f"object(s) while uploading pipeline/{run_id}/kg.json"
    )
    assert captured["kwargs"] == {"exc_info": True}


def test_worker_records_partial_artifact_persistence_progress_in_run_summary(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_queue(client)
    client.app.state.config = client.app.state.config.model_copy(
        update={
            "workflow_run_artifact_bucket": "caliber-suite",
            "workflow_run_artifact_prefix": "pipeline",
            "workflow_run_log_prefix": "logs",
        }
    )
    workflow_id = "artifact-persistence-partial-worker-wf"
    manifest = _artifact_emitting_manifest(workflow_id)
    _wid, vid = create_and_publish(
        client,
        workflow_name="Artifact Persistence Partial Worker",
        manifest=manifest,
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "emit artifacts"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    uploaded: list[str] = []

    class _PartiallyFailingClient:
        def put_object(self, **kwargs) -> None:
            key = str(kwargs["Key"])
            if key.endswith("report.html"):
                raise RuntimeError("object store offline")
            uploaded.append(key)

    captured: dict[str, object] = {}

    def _warning(message: str, *args: object, **kwargs: object) -> None:
        rendered = message % args if args else message
        captured["message"] = rendered
        captured["kwargs"] = dict(kwargs)

    worker = _build_worker(client)
    monkeypatch.setattr(worker, "_object_store_client", lambda: _PartiallyFailingClient())
    monkeypatch.setattr(workflow_run_worker_module.logger, "warning", _warning)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.summary is not None
        artifact_persistence = dict(run.summary.get("artifact_persistence") or {})
        assert artifact_persistence["status"] == "failed"
        assert artifact_persistence["bucket"] == "caliber-suite"
        assert artifact_persistence["object_count"] == 3
        assert artifact_persistence["persisted_object_count"] == 1
        assert artifact_persistence["artifact_names"] == ["kg.json", "report.html"]
        assert artifact_persistence["recent_persisted_keys"] == [f"pipeline/{run_id}/kg.json"]
        assert artifact_persistence["failed_object_key"] == f"pipeline/{run_id}/report.html"
        assert artifact_persistence["error"] == (
            "RuntimeError: object store offline while uploading "
            f"pipeline/{run_id}/report.html after storing 1 of 3 object(s)"
        )

    assert uploaded == [f"pipeline/{run_id}/kg.json"]
    assert captured["message"] == (
        f"failed to persist run artifacts for {run_id} after storing 1 of 3 object(s) "
        f"(pipeline/{run_id}/kg.json) while uploading pipeline/{run_id}/report.html"
    )
    assert captured["kwargs"] == {"exc_info": True}


def test_resume_checkpoint_row_ignores_missing_or_foreign_checkpoint_reference(client) -> None:
    _enable_queue(client)
    wid, vid = create_and_publish(client)
    first = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "workflow_id": wid, "input": "first"},
    )
    second = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "workflow_id": wid, "input": "second"},
    )
    assert first.status_code == 202
    assert second.status_code == 202
    first_run_id = first.json()["data"]["workflow_run_id"]
    second_run_id = second.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    with client.app.state.session_factory() as session:
        first_run = session.get(CaliberWorkflowRun, first_run_id)
        second_run = session.get(CaliberWorkflowRun, second_run_id)
        assert first_run is not None
        assert second_run is not None
        assert worker._resume_checkpoint_row(session, first_run) is None

        checkpoint = CaliberWorkflowRunCheckpoint(
            checkpoint_id="WRC-foreign",
            workflow_run_id=second_run_id,
            project_id=second_run.project_id,
            sequence=1,
            node_id="wait_gate",
            state_blob={"kind": "wait_for_event", "node_id": "wait_gate"},
        )
        session.add(checkpoint)
        first_run.summary = {"resume_checkpoint_id": "WRC-foreign"}
        session.commit()

        refreshed = session.get(CaliberWorkflowRun, first_run_id)
        assert refreshed is not None
        assert worker._resume_checkpoint_row(session, refreshed) is None


@pytest.mark.parametrize("kind", ["wait_for_event", "wait_until"])
def test_resume_checkpoint_for_wait_nodes_preserves_inputs_and_skips_output_replay(
    client,
    kind: str,
) -> None:
    _enable_queue(client)
    wid, vid = create_and_publish(client)
    original = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "workflow_id": wid, "input": "parent"},
    )
    retry = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "workflow_id": wid, "input": "retry"},
    )
    assert original.status_code == 202
    assert retry.status_code == 202
    original_run_id = original.json()["data"]["workflow_run_id"]
    retry_run_id = retry.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    with client.app.state.session_factory() as session:
        retry_run = session.get(CaliberWorkflowRun, retry_run_id)
        original_run = session.get(CaliberWorkflowRun, original_run_id)
        assert retry_run is not None
        assert original_run is not None
        retry_run.parent_run_id = original_run_id
        retry_run.summary = {
            "resume_checkpoint_id": "WRC-wait-node",
            "resume_checkpoint_run_id": original_run_id,
        }
        state_blob = {
            "kind": kind,
            "node_id": "wait_gate",
            "output": "stale output",
            "output_by_port": {"output": "stale output"},
            "input_by_port": {"input": "retry"},
            "resume_event_inputs": {
                "event_name": "ticket.approved",
                "event_payload": {"ticket_id": "T-42"},
            },
        }
        if kind == "wait_for_event":
            state_blob["expected_event_name"] = "ticket.approved"
        session.add(
            CaliberWorkflowRunCheckpoint(
                checkpoint_id="WRC-wait-node",
                workflow_run_id=original_run_id,
                project_id=retry_run.project_id,
                sequence=1,
                node_id="wait_gate",
                state_blob=state_blob,
            )
        )
        session.commit()

        refreshed = session.get(CaliberWorkflowRun, retry_run_id)
        assert refreshed is not None
        checkpoint = worker._resume_checkpoint(session, refreshed)

    assert checkpoint is not None
    assert checkpoint.node_id == "wait_gate"
    assert checkpoint.output == "stale output"
    assert checkpoint.output_by_port == {"output": "stale output"}
    assert checkpoint.input_by_port == {"input": "retry"}
    assert checkpoint.injected_inputs == {
        "event_name": "ticket.approved",
        "event_payload": {"ticket_id": "T-42"},
    }
    assert checkpoint.replay_output is False


def test_resume_checkpoint_for_human_approval_preserves_inputs_and_skips_output_replay(
    client,
) -> None:
    _enable_queue(client)
    wid, vid = create_and_publish(client)
    original = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "workflow_id": wid, "input": "parent"},
    )
    retry = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "workflow_id": wid, "input": "retry"},
    )
    assert original.status_code == 202
    assert retry.status_code == 202
    original_run_id = original.json()["data"]["workflow_run_id"]
    retry_run_id = retry.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    with client.app.state.session_factory() as session:
        retry_run = session.get(CaliberWorkflowRun, retry_run_id)
        original_run = session.get(CaliberWorkflowRun, original_run_id)
        assert retry_run is not None
        assert original_run is not None
        retry_run.parent_run_id = original_run_id
        retry_run.summary = {
            "resume_checkpoint_id": "WRC-human-approval",
            "resume_checkpoint_run_id": original_run_id,
        }
        session.add(
            CaliberWorkflowRunCheckpoint(
                checkpoint_id="WRC-human-approval",
                workflow_run_id=original_run_id,
                project_id=retry_run.project_id,
                sequence=1,
                node_id="human_gate",
                state_blob={
                    "kind": "human_approval",
                    "node_id": "human_gate",
                    "output": "approved output",
                    "output_by_port": {"request": "approved output"},
                    "input_by_port": {"request": "retry"},
                },
            )
        )
        session.commit()

        refreshed = session.get(CaliberWorkflowRun, retry_run_id)
        assert refreshed is not None
        checkpoint = worker._resume_checkpoint(session, refreshed)

    assert checkpoint is not None
    assert checkpoint.node_id == "human_gate"
    assert checkpoint.output == "approved output"
    assert checkpoint.output_by_port == {"request": "approved output"}
    assert checkpoint.input_by_port == {"request": "retry"}
    assert checkpoint.injected_inputs is None
    assert checkpoint.replay_output is False


@pytest.mark.parametrize(
    "kind", ["wait_for_event", "wait_until", "runtime_approval", "human_approval"]
)
def test_resume_checkpoint_rejects_unsafe_resume_boundaries_without_input_snapshot(
    client,
    kind: str,
) -> None:
    _enable_queue(client)
    wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "workflow_id": wid, "input": "hello"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        run.summary = {
            "resume_checkpoint_id": "WRC-unsafe-missing-input",
            "resume_checkpoint_run_id": run.workflow_run_id,
        }
        session.add(
            CaliberWorkflowRunCheckpoint(
                checkpoint_id="WRC-unsafe-missing-input",
                workflow_run_id=run.workflow_run_id,
                project_id=run.project_id,
                sequence=1,
                node_id="wait_gate",
                state_blob={
                    "kind": kind,
                    "node_id": "wait_gate",
                    "output": "stale output",
                    "output_by_port": {"output": "stale output"},
                    "input_by_port": ["not", "a", "dict"],
                    "resume_event_inputs": {"event_payload": {"ticket_id": "T-42"}},
                },
            )
        )
        session.commit()

        refreshed = session.get(CaliberWorkflowRun, run_id)
        assert refreshed is not None
        checkpoint = worker._resume_checkpoint(session, refreshed)

    assert checkpoint is None


def test_resume_checkpoint_rejects_wait_for_event_without_resume_payload(client) -> None:
    _enable_queue(client)
    wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "workflow_id": wid, "input": "hello"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        run.summary = {
            "resume_checkpoint_id": "WRC-wait-no-event-payload",
            "resume_checkpoint_run_id": run.workflow_run_id,
        }
        session.add(
            CaliberWorkflowRunCheckpoint(
                checkpoint_id="WRC-wait-no-event-payload",
                workflow_run_id=run.workflow_run_id,
                project_id=run.project_id,
                sequence=1,
                node_id="wait_gate",
                state_blob={
                    "kind": "wait_for_event",
                    "node_id": "wait_gate",
                    "output": "stale output",
                    "input_by_port": {"input": "hello"},
                    "expected_event_name": "ticket.approved",
                    "resume_event_inputs": {"event_name": "ticket.approved"},
                },
            )
        )
        session.commit()

        refreshed = session.get(CaliberWorkflowRun, run_id)
        assert refreshed is not None
        checkpoint = worker._resume_checkpoint(session, refreshed)

    assert checkpoint is None


@pytest.mark.parametrize(
    ("expected_event_name", "resume_event_name"),
    [
        (None, "ticket.approved"),
        ("ticket.approved", "ticket.denied"),
    ],
)
def test_resume_checkpoint_rejects_wait_for_event_with_missing_or_mismatched_event_contract(
    client,
    expected_event_name: str | None,
    resume_event_name: str,
) -> None:
    _enable_queue(client)
    wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "workflow_id": wid, "input": "hello"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        run.summary = {
            "resume_checkpoint_id": "WRC-wait-event-contract-invalid",
            "resume_checkpoint_run_id": run.workflow_run_id,
        }
        state_blob: dict[str, object] = {
            "kind": "wait_for_event",
            "node_id": "wait_gate",
            "output": "stale output",
            "input_by_port": {"input": "hello"},
            "resume_event_inputs": {
                "event_name": resume_event_name,
                "event_payload": {"ticket_id": "T-42"},
            },
        }
        if expected_event_name is not None:
            state_blob["expected_event_name"] = expected_event_name
        session.add(
            CaliberWorkflowRunCheckpoint(
                checkpoint_id="WRC-wait-event-contract-invalid",
                workflow_run_id=run.workflow_run_id,
                project_id=run.project_id,
                sequence=1,
                node_id="wait_gate",
                state_blob=state_blob,
            )
        )
        session.commit()

        refreshed = session.get(CaliberWorkflowRun, run_id)
        assert refreshed is not None
        checkpoint = worker._resume_checkpoint(session, refreshed)

    assert checkpoint is None


def test_resume_checkpoint_normalizes_generic_checkpoint_payloads(client) -> None:
    _enable_queue(client)
    wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "workflow_id": wid, "input": "hello"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        run.summary = {"resume_checkpoint_id": "WRC-generic"}
        session.add(
            CaliberWorkflowRunCheckpoint(
                checkpoint_id="WRC-generic",
                workflow_run_id=run_id,
                project_id=run.project_id,
                sequence=1,
                node_id="human_gate",
                state_blob={
                    "kind": "generic_checkpoint",
                    "node_id": "human_gate",
                    "output": 123,
                    "output_by_port": "invalid",
                    "input_by_port": ["not", "a", "dict"],
                    "resume_event_inputs": ["bad"],
                },
            )
        )
        session.commit()

        refreshed = session.get(CaliberWorkflowRun, run_id)
        assert refreshed is not None
        checkpoint = worker._resume_checkpoint(session, refreshed)

    assert checkpoint is not None
    assert checkpoint.node_id == "human_gate"
    assert checkpoint.output == "123"
    assert checkpoint.output_by_port is None
    assert checkpoint.input_by_port is None
    assert checkpoint.injected_inputs is None
    assert checkpoint.replay_output is True


def test_resume_checkpoint_rejects_unrelated_or_corrupt_state(client) -> None:
    _enable_queue(client)
    wid, vid = create_and_publish(client)
    original = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "workflow_id": wid, "input": "original"},
    )
    retry = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "workflow_id": wid, "input": "retry"},
    )
    unrelated = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "workflow_id": wid, "input": "other"},
    )
    assert original.status_code == 202
    assert retry.status_code == 202
    assert unrelated.status_code == 202
    original_run_id = original.json()["data"]["workflow_run_id"]
    retry_run_id = retry.json()["data"]["workflow_run_id"]
    unrelated_run_id = unrelated.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    with client.app.state.session_factory() as session:
        retry_run = session.get(CaliberWorkflowRun, retry_run_id)
        assert retry_run is not None
        retry_run.parent_run_id = original_run_id
        retry_run.summary = {
            "resume_checkpoint_id": "WRC-unrelated",
            "resume_checkpoint_run_id": unrelated_run_id,
        }
        session.add(
            CaliberWorkflowRunCheckpoint(
                checkpoint_id="WRC-unrelated",
                workflow_run_id=unrelated_run_id,
                project_id=retry_run.project_id,
                sequence=1,
                node_id="wait_gate",
                state_blob={"kind": "wait_for_event", "node_id": "wait_gate"},
            )
        )
        session.commit()

        refreshed = session.get(CaliberWorkflowRun, retry_run_id)
        assert refreshed is not None
        assert worker._resume_checkpoint(session, refreshed) is None

        refreshed.summary = {
            "resume_checkpoint_id": "WRC-corrupt",
            "resume_checkpoint_run_id": refreshed.workflow_run_id,
        }
        session.add(
            CaliberWorkflowRunCheckpoint(
                checkpoint_id="WRC-corrupt",
                workflow_run_id=refreshed.workflow_run_id,
                project_id=refreshed.project_id,
                sequence=2,
                node_id="wait_gate",
                state_blob={"kind": "wait_for_event", "node_id": ""},
            )
        )
        session.commit()

        final = session.get(CaliberWorkflowRun, retry_run_id)
        assert final is not None
        assert worker._resume_checkpoint(session, final) is None


@pytest.mark.parametrize(
    ("checkpoint_node_id", "state_node_id"),
    [
        ("", "wait_gate"),
        ("other_gate", "wait_gate"),
    ],
)
def test_resume_checkpoint_rejects_mismatched_checkpoint_node_identity(
    client,
    checkpoint_node_id: str,
    state_node_id: str,
) -> None:
    _enable_queue(client)
    wid, vid = create_and_publish(client)
    original = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "workflow_id": wid, "input": "original"},
    )
    retry = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "workflow_id": wid, "input": "retry"},
    )
    assert original.status_code == 202
    assert retry.status_code == 202
    original_run_id = original.json()["data"]["workflow_run_id"]
    retry_run_id = retry.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    with client.app.state.session_factory() as session:
        retry_run = session.get(CaliberWorkflowRun, retry_run_id)
        assert retry_run is not None
        retry_run.parent_run_id = original_run_id
        retry_run.summary = {
            "resume_checkpoint_id": "WRC-mismatched-node-identity",
            "resume_checkpoint_run_id": original_run_id,
        }
        session.add(
            CaliberWorkflowRunCheckpoint(
                checkpoint_id="WRC-mismatched-node-identity",
                workflow_run_id=original_run_id,
                project_id=retry_run.project_id,
                sequence=1,
                node_id=checkpoint_node_id,
                state_blob={
                    "kind": "wait_until",
                    "node_id": state_node_id,
                    "output": "stale output",
                    "input_by_port": {"input": "retry"},
                    "resume_at": "2026-06-16T12:00:00Z",
                },
            )
        )
        session.commit()

        refreshed = session.get(CaliberWorkflowRun, retry_run_id)
        assert refreshed is not None
        assert worker._resume_checkpoint(session, refreshed) is None


def test_worker_marks_run_cancelled_when_runtime_requests_cancellation(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_queue(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "cancel me"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    worker = _build_worker(client)
    monkeypatch.setattr(worker, "_cancel_requested", lambda session, rid: rid == run_id)

    def _fake_execute(
        plan,
        input_text: str,
        *,
        executor,
        session_id=None,
        preview=False,
        on_step=None,
        on_node_start=None,
        runtime_approvals_enabled=False,
        approved_human_approval_nodes=None,
        resume_checkpoint=None,
        extra_tools=None,
    ) -> WorkflowRunResult:
        del plan, executor, session_id, preview, runtime_approvals_enabled
        del approved_human_approval_nodes, resume_checkpoint, extra_tools
        if on_node_start is not None:
            on_node_start(
                "support_agent",
                SimpleNamespace(node_type="agent"),
                {"input": input_text},
            )
        step = NodeStep(
            "support_agent",
            "agent",
            "ok",
            output=f"processing {input_text}",
            input_by_port={"input": input_text},
            output_by_port={"final_output": f"processing {input_text}"},
        )
        try:
            assert on_step is not None
            on_step(step)
        except RuntimeError as exc:
            return WorkflowRunResult(
                status="error",
                output="",
                error=str(exc),
                steps=[step],
                tokens=3,
            )
        raise AssertionError("cancellation sentinel was expected")

    monkeypatch.setattr("caliber.orchestrator.workflow_run_worker.execute", _fake_execute)

    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "cancelled"
        assert run.error_code == "cancelled"
        assert run.error_summary == "cancelled by operator"
        assert run.completed_at is not None
        assert run.summary is not None
        assert run.summary["input"] == "cancel me"
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.cancelled"


@pytest.mark.parametrize(
    ("remove_kind", "expected_summary"),
    [
        ("workflow", "workflow not found"),
        ("version", "workflow version not found"),
    ],
)
def test_worker_marks_run_failed_when_dependencies_are_missing(
    client,
    remove_kind: str,
    expected_summary: str,
) -> None:
    _enable_queue(client)
    wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "workflow_id": wid, "input": "hello"},
    )
    assert created.status_code == 202
    run_id = created.json()["data"]["workflow_run_id"]

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        if remove_kind == "workflow":
            workflow = session.get(CaliberWorkflow, wid)
            assert workflow is not None
            session.delete(workflow)
        else:
            version = session.get(CaliberWorkflowVersion, vid)
            assert version is not None
            run.manifest_snapshot = None
            session.delete(version)
        session.commit()

    worker = _build_worker(client)
    worker._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.error_code == "missing_dependencies"
        assert run.error_summary == expected_summary
        assert run.completed_at is not None
        events = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .order_by(CaliberWorkflowRunEvent.sequence.asc())
            .all()
        )
        assert events[-1].event_type == "workflow.run.failed"
        assert events[-1].payload == {"status": "failed", "error": expected_summary}
