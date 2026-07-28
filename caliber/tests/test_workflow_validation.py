"""Graph validation tests (plan §19.3)."""

from __future__ import annotations

from caliber.workflows.manifest import parse_manifest
from caliber.workflows.validation import validate_manifest
from tests.workflow_helpers import make_manifest, make_support_manifest, registry_resolver


def _codes(report) -> list[str]:
    return [i.code for i in report.errors]


def _warning_codes(report) -> list[str]:
    return [i.code for i in report.warnings]


def _issue(report, code: str):
    return next(i for i in report.errors if i.code == code)


def test_valid_linear_graph() -> None:
    report = validate_manifest(parse_manifest(make_manifest()))
    assert report.valid


def test_no_start_node() -> None:
    data = make_manifest()
    del data["nodes"]["start"]
    data["edges"] = [
        {"id": "e2", "from": "agent", "to": "final", "map": {"final_output": "response"}}
    ]
    report = validate_manifest(parse_manifest(data))
    assert "no_start_node" in _codes(report)


def test_multiple_start_nodes() -> None:
    data = make_manifest()
    data["nodes"]["start2"] = {
        "id": "start2",
        "type": "start",
        "outputs": {"msg": {"type": "string"}},
    }
    report = validate_manifest(parse_manifest(data))
    assert "multiple_start_nodes" in _codes(report)


def test_no_output_node() -> None:
    data = make_manifest()
    del data["nodes"]["final"]
    data["edges"] = [{"id": "e1", "from": "start", "to": "agent", "map": {"msg": "input"}}]
    report = validate_manifest(parse_manifest(data))
    assert "no_output_node" in _codes(report)


def test_orphaned_node_warning() -> None:
    data = make_manifest()
    data["nodes"]["lonely"] = {
        "id": "lonely",
        "type": "agent",
        "name": "lonely",
        "model": "inherit",
        "instructions": {"type": "inline", "text": "x"},
        "inputs": {"input": {"type": "string"}},
        "outputs": {"final_output": {"type": "string"}},
    }
    report = validate_manifest(parse_manifest(data))
    assert report.valid  # orphan is a warning, not an error
    assert any(w.code == "orphaned_node" for w in report.warnings)


def test_one_way_handoff_valid() -> None:
    report = validate_manifest(parse_manifest(_handoff_manifest("b", "")))
    assert report.valid


def test_handoff_back_cycle_warns_but_remains_valid() -> None:
    report = validate_manifest(parse_manifest(_handoff_manifest("b", "a")))
    assert report.valid
    assert "handoff_cycle" not in _codes(report)
    assert "handoff_cycle" in _warning_codes(report)


def test_handoff_missing_target_points_to_indexed_target_field() -> None:
    data = make_manifest()
    data["nodes"]["agent"]["handoffs"] = [{"target": "NOPE"}]

    report = validate_manifest(parse_manifest(data))

    issue = _issue(report, "handoff_bad_target")
    assert issue.path == "nodes.agent.handoffs[0].target"
    assert "Handoff 1" in issue.message


def test_handoff_to_non_agent_points_to_indexed_target_field() -> None:
    data = make_manifest()
    data["nodes"]["agent"]["handoffs"] = [{"target": "final"}]

    report = validate_manifest(parse_manifest(data))

    issue = _issue(report, "handoff_non_agent")
    assert issue.path == "nodes.agent.handoffs[0].target"
    assert "Handoff 1" in issue.message


def test_self_handoff_reports_specific_error_without_generic_cycle() -> None:
    data = make_manifest()
    data["nodes"]["agent"]["handoffs"] = [{"target": "agent"}]

    report = validate_manifest(parse_manifest(data))

    issue = _issue(report, "handoff_self_target")
    assert issue.path == "nodes.agent.handoffs[0].target"
    assert "same agent" in issue.message
    assert "handoff_cycle" not in _codes(report)


