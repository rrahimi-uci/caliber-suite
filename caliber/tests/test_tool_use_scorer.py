"""Tests for the tool-use scorer.

Validates scoring rules, edge cases, and integration with the eval
provider types — matching support-agent eval dataset patterns.
"""

from __future__ import annotations

from caliber.eval.tool_use_scorer import (
    ToolUseResult,
    ToolUseScorer,
    ToolUseScorerResult,
    score_tool_use,
)

# ---------------------------------------------------------------------------
# score_tool_use — core logic
# ---------------------------------------------------------------------------


class TestScoreToolUse:
    """Unit tests for :func:`score_tool_use`."""

    def test_expected_tool_present_scores_one(self) -> None:
        examples = [{"input": "refund?", "expected_tool": "lookup_policy"}]
        responses = [{"output": "...", "tool_calls": ["lookup_policy"]}]
        result = score_tool_use(examples, responses)
        assert result.overall == 1.0
        assert result.passed == 1
        assert result.failed == 0

    def test_expected_tool_missing_scores_zero(self) -> None:
        examples = [{"input": "refund?", "expected_tool": "lookup_policy"}]
        responses = [{"output": "I think the policy is...", "tool_calls": []}]
        result = score_tool_use(examples, responses)
        assert result.overall == 0.0
        assert result.passed == 0
        assert result.failed == 1

    def test_wrong_tool_called_scores_zero(self) -> None:
        examples = [{"input": "refund?", "expected_tool": "lookup_policy"}]
        responses = [{"output": "...", "tool_calls": ["get_order_status"]}]
        result = score_tool_use(examples, responses)
        assert result.overall == 0.0
        assert result.failed == 1

    def test_no_expected_tool_scores_one(self) -> None:
        examples = [{"input": "who are you?", "expected_tool": None}]
        responses = [{"output": "I'm a support agent"}]
        result = score_tool_use(examples, responses)
        assert result.overall == 1.0
        assert result.skipped == 1

    def test_empty_expected_tool_string_scores_one(self) -> None:
        examples = [{"input": "who are you?", "expected_tool": ""}]
        responses = ["I'm a support agent"]
        result = score_tool_use(examples, responses)
        assert result.overall == 1.0
        assert result.skipped == 1

    def test_missing_expected_tool_key_scores_one(self) -> None:
        examples = [{"input": "general question"}]
        responses = ["Sure, I can help."]
        result = score_tool_use(examples, responses)
        assert result.overall == 1.0
        assert result.skipped == 1

    def test_plain_string_response_no_tool_calls(self) -> None:
        examples = [{"input": "refund?", "expected_tool": "lookup_policy"}]
        responses = ["The refund policy is 14 days."]
        result = score_tool_use(examples, responses)
        assert result.overall == 0.0
        assert result.failed == 1
        assert result.details[0].actual == []

    def test_multiple_tool_calls_includes_expected(self) -> None:
        examples = [{"input": "order + refund", "expected_tool": "lookup_policy"}]
        responses = [{"tool_calls": ["get_order_status", "lookup_policy"]}]
        result = score_tool_use(examples, responses)
        assert result.overall == 1.0
        assert result.passed == 1

    def test_batch_mixed_results(self) -> None:
        examples = [
            {"input": "refund?", "expected_tool": "lookup_policy"},
            {"input": "order?", "expected_tool": "get_order_status"},
            {"input": "hello", "expected_tool": None},
            {"input": "angry!", "expected_tool": "escalate_to_human"},
        ]
        responses = [
            {"tool_calls": ["lookup_policy"]},
            {"tool_calls": []},
            "Hi there!",
            {"tool_calls": ["escalate_to_human"]},
        ]
        result = score_tool_use(examples, responses)
        # 3 pass (1.0 each) + 1 fail (0.0) = 3/4 = 0.75
        assert result.overall == 0.75
        assert result.passed == 2
        assert result.failed == 1
        assert result.skipped == 1
        assert result.total == 4

    def test_empty_batch(self) -> None:
        result = score_tool_use([], [])
        assert result.overall == 0.0
        assert result.total == 0

    def test_all_pass(self) -> None:
        examples = [
            {"expected_tool": "lookup_policy"},
            {"expected_tool": "get_order_status"},
            {"expected_tool": "escalate_to_human"},
        ]
        responses = [
            {"tool_calls": ["lookup_policy"]},
            {"tool_calls": ["get_order_status"]},
            {"tool_calls": ["escalate_to_human"]},
        ]
        result = score_tool_use(examples, responses)
        assert result.overall == 1.0
        assert result.passed == 3
        assert result.failed == 0

    def test_all_fail(self) -> None:
        examples = [
            {"expected_tool": "lookup_policy"},
            {"expected_tool": "get_order_status"},
        ]
        responses = [
            {"tool_calls": []},
            {"tool_calls": ["lookup_policy"]},  # wrong tool
        ]
        result = score_tool_use(examples, responses)
        assert result.overall == 0.0
        assert result.failed == 2

    def test_result_details_have_reasons(self) -> None:
        examples = [
            {"expected_tool": "lookup_policy"},
            {"expected_tool": None},
        ]
        responses = [
            {"tool_calls": ["lookup_policy"]},
            "hello",
        ]
        result = score_tool_use(examples, responses)
        assert result.details[0].reason == "expected tool was called"
        assert result.details[1].reason == "no tool expectation"


