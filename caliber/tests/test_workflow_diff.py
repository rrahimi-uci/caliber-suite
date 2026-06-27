"""Graph diff tests (plan §19.8)."""

from __future__ import annotations

from caliber.workflows.diff import compute_graph_diff
from caliber.workflows.manifest import parse_manifest
from tests.workflow_helpers import make_manifest


def _ids(items) -> list[str]:
    return [i["id"] for i in items]


def test_no_change_is_empty() -> None:
    m = parse_manifest(make_manifest())
    diff = compute_graph_diff(m, m)
    assert diff["empty"] is True


def test_added_node() -> None:
    base = parse_manifest(make_manifest())
    data = make_manifest()
    data["nodes"]["g"] = {"id": "g", "type": "note", "text": "hi"}
    cand = parse_manifest(data)
    diff = compute_graph_diff(base, cand)
    assert _ids(diff["added_nodes"]) == ["g"]


def test_removed_node() -> None:
    data = make_manifest()
    data["nodes"]["extra"] = {"id": "extra", "type": "note", "text": "hi"}
    base = parse_manifest(data)
    cand = parse_manifest(make_manifest())
    diff = compute_graph_diff(base, cand)
    assert _ids(diff["removed_nodes"]) == ["extra"]


def test_modified_node() -> None:
    base = parse_manifest(make_manifest())
    data = make_manifest()
    data["nodes"]["agent"]["model"] = "gpt-x"
    cand = parse_manifest(data)
    diff = compute_graph_diff(base, cand)
    assert _ids(diff["modified_nodes"]) == ["agent"]
    fields = {c["field"] for c in diff["modified_nodes"][0]["changes"]}
    assert "model" in fields


def test_added_and_removed_edges() -> None:
    base = parse_manifest(make_manifest())
    data = make_manifest()
    data["nodes"]["mid"] = {"id": "mid", "type": "note", "text": "x"}
    data["edges"].append({"id": "e3", "from": "start", "to": "mid", "map": {"msg": "input"}})
    cand = parse_manifest(data)
    diff = compute_graph_diff(base, cand)
    assert "e3" in diff["added_edges"]


def test_diff_is_order_independent() -> None:
    base = parse_manifest(make_manifest())
    data = make_manifest()
    data["edges"] = list(reversed(data["edges"]))
    cand = parse_manifest(data)
    diff = compute_graph_diff(base, cand)
    assert diff["empty"] is True


def test_prompt_artifact_change_captured() -> None:
    base_data = make_manifest(
        artifacts={"prompts": {"p": {"registry_name": "agent", "alias": "prod"}}}
    )
    base = parse_manifest(base_data)
    cand_data = make_manifest(
        artifacts={"prompts": {"p": {"registry_name": "agent", "alias": "candidate"}}}
    )
    cand = parse_manifest(cand_data)
    diff = compute_graph_diff(base, cand)
    assert any(c["kind"] == "prompt" for c in diff["artifact_changes"])