def test_arbitrary_router_cycle_rejected() -> None:
    data = make_manifest()
    data["nodes"]["r1"] = {
        "id": "r1",
        "type": "router",
        "inputs": {"x": {"type": "string"}},
        "outputs": {"y": {"type": "string"}},
        "branches": [{"to": "r2"}],
    }
    data["nodes"]["r2"] = {
        "id": "r2",
        "type": "router",
        "inputs": {"x": {"type": "string"}},
        "outputs": {"y": {"type": "string"}},
        "branches": [{"to": "r1"}],
    }
    data["edges"].append({"id": "er1", "from": "r1", "to": "r2", "map": {"y": "x"}})
    data["edges"].append({"id": "er2", "from": "r2", "to": "r1", "map": {"y": "x"}})
    report = validate_manifest(parse_manifest(data))
    assert "arbitrary_cycle" in _codes(report)


def test_edge_type_mismatch() -> None:
    data = make_manifest()
    data["nodes"]["agent"]["inputs"] = {"input": {"type": "structured"}}
    report = validate_manifest(parse_manifest(data))
    assert "type_mismatch" in _codes(report)


def test_note_node_rejects_incoming_edges() -> None:
    data = make_manifest()
    data["nodes"]["note"] = {
        "id": "note",
        "type": "note",
        "text": "Operator-only context",
    }
    data["edges"].append(
        {"id": "e_start_note", "from": "start", "to": "note", "map": {"msg": "ignored"}}
    )

    report = validate_manifest(parse_manifest(data))

    issue = _issue(report, "map_bad_target_port")
    assert issue.path == "edges.e_start_note.map"
    assert "node 'note'" in issue.message


def test_note_node_rejects_outgoing_edges() -> None:
    data = make_manifest()
    data["nodes"]["note"] = {
        "id": "note",
        "type": "note",
        "text": "Operator-only context",
    }
    data["edges"].append(
        {"id": "e_note_final", "from": "note", "to": "final", "map": {"missing": "response"}}
    )

    report = validate_manifest(parse_manifest(data))

    issue = _issue(report, "map_bad_source_port")
    assert issue.path == "edges.e_note_final.map"
    assert "node 'note'" in issue.message


def test_string_assignable_to_messages() -> None:
    data = make_manifest()
    data["nodes"]["agent"]["inputs"] = {"input": {"type": "messages"}}
    report = validate_manifest(parse_manifest(data))
    assert report.valid


