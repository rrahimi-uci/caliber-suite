"""Unit tests for CALIBER workflow guardrail adapters."""

from __future__ import annotations

import pytest

from caliber.workflows.guardrails import (
    GuardrailBlockedError,
    GuardrailContext,
    assert_guardrail,
    enforce_guardrails,
    evaluate_guardrail,
    known_check_kinds,
)
from caliber.workflows.ir import IRGuardrail, IRGuardrailCheck, NodeType


def _guardrail(
    *checks: IRGuardrailCheck,
    node_id: str = "g1",
    on_failure: str = "block",
) -> IRGuardrail:
    return IRGuardrail(
        node_id=node_id,
        node_type=NodeType.GUARDRAIL,
        checks=list(checks),
        on_failure=on_failure,
    )


def test_called_tool_names_accepts_tool_or_name_keys() -> None:
    ctx = GuardrailContext(
        tool_calls=[
            {"tool": "lookup_policy"},
            {"name": "get_order"},
            {"tool": ""},
            {"other": "ignored"},
        ]
    )
    assert ctx.called_tool_names == {"lookup_policy", "get_order"}


def test_known_check_kinds_includes_builtin_checks() -> None:
    assert {
        "tool_required_before_claim",
        "non_empty_output",
        "max_length",
        "forbid_substring",
    }.issubset(known_check_kinds())


def test_unknown_check_returns_failed_result_without_raising() -> None:
    node = _guardrail(IRGuardrailCheck("does_not_exist"))
    [result] = evaluate_guardrail(node, GuardrailContext(response_text="ok"))
    assert result.passed is False
    assert "unknown guardrail check kind" in result.reason


def test_tool_required_before_claim_fails_without_grounding_tool() -> None:
    node = _guardrail(
        IRGuardrailCheck(
            "tool_required_before_claim",
            {"tool": "lookup_policy", "categories": ["refund_policy"]},
        )
    )
    [result] = evaluate_guardrail(node, GuardrailContext(response_text="Refunds are allowed."))
    assert result.passed is False
    assert "lookup_policy" in result.reason


def test_tool_required_before_claim_passes_when_no_claim_or_tool_called() -> None:
    node = _guardrail(
        IRGuardrailCheck(
            "tool_required_before_claim",
            {"tool": "lookup_policy", "categories": ["refund_policy"]},
        )
    )
    assert evaluate_guardrail(node, GuardrailContext(response_text="Hello"))[0].passed is True
    assert (
        evaluate_guardrail(
            node,
            GuardrailContext(
                response_text="Refunds are allowed.",
                tool_calls=[{"tool": "lookup_policy"}],
            ),
        )[0].passed
        is True
    )


def test_non_empty_max_length_and_forbid_substring_failures() -> None:
    empty = _guardrail(IRGuardrailCheck("non_empty_output"))
    assert evaluate_guardrail(empty, GuardrailContext(response_text="   "))[0].reason == (
        "response is empty"
    )

    too_long = _guardrail(IRGuardrailCheck("max_length", {"max_chars": 3}))
    assert (
        "exceeds limit"
        in evaluate_guardrail(too_long, GuardrailContext(response_text="abcd"))[0].reason
    )

    forbidden = _guardrail(IRGuardrailCheck("forbid_substring", {"substring": "secret"}))
    assert (
        "forbidden substring"
        in evaluate_guardrail(forbidden, GuardrailContext(response_text="contains SECRET"))[
            0
        ].reason
    )


def test_assert_guardrail_blocks_only_for_blocking_failure_modes() -> None:
    warning_node = _guardrail(IRGuardrailCheck("non_empty_output"), on_failure="warn")
    assert assert_guardrail(warning_node, GuardrailContext(response_text=""))[0].passed is False

    block_node = _guardrail(IRGuardrailCheck("non_empty_output"), node_id="blocker")
    with pytest.raises(GuardrailBlockedError) as excinfo:
        assert_guardrail(block_node, GuardrailContext(response_text=""))
    assert excinfo.value.node_id == "blocker"
    assert excinfo.value.reason == "response is empty"


def test_enforce_guardrails_blocks_and_passes_plain_specs() -> None:
    specs = [
        {
            "node_id": "g",
            "checks": [{"kind": "forbid_substring", "params": {"substring": "secret"}}],
        }
    ]
    enforce_guardrails("public answer", [], specs)
    with pytest.raises(GuardrailBlockedError, match="forbidden substring"):
        enforce_guardrails("secret answer", [], specs)


