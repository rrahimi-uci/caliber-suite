"""Weighted aggregation + partial-scorer semantics for the scorecard engine.

Two defects from the repository review (``ui-complete-report.md`` §4):

* ``CaliberEvalDatasetExample.weight``/``tags`` were stored but dropped by the
  generic evaluation loader, so a deliberately weighted dataset aggregated as
  an unweighted row mean and could not be sliced by tag.
* A row whose judge/scorer raised recorded the failure on ``row.error`` but
  still scored on the *surviving* scorers, so a broken judge could push a row
  over ``pass_threshold`` on incomplete evidence.

Both are pure functions of the engine, so these tests need no DB or provider.
"""

from __future__ import annotations

from typing import Any

import pytest

from caliber.eval.scorecard import ScorecardInputError, run_scorecard


def _fixed_predict(answer: str):
    def predict(_inputs: Any) -> str:
        return answer

    return predict


def _example(example_id: str, expected: str, **extra: Any) -> dict[str, Any]:
    return {
        "example_id": example_id,
        "input": {"q": example_id},
        "expected": {"expected": expected},
        **extra,
    }


def test_unweighted_examples_keep_the_plain_arithmetic_mean() -> None:
    """Backward compatibility: no ``weight`` key behaves exactly as before."""
    examples = [_example("EX-1", "Paris"), _example("EX-2", "Berlin")]

    result = run_scorecard(examples, _fixed_predict("Paris"), ["exact_match"])

    assert result.aggregate["exact_match"] == 0.5
    assert result.overall == 0.5
    assert result.pass_rate == 0.5
    assert [row.weight for row in result.rows] == [1.0, 1.0]


def test_weights_shift_the_aggregate_and_pass_rate() -> None:
    """A 3x-weighted failing row dominates a 1x-weighted passing row."""
    examples = [
        _example("EX-hit", "Paris", weight=1.0),
        _example("EX-miss", "Berlin", weight=3.0),
    ]

    result = run_scorecard(examples, _fixed_predict("Paris"), ["exact_match"])

    # (1.0*1 + 0.0*3) / 4
    assert result.aggregate["exact_match"] == 0.25
    assert result.overall == 0.25
    assert result.pass_rate == 0.25
    # Row-level scores are untouched — only the fold is weighted.
    by_id = {row.example_id: row for row in result.rows}
    assert by_id["EX-hit"].score == 1.0
    assert by_id["EX-miss"].score == 0.0
    assert by_id["EX-miss"].weight == 3.0


def test_zero_weight_row_is_scored_but_excluded_from_the_aggregate() -> None:
    examples = [
        _example("EX-hit", "Paris", weight=1.0),
        _example("EX-ignored", "Berlin", weight=0.0),
    ]

    result = run_scorecard(examples, _fixed_predict("Paris"), ["exact_match"])

    assert result.n_examples == 2
    assert result.aggregate["exact_match"] == 1.0
    assert result.overall == 1.0
    assert result.pass_rate == 1.0


def test_malformed_weight_falls_back_to_one() -> None:
    """A bad weight must not abort the run, skew the fold, or emit NaN."""
    examples = [
        _example("EX-1", "Paris", weight="heavy"),
        _example("EX-2", "Berlin", weight=-5),
        _example("EX-3", "Rome", weight=None),
        _example("EX-4", "Madrid", weight=float("inf")),
        _example("EX-5", "Lisbon", weight=float("nan")),
    ]

    result = run_scorecard(examples, _fixed_predict("Paris"), ["exact_match"])

    assert [row.weight for row in result.rows] == [1.0, 1.0, 1.0, 1.0, 1.0]
    assert result.aggregate["exact_match"] == 0.2
    assert result.overall == 0.2


def test_tags_are_carried_onto_each_row() -> None:
    examples = [_example("EX-1", "Paris", tags=["billing", "p0"])]

    result = run_scorecard(examples, _fixed_predict("Paris"), ["exact_match"])

    assert result.rows[0].tags == ["billing", "p0"]
    assert result.rows[0].to_dict()["tags"] == ["billing", "p0"]