def test_file_input_pipeline_validates() -> None:
    data = make_manifest()
    data["nodes"]["file_input"] = {
        "id": "file_input",
        "type": "file_input",
        "inputs": {"path": {"type": "string"}},
        "outputs": {"text": {"type": "string"}, "metadata": {"type": "structured"}},
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

    report = validate_manifest(parse_manifest(data))

    assert report.valid


def test_managed_file_ref_satisfies_file_source_validation() -> None:
    data = make_manifest()
    data["nodes"]["file_input"] = {
        "id": "file_input",
        "type": "file_input",
        "file_ref": {
            "file_id": "FILE-1",
            "file_ref": "caliber://projects/PRJ-1/input/source.md",
            "sha256": "d" * 64,
            "name": "source.md",
            "size_bytes": 12,
        },
    }
    report = validate_manifest(parse_manifest(data))
    assert "missing_file_path" not in _codes(report)


def test_mcp_resource_pipeline_validates() -> None:
    data = make_manifest()
    data["nodes"]["mcp_lookup"] = {
        "id": "mcp_lookup",
        "type": "mcp_resource",
        "server_id": "MCP-DOCS",
        "tool_name": "search_docs",
        "inputs": {"input": {"type": "string"}},
        "outputs": {
            "text": {"type": "string"},
            "result": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    data["edges"] = [
        {"id": "e_start_mcp", "from": "start", "to": "mcp_lookup", "map": {"msg": "input"}},
        {"id": "e_mcp_agent", "from": "mcp_lookup", "to": "agent", "map": {"text": "input"}},
        {
            "id": "e_agent_final",
            "from": "agent",
            "to": "final",
            "map": {"final_output": "response"},
        },
    ]

    report = validate_manifest(parse_manifest(data))

    assert report.valid


def test_tool_node_pipeline_validates() -> None:
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
        },
    }
    data["edges"] = [
        {"id": "e_start_tool", "from": "start", "to": "tool_lookup", "map": {"msg": "input"}},
        {"id": "e_tool_agent", "from": "tool_lookup", "to": "agent", "map": {"text": "input"}},
        {
            "id": "e_agent_final",
            "from": "agent",
            "to": "final",
            "map": {"final_output": "response"},
        },
    ]

    report = validate_manifest(parse_manifest(data), resolver=registry_resolver())

    assert report.valid


def test_orchestration_nodes_validate_and_reference_targets() -> None:
    data = make_manifest()
    data["nodes"]["for_each"] = {
        "id": "for_each",
        "type": "for_each",
        "target_node_id": "agent",
    }
    data["nodes"]["loop"] = {
        "id": "loop",
        "type": "loop",
        "target_node_id": "agent",
        "max_iterations": 4,
        "stop_condition": "iteration >= 2",
    }
    data["nodes"]["boundary"] = {
        "id": "boundary",
        "type": "error_boundary",
        "target_node_id": "agent",
    }
    data["nodes"]["subflow"] = {
        "id": "subflow",
        "type": "subworkflow",
        "workflow_id": "wf_other",
    }
    report = validate_manifest(parse_manifest(data))
    assert report.valid


def test_loop_node_requires_target() -> None:
    data = make_manifest()
    data["nodes"]["loop"] = {
        "id": "loop",
        "type": "loop",
        "max_iterations": 3,
    }

    report = validate_manifest(parse_manifest(data))

    assert "loop_missing_target" in _codes(report)


def test_orchestration_nodes_can_omit_targets_for_local_iteration_or_fallback() -> None:
    data = make_manifest()
    data["nodes"]["for_each"] = {
        "id": "for_each",
        "type": "for_each",
    }
    data["nodes"]["boundary"] = {
        "id": "boundary",
        "type": "error_boundary",
    }

    report = validate_manifest(parse_manifest(data))

    assert report.valid
    assert "foreach_bad_target" not in _codes(report)
    assert "error_boundary_bad_target" not in _codes(report)


def test_subworkflow_cannot_target_current_workflow() -> None:
    data = make_manifest()
    data["nodes"]["subflow"] = {
        "id": "subflow",
        "type": "subworkflow",
        "workflow_id": data["workflow_id"],
    }

    report = validate_manifest(parse_manifest(data))

    assert "subworkflow_self_reference" in _codes(report)


def test_orchestration_nodes_accept_executable_non_agent_targets() -> None:
    data = make_manifest()
    data["nodes"]["python"] = {
        "id": "python",
        "type": "python_code",
        "code": 'return {"text": str(input or run_input), "result": {"ok": True}}',
        "inputs": {"input": {"type": "string"}},
        "outputs": {"text": {"type": "string"}, "result": {"type": "structured"}},
    }
    data["nodes"]["knowledge"] = {
        "id": "knowledge",
        "type": "knowledge_query",
        "knowledge_base_id": "KB-1",
        "retrieval_modes": ["dense"],
        "top_k": 3,
        "inputs": {"question": {"type": "string"}},
        "outputs": {"text": {"type": "string"}, "result": {"type": "structured"}},
    }
    data["nodes"]["template"] = {
        "id": "template",
        "type": "template",
        "template": '{"question":"{{input}}"}',
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
    data["nodes"]["mcp"] = {
        "id": "mcp",
        "type": "mcp_resource",
        "server_id": "MCP-1",
        "tool_name": "search_docs",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"text": {"type": "string"}, "result": {"type": "structured"}},
    }
    data["nodes"]["tool"] = {
        "id": "tool",
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
        },
    }
    data["nodes"]["for_each"] = {
        "id": "for_each",
        "type": "for_each",
        "target_node_id": "tool",
    }
    data["nodes"]["loop"] = {
        "id": "loop",
        "type": "loop",
        "target_node_id": "template",
        "max_iterations": 3,
        "stop_condition": "iteration >= 2",
    }
    data["nodes"]["boundary"] = {
        "id": "boundary",
        "type": "error_boundary",
        "target_node_id": "tool",
        "compensate_with": "knowledge",
    }

    report = validate_manifest(parse_manifest(data), resolver=registry_resolver())

    assert report.valid