# --- Detection checks (Studio inspector vocabulary) --------------------------


def test_detection_check_kinds_registered() -> None:
    assert {"pii_detection", "toxicity_check", "budget_limit", "schema_validation"}.issubset(
        known_check_kinds()
    )


def test_pii_detection_flags_and_passes() -> None:
    node = _guardrail(IRGuardrailCheck("pii_detection", {"entities": ["email", "ssn"]}))
    [hit] = evaluate_guardrail(node, GuardrailContext(response_text="reach me at a@b.com"))
    assert hit.passed is False and "email" in hit.reason
    [clean] = evaluate_guardrail(node, GuardrailContext(response_text="no contact details here"))
    assert clean.passed is True


def test_toxicity_check_flags_and_passes() -> None:
    node = _guardrail(IRGuardrailCheck("toxicity_check", {"threshold": 0.7}))
    [hit] = evaluate_guardrail(node, GuardrailContext(response_text="you are an idiot"))
    assert hit.passed is False
    [clean] = evaluate_guardrail(node, GuardrailContext(response_text="happy to help!"))
    assert clean.passed is True


def test_budget_limit_uses_tool_costs_and_text_fallback() -> None:
    node = _guardrail(IRGuardrailCheck("budget_limit", {"max_usd": 100}))
    over = evaluate_guardrail(
        node, GuardrailContext(response_text="", tool_calls=[{"cost_usd": 150}])
    )[0]
    assert over.passed is False and "exceeds budget" in over.reason
    under = evaluate_guardrail(node, GuardrailContext(response_text="total is $42.00"))[0]
    assert under.passed is True
    # No configured limit always passes.
    assert (
        evaluate_guardrail(
            _guardrail(IRGuardrailCheck("budget_limit", {})),
            GuardrailContext(response_text="$9999"),
        )[0].passed
        is True
    )


def test_schema_validation_requires_fields_for_json_objects() -> None:
    node = _guardrail(
        IRGuardrailCheck("schema_validation", {"required_fields": ["product_id", "total"]})
    )
    ok = evaluate_guardrail(
        node, GuardrailContext(response_text='{"product_id": "X", "total": 5}')
    )[0]
    assert ok.passed is True
    missing = evaluate_guardrail(node, GuardrailContext(response_text='{"product_id": "X"}'))[0]
    assert missing.passed is False and "total" in missing.reason
    # Non-JSON / free-text can't be validated and passes (safe on text branches).
    assert evaluate_guardrail(node, GuardrailContext(response_text="just prose"))[0].passed is True


def test_budget_limit_counts_each_call_once_across_cost_aliases() -> None:
    """Regression (#19): cost_usd / amount_usd / total_usd are aliases for the
    same spend, so a single call reporting two of them must count ONCE — summing
    them double-counted and falsely tripped the budget."""
    from caliber.workflows.guardrails import _budget_limit

    one_call = GuardrailContext(tool_calls=[{"cost_usd": 6.0, "total_usd": 6.0}])
    assert _budget_limit({"max_usd": 10.0}, one_call).passed is True  # 6, not 12
    two_calls = GuardrailContext(tool_calls=[{"cost_usd": 6.0}, {"cost_usd": 6.0}])
    assert _budget_limit({"max_usd": 10.0}, two_calls).passed is False  # 12 > 10


def test_enforce_guardrails_skips_pre_agent_guards_at_output_time() -> None:
    """Regression (#27): enforce_guardrails screens the agent OUTPUT, so a
    pre_agent (input-screening) guard must NOT run here — it used to block
    legitimate output that merely quoted an input-only forbidden phrase."""
    pre = [
        {
            "node_id": "g-pre",
            "mode": "pre_agent",
            "on_failure": "block",
            "checks": [{"kind": "forbid_substring", "params": {"substring": "secret-input-token"}}],
        }
    ]
    # Output contains the input-only forbidden phrase — must NOT raise.
    enforce_guardrails("the answer mentions secret-input-token", [], pre)

    post = [dict(pre[0], node_id="g-post", mode="post_agent")]
    with pytest.raises(GuardrailBlockedError):
        enforce_guardrails("the answer mentions secret-input-token", [], post)
