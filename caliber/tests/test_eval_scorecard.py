"""Unit tests for the deterministic scorecard engine.

The engine is pure given its ``predict`` callable, so these tests inject a
fake predictor and assert exact scores — no LLM, no DB.
"""

from __future__ import annotations

import pytest

from caliber.eval.scorecard import (
    DEFAULT_SCORERS,
    expected_text,
    resolve_scorers,
    run_scorecard,
)

_EXAMPLES = [
    {"example_id": "EX-1", "input": {"question": "capital of France"}, "expected": {"expected": "Paris"}},
    {"example_id": "EX-2", "input": {"question": "2+2"}, "expected": {"expected": "4"}},
]


def _fixed_predict(answer: str):
    def predict(_inputs):
        return answer

    return predict


def test_exact_match_perfect_and_zero() -> None:
    result = run_scorecard(_EXAMPLES, _fixed_predict("Paris"), ["exact_match"])
    by_id = {row.example_id: row for row in result.rows}
    assert by_id["EX-1"].scores["exact_match"] == 1.0
    assert by_id["EX-2"].scores["exact_match"] == 0.0
    assert result.aggregate["exact_match"] == 0.5
    assert result.passed_count == 1
    assert result.failed_count == 1
    assert result.overall == 0.5
    assert result.pass_rate == 0.5


def test_exact_match_is_case_and_whitespace_insensitive() -> None:
    examples = [{"example_id": "EX-1", "input": {}, "expected": {"expected": "Paris"}}]
    result = run_scorecard(examples, _fixed_predict("  paris\n"), ["exact_match"])
    assert result.rows[0].scores["exact_match"] == 1.0


def test_token_f1_partial_overlap() -> None:
    examples = [
        {"example_id": "EX-1", "input": {}, "expected": {"expected": "the quick brown fox"}}
    ]
    # prediction shares 2 of its 3 tokens with the 4-token expected.
    result = run_scorecard(examples, _fixed_predict("the quick cat"), ["token_f1"])
    score = result.rows[0].scores["token_f1"]
    # precision = 2/3, recall = 2/4 → F1 = 2*(2/3*1/2)/(2/3+1/2) ≈ 0.5714
    assert 0.55 < score < 0.59


def test_contains_expected() -> None:
    examples = [{"example_id": "EX-1", "input": {}, "expected": {"expected": "Paris"}}]
    hit = run_scorecard(examples, _fixed_predict("The capital is Paris."), ["contains_expected"])
    miss = run_scorecard(examples, _fixed_predict("The capital is Lyon."), ["contains_expected"])
    assert hit.rows[0].scores["contains_expected"] == 1.0
    assert miss.rows[0].scores["contains_expected"] == 0.0


def test_per_row_error_isolation() -> None:
    def predict(inputs):
        if inputs.get("question") == "2+2":
            raise RuntimeError("model exploded")
        return "Paris"

    result = run_scorecard(_EXAMPLES, predict, ["exact_match"])
    by_id = {row.example_id: row for row in result.rows}
    assert by_id["EX-1"].error is None
    assert by_id["EX-1"].passed is True
    assert by_id["EX-2"].error == "model exploded"
    assert by_id["EX-2"].passed is False
    assert by_id["EX-2"].scores == {}
    # The errored row still counts as a failure; the good row still scored.
    assert result.passed_count == 1
    assert result.failed_count == 1


def test_default_scorers_used_when_none_requested() -> None:
    result = run_scorecard(_EXAMPLES, _fixed_predict("Paris"), None)
    assert result.scorers == list(DEFAULT_SCORERS)


def test_resolve_scorers_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="unknown scorer"):
        resolve_scorers(["exact_match", "bogus"])


def test_resolve_scorers_dedupes_and_preserves_order() -> None:
    assert resolve_scorers(["token_f1", "exact_match", "token_f1"]) == [
        "token_f1",
        "exact_match",
    ]


def test_expected_text_prefers_conventional_field() -> None:
    assert expected_text({"answer": "42", "note": "x"}) == "42"
    assert expected_text({"expected": "Paris"}) == "Paris"
    # Single string value when no conventional key matches.
    assert expected_text({"weird_key": "value"}) == "value"


def test_empty_examples_yield_zero_aggregate() -> None:
    result = run_scorecard([], _fixed_predict("x"), ["exact_match"])
    assert result.n_examples == 0
    assert result.overall == 0.0
    assert result.pass_rate == 0.0
    assert result.aggregate == {}