def test_parallel_and_join_warn_when_branch_structure_is_insufficient() -> None:
    data = make_manifest()
    data["nodes"]["parallel"] = {
        "id": "parallel",
        "type": "parallel",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"output": {"type": "string"}},
    }
    data["nodes"]["join"] = {
        "id": "join",
        "type": "join",
        "mode": "all",
        "inputs": {"branch": {"type": "string"}},
        "outputs": {"output": {"type": "string"}},
    }
    data["edges"] = [
        {"id": "e_start_parallel", "from": "start", "to": "parallel", "map": {"msg": "input"}},
        {"id": "e_parallel_join", "from": "parallel", "to": "join", "map": {"output": "branch"}},
        {"id": "e_join_final", "from": "join", "to": "final", "map": {"output": "response"}},
    ]

    report = validate_manifest(parse_manifest(data))
    warning_codes = _warning_codes(report)

    assert report.valid
    assert "parallel_insufficient_fanout" in warning_codes
    assert "join_insufficient_fanin" in warning_codes


def test_join_warns_when_multiple_branches_collapse_into_one_input_port() -> None:
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
        "name": "agent-two",
        "model": "inherit",
        "instructions": {"type": "inline", "text": "second branch"},
        "inputs": {"input": {"type": "string"}},
        "outputs": {"final_output": {"type": "string"}},
    }
    data["nodes"]["join"] = {
        "id": "join",
        "type": "join",
        "mode": "all",
        "inputs": {"branch": {"type": "string"}},
        "outputs": {"output": {"type": "string"}},
    }
    data["edges"] = [
        {"id": "e_start_parallel", "from": "start", "to": "parallel", "map": {"msg": "input"}},
        {
            "id": "e_parallel_agent_one",
            "from": "parallel",
            "to": "agent",
            "map": {"output": "input"},
        },
        {
            "id": "e_parallel_agent_two",
            "from": "parallel",
            "to": "agent_two",
            "map": {"output": "input"},
        },
        {
            "id": "e_agent_one_join",
            "from": "agent",
            "to": "join",
            "map": {"final_output": "branch"},
        },
        {
            "id": "e_agent_two_join",
            "from": "agent_two",
            "to": "join",
            "map": {"final_output": "branch"},
        },
        {"id": "e_join_final", "from": "join", "to": "final", "map": {"output": "response"}},
    ]

    report = validate_manifest(parse_manifest(data))

    assert report.valid


def test_knowledge_query_requires_kb_or_pinned_version() -> None:
    data = make_manifest()
    data["nodes"]["knowledge"] = {
        "id": "knowledge",
        "type": "knowledge_query",
        "knowledge_base_id": "",
        "version_ids": [],
        "retrieval_modes": [],
        "top_k": 3,
        "inputs": {"question": {"type": "string"}},
        "outputs": {"text": {"type": "string"}, "result": {"type": "structured"}},
    }

    report = validate_manifest(parse_manifest(data))

    issue = _issue(report, "missing_knowledge_target")
    assert issue.path == "nodes.knowledge.knowledge_base_id"
    assert issue.message == "Select a knowledge base or pinned version."