# ---------------------------------------------------------------------------
# ToolUseScorerResult — data class
# ---------------------------------------------------------------------------


class TestToolUseScorerResult:
    def test_fields(self) -> None:
        r = ToolUseScorerResult(overall=0.75, total=4, passed=2, failed=1, skipped=1)
        assert r.overall == 0.75
        assert r.total == 4
        assert r.details == []

    def test_detail_entries(self) -> None:
        d = ToolUseResult(
            expected="lookup_policy",
            actual=["lookup_policy"],
            score=1.0,
            reason="expected tool was called",
        )
        assert d.expected == "lookup_policy"
        assert d.score == 1.0


# ---------------------------------------------------------------------------
# ToolUseScorer — MLflow-compatible scorer class
# ---------------------------------------------------------------------------


class TestToolUseScorer:
    def test_name(self) -> None:
        scorer = ToolUseScorer()
        assert scorer.name == "tool_use"

    def test_call_returns_metric_dict(self) -> None:
        scorer = ToolUseScorer()
        result = scorer(
            inputs=[
                {"expected_tool": "lookup_policy"},
                {"expected_tool": "get_order_status"},
            ],
            predictions=[
                {"tool_calls": ["lookup_policy"]},
                {"tool_calls": []},
            ],
        )
        assert "tool_use/mean" in result
        assert result["tool_use/mean"] == 0.5

    def test_call_with_none_inputs(self) -> None:
        scorer = ToolUseScorer()
        result = scorer(inputs=None, predictions=None)
        assert result == {"tool_use/mean": 0.0}

    def test_call_all_pass(self) -> None:
        scorer = ToolUseScorer()
        result = scorer(
            inputs=[{"expected_tool": "lookup_policy"}],
            predictions=[{"tool_calls": ["lookup_policy"]}],
        )
        assert result["tool_use/mean"] == 1.0

    def test_call_no_tool_expectations(self) -> None:
        scorer = ToolUseScorer()
        result = scorer(
            inputs=[{"input": "hi"}, {"input": "bye"}],
            predictions=["hello", "goodbye"],
        )
        assert result["tool_use/mean"] == 1.0


# ---------------------------------------------------------------------------
# Integration with eval provider types
# ---------------------------------------------------------------------------


class TestToolUseScorerIntegration:
    """Tests validating the scorer works with CALIBER's eval data shapes."""

    def test_support_style_dataset(self) -> None:
        """Simulates a support-agent eval dataset shape."""
        examples = [
            {
                "input": "What is your refund policy?",
                "expected_output": "14 calendar days",
                "expected_tool": "lookup_policy",
                "category": "policy_lookup",
            },
            {
                "input": "Where is my order ORD-10421?",
                "expected_output": "delivered",
                "expected_tool": "get_order_status",
                "category": "order_status",
            },
            {
                "input": "I want to speak to a manager RIGHT NOW!",
                "expected_output": "escalating",
                "expected_tool": "escalate_to_human",
                "category": "escalation",
            },
            {
                "input": "What color is the sky?",
                "expected_output": "out of scope",
                "expected_tool": None,
                "category": "out_of_scope",
            },
        ]
        responses = [
            {"output": "14-day refund window", "tool_calls": ["lookup_policy"]},
            {"output": "Delivered", "tool_calls": ["get_order_status"]},
            {"output": "Transferring...", "tool_calls": ["escalate_to_human"]},
            "I can only help with support questions.",
        ]
        result = score_tool_use(examples, responses)
        assert result.overall == 1.0
        assert result.passed == 3
        assert result.skipped == 1

    def test_category_distribution_analysis(self) -> None:
        """Validates that tool-use results can be grouped by category."""
        examples = [
            {"category": "policy_lookup", "expected_tool": "lookup_policy"},
            {"category": "policy_lookup", "expected_tool": "lookup_policy"},
            {"category": "order_status", "expected_tool": "get_order_status"},
            {"category": "escalation", "expected_tool": "escalate_to_human"},
        ]
        responses = [
            {"tool_calls": ["lookup_policy"]},
            {"tool_calls": []},
            {"tool_calls": ["get_order_status"]},
            {"tool_calls": ["escalate_to_human"]},
        ]
        result = score_tool_use(examples, responses)

        # Group details by category
        by_category: dict[str, list[float]] = {}
        for ex, detail in zip(examples, result.details):
            cat = ex.get("category", "unknown")
            by_category.setdefault(cat, []).append(detail.score)

        # Policy lookup: 1 pass + 1 fail = 0.5
        assert sum(by_category["policy_lookup"]) / len(by_category["policy_lookup"]) == 0.5
        # Order status: 1 pass = 1.0
        assert sum(by_category["order_status"]) / len(by_category["order_status"]) == 1.0
        # Escalation: 1 pass = 1.0
        assert sum(by_category["escalation"]) / len(by_category["escalation"]) == 1.0


def test_tool_use_scorer_per_row_mlflow_signature() -> None:
    """Regression (#23): MLflow calls scorers per row with single outputs/
    expectations and no batched predictions. That path must reflect the real
    score, not fabricate 0.0."""
    scorer = ToolUseScorer()
    score = scorer(
        inputs={"expected_tool": "search"},
        outputs={"tool_calls": ["search"]},
        expectations={"expected_tool": "search"},
    )
    assert score == 1.0
    miss = scorer(
        outputs={"tool_calls": ["other"]},
        expectations={"expected_tool": "search"},
    )
    assert miss == 0.0
