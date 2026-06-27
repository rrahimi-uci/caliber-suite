"""Semantic patch tests (plan §19.6)."""

from __future__ import annotations

import pytest

from caliber.workflows.manifest import compute_manifest_hash
from caliber.workflows.patch import PatchError, apply_patch
from tests.workflow_helpers import make_manifest


def _base() -> dict:
    return make_manifest()


GUARDRAIL_NODE = {
    "id": "guard",
    "type": "guardrail",
    "mode": "post_agent",
    "inputs": {"response": {"type": "string"}},
    "outputs": {"passthrough": {"type": "string"}},
    "checks": [{"non_empty_output": {}}],
}


def test_add_node_after_inserts_and_rewires() -> None:
    base = _base()
    result = apply_patch(
        base,
        [{"op": "add_node_after", "target_node_id": "agent", "node": GUARDRAIL_NODE}],
    )
    assert "guard" in result.nodes
    # agent -> guard exists; guard -> final exists; agent no longer goes to final
    froms = {(e.from_, e.to) for e in result.edges}
    assert ("agent", "guard") in froms
    assert ("guard", "final") in froms
    assert ("agent", "final") not in froms


def test_add_node_after_missing_target() -> None:
    with pytest.raises(PatchError):
        apply_patch(
            _base(), [{"op": "add_node_after", "target_node_id": "ghost", "node": GUARDRAIL_NODE}]
        )


def test_update_node_field_changes_model() -> None:
    result = apply_patch(
        _base(),
        [
            {
                "op": "update_node_field",
                "target_node_id": "agent",
                "field_path": "model",
                "value": "gpt-x",
            }
        ],
    )
    assert result.nodes["agent"].model == "gpt-x"


def test_update_node_field_missing_node() -> None:
    with pytest.raises(PatchError):
        apply_patch(
            _base(),
            [
                {
                    "op": "update_node_field",
                    "target_node_id": "ghost",
                    "field_path": "model",
                    "value": "x",
                }
            ],
        )


def test_remove_edge() -> None:
    base = _base()
    # add guardrail then remove the auto-created edge would orphan; instead test simple remove + re-add
    result = apply_patch(
        base,
        [
            {"op": "add_node_after", "target_node_id": "agent", "node": GUARDRAIL_NODE},
            {"op": "remove_edge", "target_edge_id": "e_agent_guard"},
            {
                "op": "add_edge",
                "edge": {
                    "id": "e_new",
                    "from": "agent",
                    "to": "guard",
                    "map": {"final_output": "response"},
                },
            },
        ],
    )
    assert any(e.id == "e_new" for e in result.edges)


def test_remove_edge_missing() -> None:
    with pytest.raises(PatchError):
        apply_patch(_base(), [{"op": "remove_edge", "target_edge_id": "ghost"}])


def test_add_edge_type_mismatch_fails() -> None:
    base = _base()
    base["nodes"]["g"] = {
        "id": "g",
        "type": "guardrail",
        "mode": "post_agent",
        "inputs": {"response": {"type": "structured"}},
        "outputs": {"passthrough": {"type": "string"}},
        "checks": [],
    }
    with pytest.raises(PatchError):
        apply_patch(
            base,
            [
                {
                    "op": "add_edge",
                    "edge": {
                        "id": "ex",
                        "from": "agent",
                        "to": "g",
                        "map": {"final_output": "response"},
                    },
                }
            ],
        )


def test_update_tool_constraint() -> None:
    result = apply_patch(
        _base(),
        [
            {
                "op": "update_tool_constraint",
                "target_node_id": "agent",
                "tool_ref": "lookup_policy",
                "constraint": "required_before_claim",
            }
        ],
    )
    assert result.nodes["agent"].tool_constraints["lookup_policy"] == "required_before_claim"


def test_multiple_ops_compose() -> None:
    result = apply_patch(
        _base(),
        [
            {
                "op": "update_node_field",
                "target_node_id": "agent",
                "field_path": "model",
                "value": "m2",
            },
            {"op": "add_node_after", "target_node_id": "agent", "node": GUARDRAIL_NODE},
        ],
    )
    assert result.nodes["agent"].model == "m2"
    assert "guard" in result.nodes


def test_conflict_remove_then_update() -> None:
    with pytest.raises(PatchError):
        apply_patch(
            _base(),
            [
                {"op": "remove_node", "target_node_id": "agent"},
                {
                    "op": "update_node_field",
                    "target_node_id": "agent",
                    "field_path": "model",
                    "value": "x",
                },
            ],
        )


def test_stale_base_detected() -> None:
    with pytest.raises(PatchError):
        apply_patch(
            _base(),
            [
                {
                    "op": "update_node_field",
                    "target_node_id": "agent",
                    "field_path": "model",
                    "value": "x",
                }
            ],
            base_hash="deadbeef",
        )