def test_knowledge_query_accepts_version_ids_mapped_from_upstream_node() -> None:
    data = make_manifest()
    del data["nodes"]["agent"]
    data["nodes"]["versions"] = {
        "id": "versions",
        "type": "python_code",
        "code": 'return {"result": ["KBV-1"]}',
        "outputs": {"result": {"type": "structured"}},
    }
    data["nodes"]["knowledge"] = {
        "id": "knowledge",
        "type": "knowledge_query",
        "knowledge_base_id": "",
        "version_ids": [],
        "retrieval_modes": [],
        "top_k": 3,
        "inputs": {
            "question": {"type": "string"},
            "version_ids": {"type": "structured"},
        },
        "outputs": {"text": {"type": "string"}, "result": {"type": "structured"}},
    }
    data["edges"] = [
        {"id": "e_start_knowledge", "from": "start", "to": "knowledge", "map": {"msg": "question"}},
        {
            "id": "e_versions_knowledge",
            "from": "versions",
            "to": "knowledge",
            "map": {"result": "version_ids"},
        },
        {
            "id": "e_knowledge_final",
            "from": "knowledge",
            "to": "final",
            "map": {"text": "response"},
        },
    ]

    report = validate_manifest(parse_manifest(data))

    assert "missing_knowledge_target" not in _codes(report)


def test_knowledge_build_requires_target_chunker_and_embedder() -> None:
    data = make_manifest()
    data["nodes"]["knowledge_build"] = {
        "id": "knowledge_build",
        "type": "knowledge_build",
        "knowledge_base_id": "",
        "chunking_strategy": "",
        "embedding_model": "",
        "inputs": {
            "sources": {"type": "structured"},
            "chunking_strategy": {"type": "string"},
            "embedding_model": {"type": "string"},
        },
        "outputs": {"text": {"type": "string"}, "result": {"type": "structured"}},
    }

    report = validate_manifest(parse_manifest(data))

    assert _issue(report, "missing_knowledge_build_target").path == (
        "nodes.knowledge_build.knowledge_base_id"
    )
    assert _issue(report, "missing_knowledge_build_chunking_strategy").path == (
        "nodes.knowledge_build.chunking_strategy"
    )
    assert _issue(report, "missing_knowledge_build_embedding_model").path == (
        "nodes.knowledge_build.embedding_model"
    )


