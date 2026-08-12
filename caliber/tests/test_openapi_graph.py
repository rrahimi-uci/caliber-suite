"""Unit tests for the derived API dependency graph projection."""

from __future__ import annotations

from caliber.integrations.openapi.graph import build_graph_snapshot


def _integration() -> dict:
    return {"integration_id": "OAI-1", "name": "Ticketing"}


def _version() -> dict:
    return {
        "version_id": "OAIV-1",
        "server_urls": ["https://tickets.example.com"],
        "auth_schemes": ["bearerAuth"],
    }


def _operation(op_id: str, key: str, **overrides) -> dict:
    base = {
        "operation_id": op_id,
        "operation_key": key,
        "method": key.split(" ")[0],
        "path": key.split(" ")[1],
        "side_effect_level": "read",
        "auth_schemes": ["bearerAuth"],
        "tags": ["tickets"],
        "deprecated": False,
        "normalized_operation": {"classification": {"resource_type": "ticket", "operation_kind": "get"}},
    }
    base.update(overrides)
    return base


def test_graph_contains_one_node_per_integration_server_and_operation() -> None:
    operations = [_operation("OP-1", "GET /tickets/{id}")]
    snapshot = build_graph_snapshot(
        integration=_integration(), version=_version(), operations=operations, dependencies=[]
    )
    node_types = {node["type"] for node in snapshot["nodes"]}
    assert "integration" in node_types
    assert "server" in node_types
    assert "auth_scheme" in node_types
    assert "operation" in node_types
    assert "resource_type" in node_types
    assert "tag" in node_types


def test_every_operation_requires_auth_edge_to_its_scheme() -> None:
    operations = [_operation("OP-1", "GET /tickets/{id}")]
    snapshot = build_graph_snapshot(
        integration=_integration(), version=_version(), operations=operations, dependencies=[]
    )
    requires_auth_edges = [edge for edge in snapshot["edges"] if edge["type"] == "requires_auth"]
    assert len(requires_auth_edges) == 1
    assert requires_auth_edges[0]["from"] == "operation:OP-1"
    assert requires_auth_edges[0]["to"] == "auth_scheme:bearerAuth"


def test_dependency_rows_become_dependency_nodes_with_two_edges() -> None:
    operations = [
        _operation("OP-1", "POST /tickets", auth_schemes=[]),
        _operation("OP-2", "GET /tickets/{id}", auth_schemes=[]),
    ]
    dependency = {
        "dependency_id": "OADEP-1",
        "from_operation_id": "OP-1",
        "to_operation_id": "OP-2",
        "dependency_type": "consumes_identifier_from",
        "confidence": "medium",
        "source": "schema_match",
        "status": "suggested",
        "required": True,
        "binding_field_map": {"ticket_id": "response.id"},
    }
    snapshot = build_graph_snapshot(
        integration=_integration(),
        version=_version(),
        operations=operations,
        dependencies=[dependency],
    )
    dependency_nodes = [node for node in snapshot["nodes"] if node["type"] == "dependency"]
    assert len(dependency_nodes) == 1
    assert dependency_nodes[0]["data"]["confidence"] == "medium"
    depends_on_edges = [edge for edge in snapshot["edges"] if edge["type"] == "depends_on"]
    assert {(edge["from"], edge["to"]) for edge in depends_on_edges} == {
        ("operation:OP-1", "dependency:OADEP-1"),
        ("dependency:OADEP-1", "operation:OP-2"),
    }


def test_dependency_type_maps_to_the_documented_edge_vocabulary() -> None:
    operations = [
        _operation("OP-1", "POST /exports", auth_schemes=[]),
        _operation("OP-2", "GET /exports/{id}", auth_schemes=[]),
    ]
    dependency = {
        "dependency_id": "OADEP-1",
        "from_operation_id": "OP-1",
        "to_operation_id": "OP-2",
        "dependency_type": "polls",
        "confidence": "medium",
        "source": "rule_inference",
        "status": "suggested",
        "required": False,
        "binding_field_map": {},
    }
    snapshot = build_graph_snapshot(
        integration=_integration(),
        version=_version(),
        operations=operations,
        dependencies=[dependency],
    )
    polls_edges = [edge for edge in snapshot["edges"] if edge["type"] == "polls"]
    assert len(polls_edges) == 2


