"""Cookbook 03: the refund policy tool, its 49-case dataset and the workflow that gates it."""

from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml

from caliber.workflows.demo_tools import (
    REFUND_TIER_POLICY,
    REFUND_VELOCITY_LIMIT,
    decide_refund,
    initiate_refund,
)
from caliber.workflows.manifest import HumanApprovalNode, ToolNode, parse_manifest
from caliber.workflows.validation import validate_manifest

LAB = Path(__file__).resolve().parents[2] / "docs-site/cookbooks/03-tool-hardening-contract-lab"
DATASET = LAB / "assets/dataset/refund-fixtures.jsonl"


def _rows() -> list[dict[str, Any]]:
    return [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]


def _case(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "order_state": "delivered",
        "amount": 49.0,
        "days_since_order": 5,
        "customer_tier": "standard",
        "refunds_last_90d": 0,
        "risk_flags": [],
    }
    return {**base, **overrides}


@pytest.mark.parametrize("row", _rows(), ids=lambda row: row["id"])
def test_every_dataset_row_matches_the_policy(row: dict[str, Any]) -> None:
    assert decide_refund(**row["inputs"]) == row["expectations"]


def test_dataset_is_varied_enough_to_exercise_the_policy() -> None:
    rows = _rows()
    assert len(rows) >= 40
    assert len({row["id"] for row in rows}) == len(rows)
    tags = Counter(tag for row in rows for tag in row["tags"])
    for needed in ("boundary", "tier", "abuse", "malformed", "fail_closed", "window", "amount"):
        assert tags[needed] >= 2, needed
    reasons = {row["expectations"]["reason_code"] for row in rows}
    assert reasons >= {
        "ELIGIBLE_AUTO_APPROVE",
        "OUTSIDE_WINDOW",
        "AMOUNT_OVER_THRESHOLD",
        "NOT_REFUNDABLE_STATE",
        "RISK_FLAG_PRESENT",
        "MISSING_RISK_DATA",
        "UNKNOWN_ORDER_STATE",
        "REFUND_VELOCITY",
        "INVALID_AMOUNT",
        "INVALID_INPUT",
    }


@pytest.mark.parametrize(
    ("tier", "window", "limit"), [(t, w, a) for t, (w, a) in REFUND_TIER_POLICY.items()]
)
def test_tier_boundaries(tier: str, window: int, limit: float) -> None:
    assert (
        decide_refund(**_case(customer_tier=tier, days_since_order=window))["decision"] == "approve"
    )
    assert (
        decide_refund(**_case(customer_tier=tier, days_since_order=window + 1))["reason_code"]
        == "OUTSIDE_WINDOW"
    )
    assert decide_refund(**_case(customer_tier=tier, amount=limit))["decision"] == "approve"
    assert (
        decide_refund(**_case(customer_tier=tier, amount=limit + 0.01))["reason_code"]
        == "AMOUNT_OVER_THRESHOLD"
    )


def test_requires_approval_is_true_exactly_for_manual_review() -> None:
    for row in _rows():
        out = decide_refund(**row["inputs"])
        assert out["requires_approval"] is (out["decision"] == "manual_review")


def test_policy_is_deterministic_and_has_exactly_three_keys() -> None:
    first = decide_refund(**_case())
    assert first == decide_refund(**_case())
    assert set(first) == {"decision", "reason_code", "requires_approval"}


def test_rule_precedence_fails_closed_before_it_denies() -> None:
    expired_with_flag = decide_refund(**_case(days_since_order=400, risk_flags=["fraud_suspected"]))
    assert expired_with_flag["reason_code"] == "RISK_FLAG_PRESENT"
    serial = decide_refund(**_case(order_state="cancelled", refunds_last_90d=REFUND_VELOCITY_LIMIT))
    assert serial["reason_code"] == "REFUND_VELOCITY"
    assert decide_refund(**_case(order_state="cancelled"))["reason_code"] == "NOT_REFUNDABLE_STATE"


@pytest.mark.parametrize(
    "bad",
    [
        {"amount": "49"},
        {"amount": True},
        {"amount": None},
        {"amount": math.inf},
        {"amount": math.nan},
        {"days_since_order": -1},
        {"days_since_order": 1.5},
        {"days_since_order": False},
        {"customer_tier": "gold"},
        {"customer_tier": 3},
        {"refunds_last_90d": -2},
        {"refunds_last_90d": 1.0},
        {"risk_flags": "fraud"},
        {"risk_flags": [1]},
        {"order_state": None},
    ],
)
def test_malformed_input_fails_closed_to_manual_review(bad: dict[str, Any]) -> None:
    assert decide_refund(**_case(**bad)) == {
        "decision": "manual_review",
        "reason_code": "INVALID_INPUT",
        "requires_approval": True,
    }


def test_omitted_risk_flags_fail_closed() -> None:
    case = _case()
    del case["risk_flags"]
    assert decide_refund(**case)["reason_code"] == "MISSING_RISK_DATA"


def test_unknown_field_is_an_error_not_a_silent_decision() -> None:
    with pytest.raises(TypeError, match="unexpected keyword"):
        decide_refund(**_case(bogus=1))


def test_initiate_refund_reports_the_decided_amount_or_the_canned_total() -> None:
    assert initiate_refund("ord_1", amount=49.0) == {
        "order_id": "ord_1",
        "refund_status": "initiated",
        "amount_usd": 49.0,
    }
    assert initiate_refund("ord_1")["amount_usd"] == 1299.0
    assert initiate_refund(amount=0)["amount_usd"] == 0.0


def test_the_workflow_asset_parses_and_gates_the_write_by_the_tool_flag() -> None:
    data = yaml.safe_load((LAB / "assets/workflow/refund-decision.workflow.yaml").read_text())
    manifest = parse_manifest(data)
    tool_nodes = {n.tool_name for n in manifest.nodes.values() if isinstance(n, ToolNode)}
    assert tool_nodes == {"decide_refund", "initiate_refund"}
    # The gate is the write tool's own requires_approval, not a Human Approval node.
    assert not any(isinstance(n, HumanApprovalNode) for n in manifest.nodes.values())
    report = validate_manifest(manifest)
    assert not report.errors


def test_the_tool_assets_are_valid_registration_bodies() -> None:
    from caliber.schemas import ToolRegisterRequest

    for name in ("decide-refund", "initiate-refund"):
        body = json.loads((LAB / f"assets/tools/{name}.tool.json").read_text())
        request = ToolRegisterRequest.model_validate(body)
        assert request.module_path == "caliber.workflows.demo_tools"
    write = json.loads((LAB / "assets/tools/initiate-refund.tool.json").read_text())
    write.pop("requires_approval")
    with pytest.raises(ValueError, match="requires_approval"):
        ToolRegisterRequest.model_validate(write)
