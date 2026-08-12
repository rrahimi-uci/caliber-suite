"""Derive the API dependency graph from canonical rows.

Per §5 of the proposal, the graph is a *secondary planning and retrieval
surface*, never the source of dependency truth — that role belongs to
``CaliberOpenApiOperationDependency`` rows. Everything in this module is a pure
projection: given the same operations, dependency rows, and tool drafts, it
always produces the same node/edge set, so the snapshot cached on
``CaliberOpenApiIntegrationVersion.graph_snapshot`` is reproducible and diffable
rather than an opaque cache.

Node/edge vocabulary matches §5.2 exactly:

* nodes: integration, server, auth_scheme, tag, operation, resource_type,
  request_schema, response_schema, tool_draft, published_tool, dependency.
* edges: requires_auth, belongs_to_tag, consumes_schema, produces_schema,
  depends_on, returns_identifier_for, polls, paginates_to, publishes_as_tool.
"""

from __future__ import annotations

# The graph projection enumerates the published node/edge vocabulary directly.
# ruff: noqa: PLR0912, PLR0915
import hashlib
import json
from collections.abc import Sequence
from typing import Any

_DEPENDENCY_EDGE_TYPES = {
    "produces_identifier_for": "returns_identifier_for",
    "consumes_identifier_from": "depends_on",
    "requires_auth": "requires_auth",
    "polls": "polls",
    "paginates_to": "paginates_to",
    "compensates": "depends_on",
    "precondition_for": "depends_on",
    "grouped_with": "depends_on",
}


def build_graph_snapshot(
    *,
    integration: Any,
    version: Any,
    operations: Sequence[Any],
    dependencies: Sequence[Any],
    tool_drafts: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """Build the node/edge projection for one pinned integration version.

    Accepts either ORM rows or plain dicts for every argument (attribute access
    falls back to dict-style lookup via :func:`_get`), so the same function
    serves both the live route (ORM rows) and a unit test (plain dicts).
    """

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    seen_node_ids: set[str] = set()

    def add_node(
        node_id: str, node_type: str, label: str, data: dict[str, Any] | None = None
    ) -> None:
        if node_id in seen_node_ids:
            return
        seen_node_ids.add(node_id)
        nodes.append({"id": node_id, "type": node_type, "label": label, "data": data or {}})

    def add_edge(
        edge_id: str, edge_type: str, from_id: str, to_id: str, data: dict[str, Any] | None = None
    ) -> None:
        edges.append(
            {"id": edge_id, "type": edge_type, "from": from_id, "to": to_id, "data": data or {}}
        )

    integration_id = _get(integration, "integration_id")
    integration_node = f"integration:{integration_id}"
    add_node(integration_node, "integration", _get(integration, "name") or integration_id)

    for server_url in _get(version, "server_urls") or []:
        server_node = f"server:{server_url}"
        add_node(server_node, "server", server_url, {"url": server_url})
        add_edge(
            f"{integration_node}->{server_node}", "belongs_to_tag", integration_node, server_node
        )

    for scheme in _get(version, "auth_schemes") or []:
        scheme_node = f"auth_scheme:{scheme}"
        add_node(scheme_node, "auth_scheme", scheme)

    tool_drafts_by_operation: dict[str, list[Any]] = {}
    for draft in tool_drafts or []:
        for op_id in _draft_operation_ids(draft):
            tool_drafts_by_operation.setdefault(op_id, []).append(draft)

    operation_nodes: dict[str, str] = {}
    for operation in operations:
        operation_id = _get(operation, "operation_id")
        operation_node = f"operation:{operation_id}"
        operation_nodes[operation_id] = operation_node
        classification = _classification(operation)
        add_node(
            operation_node,
            "operation",
            _get(operation, "operation_key") or operation_id,
            {
                "method": _get(operation, "method"),
                "path": _get(operation, "path"),
                "side_effect_level": _get(operation, "side_effect_level"),
                "resource_type": classification.get("resource_type"),
                "operation_kind": classification.get("operation_kind"),
                "is_paginated": classification.get("is_paginated", False),
                "is_async": classification.get("is_async", False),
                "deprecated": bool(_get(operation, "deprecated")),
            },
        )
        add_edge(
            f"{integration_node}->{operation_node}",
            "belongs_to_tag",
            integration_node,
            operation_node,
        )

        resource_type = classification.get("resource_type")
        if resource_type:
            resource_node = f"resource_type:{resource_type}"
            add_node(resource_node, "resource_type", resource_type)
            add_edge(
                f"{operation_node}->{resource_node}",
                "belongs_to_tag",
                operation_node,
                resource_node,
            )

        for tag in _get(operation, "tags") or []:
            tag_node = f"tag:{tag}"
            add_node(tag_node, "tag", tag)
            add_edge(f"{operation_node}->{tag_node}", "belongs_to_tag", operation_node, tag_node)

        for scheme in _get(operation, "auth_schemes") or []:
            scheme_node = f"auth_scheme:{scheme}"
            add_node(scheme_node, "auth_scheme", scheme)
            add_edge(
                f"{operation_node}->{scheme_node}",
                "requires_auth",
                operation_node,
                scheme_node,
            )

        for schema_node in _schema_nodes(operation):
            add_node(
                schema_node["node_id"],
                schema_node["node_type"],
                schema_node["label"],
                schema_node["data"],
            )
            add_edge(
                f"{operation_node}->{schema_node['node_id']}:{schema_node['edge_type']}",
                schema_node["edge_type"],
                operation_node,
                schema_node["node_id"],
            )

        for draft in tool_drafts_by_operation.get(operation_id, []):
            draft_id = _get(draft, "draft_id")
            draft_node = f"tool_draft:{draft_id}"
            add_node(
                draft_node,
                "tool_draft",
                _get(draft, "name") or draft_id,
                {"status": _get(draft, "status")},
            )
            add_edge(
                f"{operation_node}->{draft_node}",
                "publishes_as_tool",
                operation_node,
                draft_node,
            )
            published_tool_id = _get(draft, "published_tool_id")
            if published_tool_id:
                tool_node = f"published_tool:{published_tool_id}"
                add_node(tool_node, "published_tool", published_tool_id)
                add_edge(
                    f"{draft_node}->{tool_node}",
                    "publishes_as_tool",
                    draft_node,
                    tool_node,
                )

    for dependency in dependencies:
        from_op = _get(dependency, "from_operation_id")
        to_op = _get(dependency, "to_operation_id")
        from_node = operation_nodes.get(from_op)
        to_node = operation_nodes.get(to_op)
        if from_node is None or to_node is None:
            continue
        dependency_type = _get(dependency, "dependency_type")
        edge_type = _DEPENDENCY_EDGE_TYPES.get(dependency_type, "depends_on")
        dependency_id = _get(dependency, "dependency_id") or f"{from_op}:{to_op}:{dependency_type}"
        dependency_node = f"dependency:{dependency_id}"
        add_node(
            dependency_node,
            "dependency",
            dependency_type,
            {
                "dependency_type": dependency_type,
                "confidence": _get(dependency, "confidence"),
                "source": _get(dependency, "source"),
                "status": _get(dependency, "status"),
                "required": bool(_get(dependency, "required")),
                "binding_field_map": _get(dependency, "binding_field_map") or {},
            },
        )
        add_edge(f"{from_node}->{dependency_node}", edge_type, from_node, dependency_node)
        add_edge(f"{dependency_node}->{to_node}", edge_type, dependency_node, to_node)

    return {
        "integration_id": integration_id,
        "integration_version_id": _get(version, "version_id"),
        "nodes": nodes,
        "edges": edges,
        "summary": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "operation_count": len(operation_nodes),
            "dependency_count": len(dependencies),
        },
    }


