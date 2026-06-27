"""Semantic graph diff for the approval UI (plan §12.4, §16.7.8, §19.8).

Computes an order-independent diff between two manifests so reviewers see graph
changes (added/removed/changed nodes and edges), artifact changes (prompt refs,
tool sets), and deploy-gate threshold changes — not a raw JSON text diff.

The diff is keyed by stable ids, so reordering nodes/edges produces an empty
diff. The output is plain JSON-able dicts, suitable for direct serialization
and for golden snapshot tests.
"""

from __future__ import annotations

from typing import Any

from caliber.workflows.manifest import WorkflowManifest


def _node_dict(manifest: WorkflowManifest) -> dict[str, dict[str, Any]]:
    return {
        nid: node.model_dump(mode="json", by_alias=True, exclude_none=True)
        for nid, node in manifest.nodes.items()
    }


def _edge_dict(manifest: WorkflowManifest) -> dict[str, dict[str, Any]]:
    return {
        edge.id: edge.model_dump(mode="json", by_alias=True, exclude_none=True)
        for edge in manifest.edges
    }


def _field_changes(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for key in sorted(set(before) | set(after)):
        old = before.get(key)
        new = after.get(key)
        if old != new:
            changes.append({"field": key, "from": old, "to": new})
    return changes


def compute_graph_diff(
    base: WorkflowManifest,
    candidate: WorkflowManifest,
) -> dict[str, Any]:
    """Return a structured, order-independent diff between two manifests."""
    base_nodes = _node_dict(base)
    cand_nodes = _node_dict(candidate)
    base_edges = _edge_dict(base)
    cand_edges = _edge_dict(candidate)

    added_nodes = sorted(set(cand_nodes) - set(base_nodes))
    removed_nodes = sorted(set(base_nodes) - set(cand_nodes))
    modified_nodes: list[dict[str, Any]] = []
    for nid in sorted(set(base_nodes) & set(cand_nodes)):
        if base_nodes[nid] != cand_nodes[nid]:
            modified_nodes.append(
                {"id": nid, "changes": _field_changes(base_nodes[nid], cand_nodes[nid])}
            )

    added_edges = sorted(set(cand_edges) - set(base_edges))
    removed_edges = sorted(set(base_edges) - set(cand_edges))
    modified_edges: list[dict[str, Any]] = []
    for eid in sorted(set(base_edges) & set(cand_edges)):
        if base_edges[eid] != cand_edges[eid]:
            modified_edges.append(
                {"id": eid, "changes": _field_changes(base_edges[eid], cand_edges[eid])}
            )

    artifact_changes = _artifact_changes(base, candidate)
    deploy_gate_changes = _deploy_gate_changes(base, candidate)

    return {
        "added_nodes": [{"id": nid, "type": cand_nodes[nid].get("type")} for nid in added_nodes],
        "removed_nodes": [
            {"id": nid, "type": base_nodes[nid].get("type")} for nid in removed_nodes
        ],
        "modified_nodes": modified_nodes,
        "added_edges": added_edges,
        "removed_edges": removed_edges,
        "modified_edges": modified_edges,
        "artifact_changes": artifact_changes,
        "deploy_gate_changes": deploy_gate_changes,
        "empty": not any(
            [
                added_nodes,
                removed_nodes,
                modified_nodes,
                added_edges,
                removed_edges,
                modified_edges,
                artifact_changes,
                deploy_gate_changes,
            ]
        ),
    }


def _artifact_changes(base: WorkflowManifest, candidate: WorkflowManifest) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    base_prompts = {k: v.model_dump(mode="json") for k, v in base.artifacts.prompts.items()}
    cand_prompts = {k: v.model_dump(mode="json") for k, v in candidate.artifacts.prompts.items()}
    for key in sorted(set(base_prompts) | set(cand_prompts)):
        old = base_prompts.get(key)
        new = cand_prompts.get(key)
        if old != new:
            changes.append({"kind": "prompt", "ref": key, "from": old, "to": new})
    return changes


def _deploy_gate_changes(
    base: WorkflowManifest, candidate: WorkflowManifest
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    base_gates = {k: v.model_dump(mode="json") for k, v in base.deploy_gates.items()}
    cand_gates = {k: v.model_dump(mode="json") for k, v in candidate.deploy_gates.items()}
    for key in sorted(set(base_gates) | set(cand_gates)):
        old = base_gates.get(key)
        new = cand_gates.get(key)
        if old != new:
            changes.append({"name": key, "from": old, "to": new})
    return changes


__all__ = ["compute_graph_diff"]
