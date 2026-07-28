"""Manifest model tests (plan §19.2)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from caliber.workflows.manifest import (
    FileInputNode,
    FolderInputNode,
    GuardrailNode,
    McpResourceNode,
    PythonCodeNode,
    UnsupportedSchemaVersionError,
    WorkflowManifest,
    compute_manifest_hash,
    parse_manifest,
)
from tests.workflow_helpers import make_manifest


def test_valid_minimal_manifest_parses() -> None:
    manifest = parse_manifest(make_manifest())
    assert isinstance(manifest, WorkflowManifest)
    assert manifest.workflow_id == "test_wf"
    assert set(manifest.nodes) == {"start", "agent", "final"}


def test_file_input_accepts_one_content_pinned_managed_ref() -> None:
    data = make_manifest()
    data["nodes"]["file_input"] = {
        "id": "file_input",
        "type": "file_input",
        "file_ref": {
            "file_id": "FILE-1",
            "file_ref": "caliber://projects/PRJ-1/input/source.md",
            "sha256": "a" * 64,
            "name": "source.md",
            "size_bytes": 12,
            "media_type": "text/markdown",
            "object_version_id": "v1",
        },
    }
    node = parse_manifest(data).nodes["file_input"]
    assert isinstance(node, FileInputNode)
    assert node.file_ref is not None
    assert node.file_ref.sha256 == "a" * 64
    assert node.outputs["file_ref"].type == "structured"


def test_file_input_rejects_managed_ref_plus_legacy_path() -> None:
    data = make_manifest()
    data["nodes"]["file_input"] = {
        "id": "file_input",
        "type": "file_input",
        "path": "/tmp/source.md",
        "file_ref": {
            "file_id": "FILE-1",
            "file_ref": "caliber://projects/PRJ-1/input/source.md",
            "sha256": "a" * 64,
            "name": "source.md",
            "size_bytes": 12,
        },
    }
    with pytest.raises(ValidationError, match="either file_ref or path"):
        parse_manifest(data)


def test_missing_schema_version_rejected() -> None:
    data = make_manifest()
    del data["schema_version"]
    with pytest.raises((ValidationError, UnsupportedSchemaVersionError)):
        parse_manifest(data)


def test_missing_workflow_id_rejected() -> None:
    data = make_manifest()
    del data["workflow_id"]
    with pytest.raises(ValidationError):
        parse_manifest(data)


def test_unknown_node_type_rejected() -> None:
    data = make_manifest()
    data["nodes"]["weird"] = {"id": "weird", "type": "unknown"}
    with pytest.raises(ValidationError):
        parse_manifest(data)


def test_node_key_id_mismatch_rejected() -> None:
    data = make_manifest()
    data["nodes"]["agent"]["id"] = "different"
    with pytest.raises(ValidationError):
        parse_manifest(data)


def test_duplicate_edge_ids_rejected() -> None:
    data = make_manifest()
    data["edges"].append({"id": "e1", "from": "start", "to": "final", "map": {"msg": "response"}})
    with pytest.raises(ValidationError):
        parse_manifest(data)


def test_empty_nodes_rejected() -> None:
    data = make_manifest(nodes={}, edges=[])
    with pytest.raises(ValidationError):
        parse_manifest(data)


def test_agent_requires_model_and_instructions() -> None:
    data = make_manifest()
    del data["nodes"]["agent"]["model"]
    with pytest.raises(ValidationError):
        parse_manifest(data)
    data = make_manifest()
    del data["nodes"]["agent"]["instructions"]
    with pytest.raises(ValidationError):
        parse_manifest(data)


def test_version_constraint_parses() -> None:
    data = make_manifest(
        tools={"t": {"registry_ref": "tool.x.v1", "version_constraint": ">=1.0,<2.0"}}
    )
    manifest = parse_manifest(data)
    assert manifest.tools["t"].version_constraint == ">=1.0,<2.0"


def test_invalid_version_constraint_rejected() -> None:
    data = make_manifest(
        tools={"t": {"registry_ref": "tool.x.v1", "version_constraint": "not_a_version"}}
    )
    with pytest.raises(ValidationError):
        parse_manifest(data)


def test_legacy_tool_binding_defaults_to_registered_function() -> None:
    data = make_manifest(
        tools={"t": {"registry_ref": "tool.lookup_policy.v1", "version_constraint": ">=1.0,<2.0"}}
    )
    manifest = parse_manifest(data)
    binding = manifest.tools["t"]
    assert binding.type == "registered_function"


def test_mcp_tool_binding_parses() -> None:
    data = make_manifest(
        tools={
            "customer_lookup": {
                "type": "mcp_tool",
                "server_id": "MCP-DB-PROD",
                "tool_name": "postgres.query",
                "side_effect_level": "read",
                "requires_approval": False,
            }
        }
    )
    manifest = parse_manifest(data)
    binding = manifest.tools["customer_lookup"]
    assert binding.type == "mcp_tool"


def test_deploy_gates_parse_and_are_not_nodes() -> None:
    data = make_manifest(
        artifacts={"eval_datasets": {"d": {"dataset_name": "ds"}}},
        deploy_gates={
            "g": {
                "type": "deploy_gate",
                "dataset_ref": "d",
                "required_for_aliases": ["prod"],
                "thresholds": {"min_overall_delta": 0.02},
            }
        },
    )
    manifest = parse_manifest(data)
    assert "g" in manifest.deploy_gates
    assert "g" not in manifest.nodes


def test_manifest_round_trips() -> None:
    manifest = parse_manifest(make_manifest())
    again = parse_manifest(manifest.to_dict())
    assert again.to_dict() == manifest.to_dict()


def test_manifest_hash_is_deterministic_and_key_order_independent() -> None:
    data = make_manifest()
    shuffled = {k: data[k] for k in reversed(list(data))}
    assert compute_manifest_hash(data) == compute_manifest_hash(shuffled)


def test_manifest_hash_changes_on_content_change() -> None:
    a = compute_manifest_hash(make_manifest())
    b = compute_manifest_hash(make_manifest(name="Different Name"))
    assert a != b


def test_parsed_hash_matches_raw_dict_hash() -> None:
    data = make_manifest()
    assert parse_manifest(data).manifest_hash() == compute_manifest_hash(data)


def test_schema_version_2_rejected() -> None:
    data = make_manifest()
    data["schema_version"] = 2
    with pytest.raises(UnsupportedSchemaVersionError):
        parse_manifest(data)


def test_runtime_pinned_only() -> None:
    data = make_manifest()
    data["runtime"]["sdk_version_policy"] = "manifest-pinned"
    with pytest.raises(ValidationError):
        parse_manifest(data)


def test_runtime_openai_execution_overrides_parse() -> None:
    data = make_manifest()
    data["runtime"]["openai"] = {
        "workflow_api": "responses",
        "parallel_tool_calls": "enabled",
        "prompt_cache_mode": "auto",
        "prompt_cache_retention": "24h",
    }
    manifest = parse_manifest(data)
    assert manifest.runtime.openai is not None
    assert manifest.runtime.openai.workflow_api == "responses"
    assert manifest.runtime.openai.parallel_tool_calls == "enabled"
    assert manifest.runtime.openai.prompt_cache_mode == "auto"
    assert manifest.runtime.openai.prompt_cache_retention == "24h"


def test_runtime_openai_invalid_override_rejected() -> None:
    data = make_manifest()
    data["runtime"]["openai"] = {"workflow_api": "realtime"}
    with pytest.raises(ValidationError):
        parse_manifest(data)


def test_file_and_folder_input_nodes_parse() -> None:
    data = make_manifest()
    data["nodes"]["file_input"] = {
        "id": "file_input",
        "type": "file_input",
        "path": "",
        "max_bytes": 1234,
    }
    data["nodes"]["folder_input"] = {
        "id": "folder_input",
        "type": "folder_input",
        "pattern": "*.md",
        "recursive": False,
        "max_files": 5,
        "max_bytes_per_file": 4096,
    }

    manifest = parse_manifest(data)

    assert isinstance(manifest.nodes["file_input"], FileInputNode)
    assert manifest.nodes["file_input"].outputs["text"].type == "string"
    assert isinstance(manifest.nodes["folder_input"], FolderInputNode)
    assert manifest.nodes["folder_input"].outputs["files"].type == "structured"


def test_mcp_resource_node_parses() -> None:
    data = make_manifest()
    data["nodes"]["mcp_lookup"] = {
        "id": "mcp_lookup",
        "type": "mcp_resource",
        "server_id": "MCP-DOCS",
        "tool_name": "search_docs",
        "timeout_seconds": 30,
    }

    manifest = parse_manifest(data)

    assert isinstance(manifest.nodes["mcp_lookup"], McpResourceNode)
    assert manifest.nodes["mcp_lookup"].outputs["result"].type == "structured"


def test_python_code_node_parses() -> None:
    data = make_manifest()
    data["nodes"]["python"] = {
        "id": "python",
        "type": "python_code",
        "code": 'return {"text": input.upper(), "result": {"length": len(input)}}',
        "timeout_seconds": 10,
    }

    manifest = parse_manifest(data)

    assert isinstance(manifest.nodes["python"], PythonCodeNode)
    assert manifest.nodes["python"].inputs["context"].type == "structured"
    assert manifest.nodes["python"].outputs["text"].type == "string"


def test_knowledge_query_node_allows_empty_retrieval_modes_for_kb_default() -> None:
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

    manifest = parse_manifest(data)
    node = manifest.nodes["knowledge"]

    assert node.type == "knowledge_query"
    assert node.retrieval_modes == []


def test_knowledge_query_node_defaults_to_kb_default_when_modes_are_omitted() -> None:
    data = make_manifest()
    data["nodes"]["knowledge"] = {
        "id": "knowledge",
        "type": "knowledge_query",
        "knowledge_base_id": "KB-1",
        "version_ids": [],
        "top_k": 4,
    }

    manifest = parse_manifest(data)
    node = manifest.nodes["knowledge"]

    assert node.type == "knowledge_query"
    assert node.retrieval_modes == []


def test_knowledge_query_node_exposes_runtime_retrieval_modes_input() -> None:
    data = make_manifest()
    data["nodes"]["knowledge"] = {
        "id": "knowledge",
        "type": "knowledge_query",
        "knowledge_base_id": "KB-1",
        "top_k": 4,
    }

    manifest = parse_manifest(data)
    node = manifest.nodes["knowledge"]

    assert node.type == "knowledge_query"
    assert node.inputs["retrieval_modes"].type == "structured"


def test_knowledge_build_node_exposes_runtime_override_inputs() -> None:
    data = make_manifest()
    data["nodes"]["knowledge_build"] = {
        "id": "knowledge_build",
        "type": "knowledge_build",
        "knowledge_base_id": "KB-1",
        "chunking_strategy": "recursive",
        "embedding_model": "BAAI/bge-m3",
    }

    manifest = parse_manifest(data)
    node = manifest.nodes["knowledge_build"]

    assert node.type == "knowledge_build"
    assert node.inputs["input"].type == "string"
    assert node.inputs["sources"].type == "structured"
    assert node.inputs["chunking_strategy"].type == "string"
    assert node.inputs["embedding_model"].type == "string"
    assert node.outputs["version_id"].type == "string"
    assert node.outputs["run_id"].type == "string"


def test_guardrail_warn_failure_mode_parses() -> None:
    data = make_manifest()
    data["nodes"]["guard"] = {
        "id": "guard",
        "type": "guardrail",
        "checks": [{"non_empty_output": {}}],
        "on_failure": "warn",
    }

    manifest = parse_manifest(data)

    assert isinstance(manifest.nodes["guard"], GuardrailNode)
    assert manifest.nodes["guard"].on_failure == "warn"


def test_orchestration_nodes_parse() -> None:
    data = make_manifest()
    data["nodes"]["wait_until"] = {
        "id": "wait_until",
        "type": "wait_until",
        "wait_until": "2099-01-01T00:00:00Z",
    }
    data["nodes"]["wait_event"] = {
        "id": "wait_event",
        "type": "wait_for_event",
        "event_name": "ticket.approved",
    }
    data["nodes"]["for_each"] = {
        "id": "for_each",
        "type": "for_each",
        "target_node_id": "agent",
    }
    data["nodes"]["loop"] = {
        "id": "loop",
        "type": "loop",
        "target_node_id": "agent",
        "max_iterations": 5,
        "stop_condition": "iteration >= 3",
    }
    data["nodes"]["boundary"] = {
        "id": "boundary",
        "type": "error_boundary",
        "target_node_id": "agent",
    }
    data["nodes"]["subflow"] = {
        "id": "subflow",
        "type": "subworkflow",
        "workflow_id": "other_workflow",
    }
    data["nodes"]["parallel"] = {"id": "parallel", "type": "parallel"}
    data["nodes"]["join"] = {"id": "join", "type": "join"}
    manifest = parse_manifest(data)
    assert manifest.nodes["wait_until"].type == "wait_until"
    assert manifest.nodes["wait_event"].type == "wait_for_event"
    assert manifest.nodes["for_each"].type == "for_each"
    assert manifest.nodes["loop"].type == "loop"
    assert manifest.nodes["boundary"].type == "error_boundary"
    assert manifest.nodes["subflow"].type == "subworkflow"