def test_knowledge_build_accepts_runtime_chunker_and_embedder_inputs() -> None:
    data = make_manifest()
    del data["nodes"]["agent"]
    data["nodes"]["selector"] = {
        "id": "selector",
        "type": "python_code",
        "code": 'return {"chunking": "recursive", "embedding": "BAAI/bge-m3"}',
        "outputs": {
            "chunking": {"type": "string"},
            "embedding": {"type": "string"},
        },
    }
    data["nodes"]["knowledge_build"] = {
        "id": "knowledge_build",
        "type": "knowledge_build",
        "knowledge_base_id": "KB-1",
        "chunking_strategy": "",
        "embedding_model": "",
        "inputs": {
            "chunking_strategy": {"type": "string"},
            "embedding_model": {"type": "string"},
        },
        "outputs": {"text": {"type": "string"}, "result": {"type": "structured"}},
    }
    data["edges"] = [
        {"id": "e_start_selector", "from": "start", "to": "selector", "map": {"msg": "input"}},
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

    report = validate_manifest(parse_manifest(data))

    assert "missing_knowledge_build_chunking_strategy" not in _codes(report)
    assert "missing_knowledge_build_embedding_model" not in _codes(report)


def test_node_setup_requires_storage_locations_and_guardrails() -> None:
    data = make_manifest()
    data["nodes"]["file_input"] = {
        "id": "file_input",
        "type": "file_input",
        "path": "",
    }
    data["nodes"]["folder_input"] = {
        "id": "folder_input",
        "type": "folder_input",
        "path": "",
    }
    data["nodes"]["bucket_in"] = {
        "id": "bucket_in",
        "type": "input_bucket",
        "bucket": "",
    }
    data["nodes"]["bucket_out"] = {
        "id": "bucket_out",
        "type": "output_bucket",
        "bucket": "",
    }
    data["nodes"]["folder_out"] = {
        "id": "folder_out",
        "type": "output_folder",
        "path": "",
    }
    data["nodes"]["guard"] = {
        "id": "guard",
        "type": "guardrail",
        "mode": "post_agent",
        "inputs": {"response": {"type": "string"}},
        "outputs": {"clean": {"type": "string"}},
        "checks": [],
    }

    report = validate_manifest(parse_manifest(data))

    assert _issue(report, "missing_file_path").path == "nodes.file_input.path"
    assert _issue(report, "missing_folder_path").path == "nodes.folder_input.path"
    assert _issue(report, "missing_input_bucket").path == "nodes.bucket_in.bucket"
    assert _issue(report, "missing_output_bucket").path == "nodes.bucket_out.bucket"
    assert _issue(report, "missing_output_folder_path").path == "nodes.folder_out.path"
    assert _issue(report, "missing_guardrail_checks").path == "nodes.guard.checks"


def test_router_requires_branches_and_matching_edges() -> None:
    data = make_manifest()
    data["nodes"]["router_empty"] = {
        "id": "router_empty",
        "type": "router",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"route": {"type": "string"}},
        "branches": [],
    }
    data["nodes"]["router_bad"] = {
        "id": "router_bad",
        "type": "router",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"route": {"type": "string"}},
        "branches": [
            {"condition": {"op": "contains", "value": "refund"}, "to": "ghost"},
            {"to": "agent"},
        ],
    }

    report = validate_manifest(parse_manifest(data))

    assert _issue(report, "missing_router_branches").path == "nodes.router_empty.branches"
    assert _issue(report, "router_bad_target").path == "nodes.router_bad.branches[0].to"
    assert _issue(report, "router_missing_edge").path == "nodes.router_bad.branches[1].to"


def test_orchestration_node_bad_target_fails_validation() -> None:
    data = make_manifest()
    data["nodes"]["for_each"] = {
        "id": "for_each",
        "type": "for_each",
        "target_node_id": "ghost",
    }
    report = validate_manifest(parse_manifest(data))
    assert "foreach_bad_target" in _codes(report)


def test_orchestration_nodes_reject_blocking_or_terminal_targets() -> None:
    data = make_manifest()
    data["nodes"]["wait_event"] = {
        "id": "wait_event",
        "type": "wait_for_event",
        "event_name": "resume.ticket",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"output": {"type": "string"}},
    }
    data["nodes"]["for_each"] = {
        "id": "for_each",
        "type": "for_each",
        "target_node_id": "wait_event",
    }
    data["nodes"]["boundary"] = {
        "id": "boundary",
        "type": "error_boundary",
        "target_node_id": "wait_event",
        "compensate_with": "final",
    }

    report = validate_manifest(parse_manifest(data))
    codes = _codes(report)

    assert "foreach_unsupported_target_type" in codes
    assert "error_boundary_unsupported_target_type" in codes
    assert "error_boundary_unsupported_compensation_type" in codes


def test_map_references_nonexistent_output() -> None:
    data = make_manifest()
    data["edges"][0]["map"] = {"nonexistent": "input"}
    report = validate_manifest(parse_manifest(data))
    assert "map_bad_source_port" in _codes(report)


def test_map_references_nonexistent_input() -> None:
    data = make_manifest()
    data["edges"][0]["map"] = {"msg": "nonexistent"}
    report = validate_manifest(parse_manifest(data))
    assert "map_bad_target_port" in _codes(report)


def test_unbound_tool() -> None:
    data = make_manifest()
    data["nodes"]["agent"]["tools"] = ["lookup_policy"]
    report = validate_manifest(parse_manifest(data))
    assert "unbound_tool" in _codes(report)


