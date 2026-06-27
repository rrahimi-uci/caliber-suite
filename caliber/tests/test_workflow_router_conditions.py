"""Router condition evaluation + routing behavior tests."""

from __future__ import annotations

import pytest

from caliber.workflows.ir import IRRouter, IRRouterBranch, NodeType
from caliber.workflows.runtime import _condition_matches, _route


@pytest.mark.parametrize(
    ("condition", "context", "expected"),
    [
        ({"op": "contains", "value": "refund"}, "refund policy", True),
        ({"op": "mentions", "value": "refund"}, "refund policy", True),
        ({"op": "not_contains", "value": "billing"}, "refund policy", True),
        ({"op": "equals", "value": "refund"}, "refund", True),
        ({"op": "not_equals", "value": "billing"}, "refund", True),
        ({"op": "starts_with", "value": "ref"}, "refund", True),
        ({"op": "ends_with", "value": "und"}, "refund", True),
        ({"op": "regex", "value": r"^refund"}, "refund policy", True),
        ({"op": "regex", "value": "(["}, "refund policy", False),
        (
            {"op": "exists", "field": "metadata.ticket_id"},
            {"metadata": {"ticket_id": "T-100"}},
            True,
        ),
        (
            {"op": "exists", "field": "metadata.ticket_id"},
            {"metadata": {"ticket_id": ""}},
            False,
        ),
        ({"op": "gt", "field": "score", "value": 0.8}, {"score": 0.9}, True),
        ({"op": "gte", "field": "score", "value": 0.8}, {"score": 0.8}, True),
        ({"op": "lt", "field": "score", "value": 0.8}, {"score": 0.2}, True),
        ({"op": "lte", "field": "score", "value": 0.8}, {"score": 0.8}, True),
        (
            {"op": "in", "field": "intent", "value": ["Refund", "Billing"]},
            {"intent": "refund"},
            True,
        ),
        (
            {
                "op": "in",
                "field": "intent",
                "value": ["Refund", "Billing"],
                "case_sensitive": True,
            },
            {"intent": "refund"},
            False,
        ),
        ({"op": "in", "field": "intent", "value": "refund,billing"}, {"intent": "bill"}, True),
        ({"contains": "refund", "field": "input"}, {"input": "Refund policy"}, True),
        ({"op": "unknown", "value": "x"}, "text", False),
        ({"op": "contains"}, "text", False),
    ],
)
def test_condition_operator_matrix(condition, context, expected) -> None:
    assert _condition_matches(condition, context) is expected


@pytest.mark.parametrize(
    "condition",
    [
        {},
        {"field": "input"},
        {"not": "invalid"},
        {"all": []},
        {"any": []},
        {"all": [123]},
        {"any": [123]},
    ],
)
def test_malformed_conditions_fail_closed(condition) -> None:
    assert _condition_matches(condition, {"input": "refund policy"}) is False


def test_nested_logical_conditions() -> None:
    ctx = {
        "input": "refund request",
        "intent": "billing",
        "priority": "high",
        "output": "approve",
        "meta": {"score": 0.92},
    }
    condition = {
        "all": [
            {"op": "contains", "field": "input", "value": "refund"},
            {
                "any": [
                    {"op": "equals", "field": "intent", "value": "refund"},
                    {"op": "equals", "field": "priority", "value": "high"},
                ]
            },
            {"not": {"op": "contains", "field": "output", "value": "deny"}},
            {"op": "gt", "field": "meta.score", "value": 0.9},
        ]
    }
    assert _condition_matches(condition, ctx) is True

    ctx["output"] = "deny"
    assert _condition_matches(condition, ctx) is False


def test_route_uses_first_match_then_fallback() -> None:
    router = IRRouter(
        node_id="router",
        node_type=NodeType.ROUTER,
        branches=[
            IRRouterBranch({"op": "contains", "field": "input", "value": "refund"}, "queue_a"),
            IRRouterBranch(
                {"op": "contains", "field": "input", "value": "refund urgent"}, "queue_b"
            ),
            IRRouterBranch(None, "fallback"),
        ],
    )
    assert _route(router, {"input": "refund urgent"}) == "queue_a"
    assert _route(router, {"input": "other"}) == "fallback"


def test_route_skips_invalid_conditions() -> None:
    router = IRRouter(
        node_id="router",
        node_type=NodeType.ROUTER,
        branches=[
            IRRouterBranch({}, "bad"),
            IRRouterBranch({"not": "invalid"}, "also_bad"),
            IRRouterBranch({"contains": "refund", "field": "input"}, "queue_refund"),
            IRRouterBranch(None, "fallback"),
        ],
    )
    assert _route(router, {"input": "refund policy"}) == "queue_refund"
    assert _route(router, {"input": "shipping"}) == "fallback"


def test_route_defaults_to_first_branch_without_fallback() -> None:
    router = IRRouter(
        node_id="router",
        node_type=NodeType.ROUTER,
        branches=[
            IRRouterBranch({"op": "equals", "field": "input", "value": "billing"}, "billing"),
            IRRouterBranch({"op": "equals", "field": "input", "value": "refund"}, "refund"),
        ],
    )
    assert _route(router, {"input": "other"}) == "billing"
    assert _route(IRRouter(node_id="empty", node_type=NodeType.ROUTER), {"input": "other"}) is None