def test_row_with_a_failed_scorer_cannot_pass() -> None:
    """Partial evidence must not clear the threshold.

    ``exact_match`` scores 1.0 here, so the surviving-subset mean would be a
    clean pass — but the judge blew up, so the row is incompletely evaluated
    and must be reported as failed.
    """

    def exploding_judge(_prediction: str, _inputs: Any, _expected: Any) -> float:
        raise RuntimeError("judge backend unavailable")

    examples = [_example("EX-1", "Paris")]

    result = run_scorecard(
        examples,
        _fixed_predict("Paris"),
        ["exact_match", "Judge.J-1"],
        judge_runners={"Judge.J-1": exploding_judge},
    )

    row = result.rows[0]
    assert row.scores["exact_match"] == 1.0
    assert row.error is not None
    assert "Judge.J-1" in row.error
    assert row.score == 0.0
    assert row.passed is False
    assert result.pass_rate == 0.0
    assert result.failed_count == 1
    # The surviving scorer remains available as raw diagnostic evidence, but
    # an incomplete row cannot enter a comparable headline aggregate.
    assert "exact_match" not in result.aggregate
    assert "Judge.J-1" not in result.aggregate


def test_all_zero_weights_are_rejected_before_prediction() -> None:
    """Explicit exclusion cannot silently become equal weighting."""
    examples = [
        _example("EX-1", "Paris", weight=0.0),
        _example("EX-2", "Berlin", weight=0.0),
    ]
    prediction_calls = 0

    def predict(_inputs: Any) -> str:
        nonlocal prediction_calls
        prediction_calls += 1
        return "Paris"

    with pytest.raises(ScorecardInputError, match="at least one value greater than zero"):
        run_scorecard(examples, predict, ["exact_match"])

    assert prediction_calls == 0


def test_single_zero_weight_row_still_uses_the_weighted_path() -> None:
    """A zero-weight row is valid when another row supplies aggregate weight."""
    examples = [
        _example("EX-hit", "Paris", weight=1.0),
        _example("EX-ignored", "Berlin", weight=0.0),
    ]

    result = run_scorecard(examples, _fixed_predict("Paris"), ["exact_match"])

    # The zero-weight failing row contributes nothing to the weighted fold.
    assert result.overall == 1.0
    assert result.pass_rate == 1.0
    # ...while the raw counts still report both rows.
    assert (result.passed_count, result.failed_count) == (1, 1)


def test_every_failing_scorer_is_reported_not_just_the_first() -> None:
    """Two broken judges must both surface, or debugging takes two round trips."""

    def boom(name: str):
        def runner(_prediction: str, _inputs: Any, _expected: Any) -> float:
            raise RuntimeError(f"{name} unavailable")

        return runner

    result = run_scorecard(
        [_example("EX-1", "Paris")],
        _fixed_predict("Paris"),
        ["exact_match", "Judge.A", "Judge.B"],
        judge_runners={"Judge.A": boom("A"), "Judge.B": boom("B")},
    )

    error = result.rows[0].error or ""
    assert "Judge.A: A unavailable" in error
    assert "Judge.B: B unavailable" in error
    assert result.rows[0].passed is False
    # The healthy scorer's evidence survives.
    assert result.rows[0].scores["exact_match"] == 1.0
    assert result.rows[0].score == 0.0


def test_incomplete_row_is_excluded_from_aggregate_but_penalizes_overall() -> None:
    """Only complete rows define scorer means; incomplete rows score zero overall."""

    def selective_judge(_prediction: str, inputs: Any, _expected: Any) -> float:
        if inputs["q"] == "EX-incomplete":
            raise RuntimeError("judge unavailable")
        return 0.5

    result = run_scorecard(
        [
            _example("EX-complete", "Paris"),
            _example("EX-incomplete", "Paris"),
        ],
        _fixed_predict("Paris"),
        ["exact_match", "Judge.J-1"],
        judge_runners={"Judge.J-1": selective_judge},
    )

    assert result.aggregate == {"exact_match": 1.0, "Judge.J-1": 0.5}
    assert result.rows[1].scores == {"exact_match": 1.0}
    assert result.rows[1].score == 0.0
    # Complete row score is (1.0 + 0.5) / 2; incomplete row is fail-closed zero.
    assert result.overall == 0.375


def test_row_with_all_scorers_healthy_still_passes() -> None:
    """The stricter pass rule must not make healthy rows fail."""

    def happy_judge(_prediction: str, _inputs: Any, _expected: Any) -> float:
        return 1.0

    result = run_scorecard(
        [_example("EX-1", "Paris")],
        _fixed_predict("Paris"),
        ["exact_match", "Judge.J-1"],
        judge_runners={"Judge.J-1": happy_judge},
    )

    assert result.rows[0].error is None
    assert result.rows[0].passed is True
    assert result.pass_rate == 1.0
