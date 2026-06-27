"""Tests covering remaining gaps in validation.py (93% → higher).

Targets: edge_bad_source, edge_bad_target, empty-start reachability
fast-return, handoff_non_agent, unknown_guardrail_check, output_unreachable.
"""

from __future__ import annotations

from caliber.workflows.manifest import parse_manifest
from caliber.workflows.validation import validate_manifest
from tests.workflow_helpers import make_manifest, make_support_manifest


def _codes(report) -> list[str]:
    return [i.code for i in report.errors]


def _warning_codes(report) -> list[str]:
    return [w.code for w in report.warnings]


# ------------------------------------------------------------------
# edge_bad_source / edge_bad_target (lines 209-214, 216-221)
# ------------------------------------------------------------------


def test_edge_source_references_nonexistent_node() -> None:
    """An edge whose 'from' field points at a node not in the manifest."""
    data = make_manifest()
    data["edges"].append(
        {
            "id": "e_bad",
            "from": "GHOST",
            "to": "agent",
            "map": {"x": "input"},
        }
    )
    report = validate_manifest(parse_manifest(data))
    assert "edge_bad_source" in _codes(report)


def test_edge_target_references_nonexistent_node() -> None:
    """An edge whose 'to' field points at a node not in the manifest."""
    data = make_manifest()
    data["edges"].append(
        {
            "id": "e_bad",
            "from": "agent",
            "to": "GHOST",
            "map": {"final_output": "x"},
        }
    )
    report = validate_manifest(parse_manifest(data))
    assert "edge_bad_target" in _codes(report)


# ------------------------------------------------------------------
# handoff_non_agent (line 373)
# ------------------------------------------------------------------


def test_handoff_to_non_agent_node_rejected() -> None:
    """Agent hands off to an output node (not an agent) → error."""
    data = make_manifest()
    data["nodes"]["agent"]["handoffs"] = [{"target": "final"}]
    report = validate_manifest(parse_manifest(data))
    assert "handoff_non_agent" in _codes(report)


# ------------------------------------------------------------------
# handoff_bad_target (line 367)
# ------------------------------------------------------------------


def test_handoff_to_missing_node_rejected() -> None:
    """Agent hands off to a node that doesn't exist → error."""
    data = make_manifest()
    data["nodes"]["agent"]["handoffs"] = [{"target": "NOPE"}]
    report = validate_manifest(parse_manifest(data))
    assert "handoff_bad_target" in _codes(report)


# ------------------------------------------------------------------
# unknown_guardrail_check (line 471)
# ------------------------------------------------------------------


def test_unknown_guardrail_check_kind_warns() -> None:
    """A guardrail with an unknown check kind produces a warning."""
    data = make_support_manifest()
    data["nodes"]["policy_guardrail"]["checks"] = [{"totally_unknown_check_kind": {"foo": "bar"}}]
    report = validate_manifest(parse_manifest(data))
    assert any(w.code == "unknown_guardrail_check" for w in report.warnings)


# ------------------------------------------------------------------
# output_unreachable (line 252)
# ------------------------------------------------------------------


def test_output_unreachable_when_edge_skips_output() -> None:
    """Start reaches agent, but agent is not connected to output → unreachable."""
    data = make_manifest()
    # Remove the edge from agent to final
    data["edges"] = [e for e in data["edges"] if e["to"] != "final"]
    report = validate_manifest(parse_manifest(data))
    assert "output_unreachable" in _codes(report)


# ------------------------------------------------------------------
# deprecated_tool warning (line 411-412 area)
# ------------------------------------------------------------------


def test_deprecated_tool_warning() -> None:
    """Resolver marks a tool as deprecated → warning."""
    from caliber.workflows.tools import InMemoryToolResolver, ToolRegistryEntry

    resolver = InMemoryToolResolver(
        [
            ToolRegistryEntry(
                name="lookup_policy",
                version="1.5",
                module_path="m",
                callable_name="f",
                status="deprecated",
            ),
            ToolRegistryEntry(name="get_order", version="1.0", module_path="m", callable_name="g"),
            ToolRegistryEntry(
                name="escalate",
                version="1.0",
                module_path="m",
                callable_name="e",
                side_effect_level="external_action",
            ),
        ]
    )
    data = make_support_manifest()
    report = validate_manifest(parse_manifest(data), resolver=resolver)
    assert any(w.code == "deprecated_tool" for w in report.warnings)
