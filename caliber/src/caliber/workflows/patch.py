"""Semantic patch application for workflow manifests (plan §17.3, §19.6).

CALIBER proposes workflow changes as *semantic* operations keyed by stable ids,
never RFC-6902 positional JSON Patch (whose ``/nodes/2/tools/0`` paths corrupt
when the manifest is edited between diagnosis and application — plan §17.3).

Supported operations:

* ``add_node_after`` — insert a node downstream of a target, rewiring the
  target's outgoing edges through the new node;
* ``remove_node`` — remove a node and its incident edges;
* ``update_node_field`` — set a field (dotted path) on a node;
* ``add_edge`` / ``remove_edge`` — wire/unwire by id;
* ``update_tool_constraint`` — record a per-tool usage constraint on an agent.

Every op references stable ids. A missing target fails loudly
(:class:`PatchError`) rather than corrupting. The full candidate manifest is
always re-materialized and re-validated before return.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from caliber.workflows.manifest import (
    WorkflowManifest,
    compute_manifest_hash,
    parse_manifest,
)
from caliber.workflows.tools import ToolResolver
from caliber.workflows.validation import validate_manifest


class PatchError(Exception):
    """Raised when a semantic patch cannot be applied or yields an invalid manifest."""


def apply_patch(
    base_manifest: dict[str, Any],
    operations: list[dict[str, Any]],
    *,
    base_hash: str | None = None,
    resolver: ToolResolver | None = None,
) -> WorkflowManifest:
    """Apply semantic operations to a base manifest dict and return the result.

    Parameters
    ----------
    base_manifest:
        The base manifest as a dict (wire shape).
    operations:
        Ordered list of semantic op dicts (see module docstring).
    base_hash:
        When provided, the base manifest's canonical hash must match — guards
        against applying a patch to a base that changed since diagnosis.
    resolver:
        Optional tool resolver for the final type/tool validation pass.

    Raises
    ------
    PatchError
        On stale base, unknown op, missing target id, op conflicts, or an
        invalid resulting manifest.
    """
    if base_hash is not None:
        actual = compute_manifest_hash(base_manifest)
        if actual != base_hash:
            raise PatchError(
                f"stale base: expected manifest_hash {base_hash!r} but base hashes to {actual!r}"
            )

    work = deepcopy(base_manifest)
    work.setdefault("nodes", {})
    work.setdefault("edges", [])

    removed_nodes: set[str] = set()
    removed_edges: set[str] = set()

    for index, op in enumerate(operations):
        kind = op.get("op")
        if kind is None:
            raise PatchError(f"operation #{index} is missing 'op'")
        handler = _HANDLERS.get(kind)
        if handler is None:
            raise PatchError(f"unknown patch op {kind!r}")
        handler(work, op, removed_nodes, removed_edges)

    try:
        manifest = parse_manifest(work)
    except Exception as exc:
        raise PatchError(f"patch produced an invalid manifest: {exc}") from exc

    report = validate_manifest(manifest, resolver=resolver)
    if not report.valid:
        codes = [e.code for e in report.errors]
        raise PatchError(f"patch produced a manifest that fails validation: {codes}")
    return manifest


# ---------------------------------------------------------------------------
# Operation handlers
# ---------------------------------------------------------------------------


def _require_node(work: dict[str, Any], node_id: str, removed: set[str]) -> dict[str, Any]:
    if node_id in removed:
        raise PatchError(f"conflict: node {node_id!r} was removed earlier in this patch")
    node: dict[str, Any] | None = work["nodes"].get(node_id)
    if node is None:
        raise PatchError(f"target node {node_id!r} not found")
    return node


def _first_port(ports: dict[str, Any] | None) -> str | None:
    if not ports:
        return None
    return next(iter(ports))


def _op_add_node_after(
    work: dict[str, Any],
    op: dict[str, Any],
    removed_nodes: set[str],
    removed_edges: set[str],
) -> None:
    target_id = op.get("target_node_id")
    new_node = op.get("node")
    if not target_id or not isinstance(new_node, dict):
        raise PatchError("add_node_after requires 'target_node_id' and 'node'")
    _require_node(work, target_id, removed_nodes)
    new_id = new_node.get("id")
    if not new_id:
        raise PatchError("add_node_after node is missing 'id'")
    if new_id in work["nodes"]:
        raise PatchError(f"add_node_after: node {new_id!r} already exists")

    target = work["nodes"][target_id]
    new_in = _first_port(new_node.get("inputs")) or "response"
    new_out = _first_port(new_node.get("outputs")) or "passthrough"
    target_out = _first_port(target.get("outputs")) or "final_output"

    work["nodes"][new_id] = deepcopy(new_node)

    # Redirect the target's outgoing edges so they originate from the new node.
    for edge in work["edges"]:
        if edge.get("from") == target_id and edge.get("id") not in removed_edges:
            edge["from"] = new_id
            edge["map"] = {new_out: next(iter(edge.get("map", {}).values()), new_out)}

    # Connect target -> new node.
    work["edges"].append(
        {
            "id": op.get("edge_id") or f"e_{target_id}_{new_id}",
            "from": target_id,
            "to": new_id,
            "map": {target_out: new_in},
        }
    )


def _op_remove_node(
    work: dict[str, Any],
    op: dict[str, Any],
    removed_nodes: set[str],
    _removed_edges: set[str],
) -> None:
    target_id = op.get("target_node_id")
    if not target_id:
        raise PatchError("remove_node requires 'target_node_id'")
    _require_node(work, target_id, removed_nodes)
    del work["nodes"][target_id]
    work["edges"] = [
        e for e in work["edges"] if e.get("from") != target_id and e.get("to") != target_id
    ]
    removed_nodes.add(target_id)


def _op_update_node_field(
    work: dict[str, Any],
    op: dict[str, Any],
    removed_nodes: set[str],
    _removed_edges: set[str],
) -> None:
    target_id = op.get("target_node_id")
    field_path = op.get("field_path")
    if not target_id or not field_path:
        raise PatchError("update_node_field requires 'target_node_id' and 'field_path'")
    node = _require_node(work, target_id, removed_nodes)
    if "value" not in op:
        raise PatchError("update_node_field requires 'value'")
    parts = str(field_path).split(".")
    cursor: Any = node
    for part in parts[:-1]:
        cursor = cursor.setdefault(part, {})
        if not isinstance(cursor, dict):
            raise PatchError(f"field path {field_path!r} traverses a non-object at {part!r}")
    cursor[parts[-1]] = deepcopy(op["value"])


def _op_add_edge(
    work: dict[str, Any],
    op: dict[str, Any],
    removed_nodes: set[str],
    _removed_edges: set[str],
) -> None:
    edge = op.get("edge")
    if not isinstance(edge, dict):
        raise PatchError("add_edge requires an 'edge' object")
    edge = deepcopy(edge)
    if not edge.get("id"):
        raise PatchError("add_edge edge is missing 'id'")
    for endpoint in ("from", "to"):
        nid = edge.get(endpoint)
        if nid not in work["nodes"] or nid in removed_nodes:
            raise PatchError(f"add_edge endpoint {endpoint}={nid!r} does not exist")
    if any(e.get("id") == edge["id"] for e in work["edges"]):
        raise PatchError(f"add_edge: edge {edge['id']!r} already exists")
    if "map" not in edge:
        src_out = _first_port(work["nodes"][edge["from"]].get("outputs")) or "final_output"
        tgt_in = _first_port(work["nodes"][edge["to"]].get("inputs")) or "response"
        edge["map"] = {src_out: tgt_in}
    work["edges"].append(edge)


def _op_remove_edge(
    work: dict[str, Any],
    op: dict[str, Any],
    _removed_nodes: set[str],
    removed_edges: set[str],
) -> None:
    edge_id = op.get("target_edge_id")
    if not edge_id:
        raise PatchError("remove_edge requires 'target_edge_id'")
    before = len(work["edges"])
    work["edges"] = [e for e in work["edges"] if e.get("id") != edge_id]
    if len(work["edges"]) == before:
        raise PatchError(f"remove_edge: edge {edge_id!r} not found")
    removed_edges.add(edge_id)


def _op_update_tool_constraint(
    work: dict[str, Any],
    op: dict[str, Any],
    removed_nodes: set[str],
    _removed_edges: set[str],
) -> None:
    target_id = op.get("target_node_id")
    tool_ref = op.get("tool_ref")
    constraint = op.get("constraint")
    if not target_id or not tool_ref or not constraint:
        raise PatchError(
            "update_tool_constraint requires 'target_node_id', 'tool_ref', and 'constraint'"
        )
    node = _require_node(work, target_id, removed_nodes)
    if node.get("type") != "agent":
        raise PatchError(f"update_tool_constraint target {target_id!r} is not an agent node")
    constraints = node.setdefault("tool_constraints", {})
    constraints[tool_ref] = constraint


_HANDLERS = {
    "add_node_after": _op_add_node_after,
    "remove_node": _op_remove_node,
    "update_node_field": _op_update_node_field,
    "add_edge": _op_add_edge,
    "remove_edge": _op_remove_edge,
    "update_tool_constraint": _op_update_tool_constraint,
}


__all__ = ["PatchError", "apply_patch"]