def _draft_operation_ids(draft: Any) -> list[str]:
    primary = _get(draft, "operation_id")
    additional = _get(draft, "additional_operation_ids") or []
    ids = [primary] if primary else []
    ids.extend(op_id for op_id in additional if op_id)
    return ids


def _classification(operation: Any) -> dict[str, Any]:
    normalized = _get(operation, "normalized_operation")
    if isinstance(normalized, dict):
        classification = normalized.get("classification")
        if isinstance(classification, dict):
            return classification
    return {}


def _schema_nodes(operation: Any) -> list[dict[str, Any]]:
    normalized = _get(operation, "normalized_operation")
    if not isinstance(normalized, dict):
        return []

    nodes: dict[tuple[str, str], dict[str, Any]] = {}
    request_body = normalized.get("requestBody")
    if isinstance(request_body, dict):
        content = request_body.get("content")
        if isinstance(content, dict):
            for content_type, media in sorted(content.items()):
                schema = media.get("schema") if isinstance(media, dict) else None
                if isinstance(schema, dict):
                    _merge_schema_node(
                        nodes,
                        role="request",
                        schema=schema,
                        marker=str(content_type),
                    )

    responses = normalized.get("responses")
    if isinstance(responses, dict):
        for status, response in sorted(responses.items()):
            if not str(status).startswith("2") or not isinstance(response, dict):
                continue
            content = response.get("content")
            if not isinstance(content, dict):
                continue
            for content_type, media in sorted(content.items()):
                schema = media.get("schema") if isinstance(media, dict) else None
                if isinstance(schema, dict):
                    _merge_schema_node(
                        nodes,
                        role="response",
                        schema=schema,
                        marker=f"{status}:{content_type}",
                    )
    return list(nodes.values())


def _merge_schema_node(
    nodes: dict[tuple[str, str], dict[str, Any]],
    *,
    role: str,
    schema: dict[str, Any],
    marker: str,
) -> None:
    fingerprint = _schema_fingerprint(schema)
    key = (role, fingerprint)
    entry = nodes.get(key)
    if entry is None:
        node_type = f"{role}_schema"
        entry = {
            "node_id": f"{node_type}:{fingerprint}",
            "node_type": node_type,
            "label": str(schema.get("title") or f"{role} schema {fingerprint[:8]}"),
            "data": {
                "fingerprint": fingerprint,
                "schema": schema,
                "markers": [marker],
            },
            "edge_type": "consumes_schema" if role == "request" else "produces_schema",
        }
        nodes[key] = entry
        return
    markers = entry["data"].setdefault("markers", [])
    if marker not in markers:
        markers.append(marker)


def _schema_fingerprint(schema: dict[str, Any]) -> str:
    rendered = json.dumps(schema, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:16]


def _get(item: Any, name: str) -> Any:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)