def test_matching_base_hash_ok() -> None:
    base = _base()
    h = compute_manifest_hash(base)
    result = apply_patch(
        base,
        [
            {
                "op": "update_node_field",
                "target_node_id": "agent",
                "field_path": "model",
                "value": "x",
            }
        ],
        base_hash=h,
    )
    assert result.nodes["agent"].model == "x"


def test_unknown_op_rejected() -> None:
    with pytest.raises(PatchError):
        apply_patch(_base(), [{"op": "frobnicate"}])


def test_missing_op_rejected() -> None:
    with pytest.raises(PatchError, match="missing 'op'"):
        apply_patch(_base(), [{}])


def test_invalid_manifest_after_patch_is_wrapped() -> None:
    with pytest.raises(PatchError, match="invalid manifest"):
        apply_patch({"nodes": {}, "edges": []}, [])


def test_add_node_after_validates_required_fields_and_conflicts() -> None:
    with pytest.raises(PatchError, match="requires 'target_node_id'"):
        apply_patch(_base(), [{"op": "add_node_after", "node": GUARDRAIL_NODE}])

    node_without_id = dict(GUARDRAIL_NODE)
    node_without_id.pop("id")
    with pytest.raises(PatchError, match="missing 'id'"):
        apply_patch(
            _base(),
            [{"op": "add_node_after", "target_node_id": "agent", "node": node_without_id}],
        )

    duplicate = dict(GUARDRAIL_NODE, id="agent")
    with pytest.raises(PatchError, match="already exists"):
        apply_patch(
            _base(),
            [{"op": "add_node_after", "target_node_id": "agent", "node": duplicate}],
        )


def test_remove_node_and_removed_conflict_paths() -> None:
    with pytest.raises(PatchError, match="requires 'target_node_id'"):
        apply_patch(_base(), [{"op": "remove_node"}])

    with pytest.raises(PatchError, match="removed earlier"):
        apply_patch(
            _base(),
            [
                {"op": "remove_node", "target_node_id": "agent"},
                {
                    "op": "add_node_after",
                    "target_node_id": "agent",
                    "node": GUARDRAIL_NODE,
                },
            ],
        )


def test_update_node_field_validates_path_and_value() -> None:
    with pytest.raises(PatchError, match="requires 'target_node_id'"):
        apply_patch(_base(), [{"op": "update_node_field", "field_path": "model", "value": "x"}])

    with pytest.raises(PatchError, match="requires 'value'"):
        apply_patch(
            _base(),
            [{"op": "update_node_field", "target_node_id": "agent", "field_path": "model"}],
        )

    with pytest.raises(PatchError, match="traverses a non-object"):
        apply_patch(
            _base(),
            [
                {
                    "op": "update_node_field",
                    "target_node_id": "agent",
                    "field_path": "model.name",
                    "value": "x",
                }
            ],
        )


def test_add_edge_validates_shape_endpoints_and_defaults_map() -> None:
    with pytest.raises(PatchError, match="requires an 'edge' object"):
        apply_patch(_base(), [{"op": "add_edge"}])

    with pytest.raises(PatchError, match="missing 'id'"):
        apply_patch(_base(), [{"op": "add_edge", "edge": {"from": "agent", "to": "final"}}])

    with pytest.raises(PatchError, match="does not exist"):
        apply_patch(
            _base(),
            [{"op": "add_edge", "edge": {"id": "ghost", "from": "agent", "to": "missing"}}],
        )

    with pytest.raises(PatchError, match="already exists"):
        apply_patch(
            _base(),
            [{"op": "add_edge", "edge": {"id": "e1", "from": "agent", "to": "final"}}],
        )

    result = apply_patch(
        _base(),
        [
            {"op": "remove_edge", "target_edge_id": "e2"},
            {"op": "add_edge", "edge": {"id": "e_default", "from": "agent", "to": "final"}},
        ],
    )
    edge = next(e for e in result.edges if e.id == "e_default")
    assert edge.map == {"final_output": "response"}


def test_remove_edge_requires_target_id() -> None:
    with pytest.raises(PatchError, match="requires 'target_edge_id'"):
        apply_patch(_base(), [{"op": "remove_edge"}])


def test_update_tool_constraint_validates_required_fields_and_agent_target() -> None:
    with pytest.raises(PatchError, match="requires 'target_node_id'"):
        apply_patch(
            _base(),
            [
                {
                    "op": "update_tool_constraint",
                    "tool_ref": "lookup_policy",
                    "constraint": "required",
                }
            ],
        )

    with pytest.raises(PatchError, match="not an agent"):
        apply_patch(
            _base(),
            [
                {
                    "op": "update_tool_constraint",
                    "target_node_id": "final",
                    "tool_ref": "lookup_policy",
                    "constraint": "required",
                }
            ],
        )