def test_a_dependency_referencing_an_unknown_operation_is_skipped_not_crashed() -> None:
    operations = [_operation("OP-1", "GET /tickets/{id}", auth_schemes=[])]
    dependency = {
        "dependency_id": "OADEP-1",
        "from_operation_id": "OP-1",
        "to_operation_id": "OP-MISSING",
        "dependency_type": "depends_on",
        "confidence": "low",
        "source": "rule_inference",
        "status": "advisory",
        "required": False,
        "binding_field_map": {},
    }
    snapshot = build_graph_snapshot(
        integration=_integration(),
        version=_version(),
        operations=operations,
        dependencies=[dependency],
    )
    assert not any(node["type"] == "dependency" for node in snapshot["nodes"])


def test_published_tool_draft_chains_operation_to_draft_to_tool() -> None:
    operations = [_operation("OP-1", "GET /tickets/{id}", auth_schemes=[])]
    draft = {
        "draft_id": "OATD-1",
        "operation_id": "OP-1",
        "additional_operation_ids": [],
        "name": "get_ticket",
        "status": "published",
        "published_tool_id": "TL-1",
    }
    snapshot = build_graph_snapshot(
        integration=_integration(),
        version=_version(),
        operations=operations,
        dependencies=[],
        tool_drafts=[draft],
    )
    node_ids = {node["id"] for node in snapshot["nodes"]}
    assert "tool_draft:OATD-1" in node_ids
    assert "published_tool:TL-1" in node_ids
    publishes_edges = {
        (edge["from"], edge["to"]) for edge in snapshot["edges"] if edge["type"] == "publishes_as_tool"
    }
    assert ("operation:OP-1", "tool_draft:OATD-1") in publishes_edges
    assert ("tool_draft:OATD-1", "published_tool:TL-1") in publishes_edges


def test_a_tool_pack_draft_attaches_to_every_bound_operation() -> None:
    operations = [
        _operation("OP-1", "GET /tickets", auth_schemes=[]),
        _operation("OP-2", "GET /tickets/{id}", auth_schemes=[]),
    ]
    draft = {
        "draft_id": "OATD-1",
        "operation_id": "OP-1",
        "additional_operation_ids": ["OP-2"],
        "name": "ticket_pack",
        "status": "draft",
        "published_tool_id": None,
    }
    snapshot = build_graph_snapshot(
        integration=_integration(),
        version=_version(),
        operations=operations,
        dependencies=[],
        tool_drafts=[draft],
    )
    publishes_edges = {edge["from"] for edge in snapshot["edges"] if edge["type"] == "publishes_as_tool"}
    assert "operation:OP-1" in publishes_edges
    assert "operation:OP-2" in publishes_edges


def test_snapshot_is_deterministic_across_repeated_calls() -> None:
    operations = [_operation("OP-1", "GET /tickets/{id}")]
    first = build_graph_snapshot(
        integration=_integration(), version=_version(), operations=operations, dependencies=[]
    )
    second = build_graph_snapshot(
        integration=_integration(), version=_version(), operations=list(operations), dependencies=[]
    )
    assert first == second


def test_summary_counts_match_the_actual_node_and_edge_lists() -> None:
    operations = [_operation("OP-1", "GET /tickets/{id}")]
    snapshot = build_graph_snapshot(
        integration=_integration(), version=_version(), operations=operations, dependencies=[]
    )
    assert snapshot["summary"]["node_count"] == len(snapshot["nodes"])
    assert snapshot["summary"]["edge_count"] == len(snapshot["edges"])
    assert snapshot["summary"]["operation_count"] == 1


def test_graph_projects_request_and_response_schema_nodes_by_fingerprint() -> None:
    operation = _operation(
        "OP-1",
        "POST /tickets",
        method="POST",
        path="/tickets",
        side_effect_level="write",
        normalized_operation={
            "classification": {"resource_type": "ticket", "operation_kind": "create"},
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {"title": {"type": "string"}},
                            "required": ["title"],
                        }
                    }
                },
            },
            "responses": {
                "201": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {"ticket_id": {"type": "string"}},
                                "required": ["ticket_id"],
                            }
                        }
                    }
                }
            },
        },
    )
    snapshot = build_graph_snapshot(
        integration=_integration(),
        version=_version(),
        operations=[operation],
        dependencies=[],
    )
    node_types = {node["type"] for node in snapshot["nodes"]}
    assert "request_schema" in node_types
    assert "response_schema" in node_types
    edge_types = {edge["type"] for edge in snapshot["edges"]}
    assert "consumes_schema" in edge_types
    assert "produces_schema" in edge_types