def test_tool_not_registered() -> None:
    data = make_support_manifest()
    # resolver only knows lookup_policy/get_order/escalate; point one at a missing family
    data["tools"]["lookup_policy"]["registry_ref"] = "tool.missing.v1"
    report = validate_manifest(parse_manifest(data), resolver=registry_resolver())
    assert "missing_tool" in _codes(report)


def test_version_constraint_unsatisfied() -> None:
    data = make_support_manifest()
    data["tools"]["lookup_policy"]["version_constraint"] = ">=2.0"
    report = validate_manifest(parse_manifest(data), resolver=registry_resolver())
    assert "missing_tool" in _codes(report)


def test_write_tool_without_approval_warns() -> None:
    data = make_support_manifest()
    report = validate_manifest(parse_manifest(data), resolver=registry_resolver())
    # escalate is external_action and there's no human approval node
    assert any(w.code == "write_tool_without_approval" for w in report.warnings)


def test_direct_tool_without_approval_warns() -> None:
    data = make_manifest()
    data["nodes"]["tool"] = {
        "id": "tool",
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
    }
    data["tools"] = {
        "escalate": {
            "registry_ref": "tool.escalate.v1",
            "version_constraint": ">=1.0",
        }
    }

    report = validate_manifest(parse_manifest(data), resolver=registry_resolver())

    assert any(w.code == "write_tool_without_approval" for w in report.warnings)


def test_mcp_write_tool_without_approval_warns_without_registry_resolver() -> None:
    data = make_manifest()
    data["nodes"]["agent"]["tools"] = ["write_docs"]
    data["tools"] = {
        "write_docs": {
            "type": "mcp_tool",
            "server_id": "MCP-DOCS",
            "tool_name": "write_file",
            "side_effect_level": "write",
        }
    }
    report = validate_manifest(parse_manifest(data))
    assert any(w.code == "write_tool_without_approval" for w in report.warnings)


def test_missing_prompt_ref() -> None:
    data = make_manifest()
    data["nodes"]["agent"]["instructions"] = {"type": "mlflow_prompt", "ref": "nope"}
    report = validate_manifest(parse_manifest(data))
    assert "missing_prompt_ref" in _codes(report)


def test_deploy_gate_missing_dataset() -> None:
    data = make_manifest(
        deploy_gates={
            "g": {"type": "deploy_gate", "dataset_ref": "ghost", "required_for_aliases": ["prod"]}
        }
    )
    report = validate_manifest(parse_manifest(data))
    assert "missing_eval_dataset" in _codes(report)


def test_agent_eval_dataset_missing_artifact() -> None:
    data = make_manifest(artifacts={"eval_datasets": {}})
    data["nodes"]["agent"]["eval_dataset"] = "ED-support"
    report = validate_manifest(parse_manifest(data))
    assert "missing_eval_dataset" in _codes(report)


def _handoff_manifest(target: str, back: str) -> dict:
    nodes = {
        "start": {"id": "start", "type": "start", "outputs": {"msg": {"type": "string"}}},
        "a": {
            "id": "a",
            "type": "agent",
            "name": "a",
            "model": "inherit",
            "instructions": {"type": "inline", "text": "x"},
            "inputs": {"input": {"type": "string"}},
            "outputs": {"final_output": {"type": "string"}},
            "handoffs": [{"target": target}],
        },
        "b": {
            "id": "b",
            "type": "agent",
            "name": "b",
            "model": "inherit",
            "instructions": {"type": "inline", "text": "y"},
            "inputs": {"input": {"type": "string"}},
            "outputs": {"final_output": {"type": "string"}},
            "handoffs": ([{"target": back}] if back else []),
        },
        "final": {"id": "final", "type": "output", "inputs": {"response": {"type": "string"}}},
    }
    return make_manifest(
        nodes=nodes,
        edges=[
            {"id": "e1", "from": "start", "to": "a", "map": {"msg": "input"}},
            {"id": "e2", "from": "a", "to": "final", "map": {"final_output": "response"}},
        ],
    )
