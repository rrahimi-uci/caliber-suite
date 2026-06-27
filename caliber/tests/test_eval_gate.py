"""Tests for the regression gate.

Pure-function tests — no DB, no provider, no MLflow. The gate's behavior
is the load-bearing part of the eval-driven promotion mechanism, so we
pin every branch.
"""

from __future__ import annotations

from caliber.eval.gate import (
    DEFAULT_MAX_REGRESSION_DELTA,
    DEFAULT_MIN_AGGREGATE_SCORE,
    apply_gate,
)
from caliber.eval.provider import EvalComparison, ScoreSet


def _comparison(
    *,
    candidate_overall: float = 0.94,
    baseline_overall: float | None = 0.88,
    dim_deltas: dict[str, float] | None = None,
) -> EvalComparison:
    cand_dims = {"factual": 0.97, "tone": 0.89}
    base = (
        ScoreSet(overall=baseline_overall, dimensions={"factual": 0.88, "tone": 0.89})
        if baseline_overall is not None
        else None
    )
    deltas: dict[str, float] = {}
    if base is not None:
        deltas["overall"] = round(candidate_overall - baseline_overall, 4)
        for dim, value in cand_dims.items():
            deltas[dim] = round(value - base.dimensions[dim], 4)
    if dim_deltas:
        deltas.update(dim_deltas)
    return EvalComparison(
        candidate=ScoreSet(overall=candidate_overall, dimensions=cand_dims),
        baseline=base,
        deltas=deltas,
        eval_dataset_id="default",
        n_examples=120,
    )


def test_pass_when_overall_meets_floor_and_no_regression() -> None:
    decision = apply_gate(_comparison())
    assert decision.passed
    assert decision.reasons == []


def test_reject_when_overall_below_floor() -> None:
    decision = apply_gate(_comparison(candidate_overall=0.80))
    assert not decision.passed
    assert any("below min_aggregate_score" in r for r in decision.reasons)


def test_reject_when_dimension_regresses_beyond_tolerance() -> None:
    """Any dim regressing more than ``max_regression_delta`` fails the gate."""
    decision = apply_gate(_comparison(dim_deltas={"tone": -0.05}))
    assert not decision.passed
    assert any("tone" in r and "regressed" in r for r in decision.reasons)


def test_non_finite_overall_fails_closed() -> None:
    """Regression (#3): a NaN/inf aggregate must REJECT (fail-closed), not pass.
    ``nan < min`` is False, so without an explicit guard the candidate would
    otherwise sail through the gate."""
    for bad in (float("nan"), float("inf"), float("-inf")):
        cmp = EvalComparison(
            candidate=ScoreSet(overall=bad, dimensions={}),
            baseline=None,
            deltas={},
            eval_dataset_id="default",
            n_examples=10,
        )
        decision = apply_gate(cmp, {"min_aggregate_score": 0.85})
        assert decision.passed is False
        assert decision.reasons


def test_pass_when_regression_within_tolerance() -> None:
    """A -0.01 dim delta is within the default 0.02 tolerance band."""
    decision = apply_gate(_comparison(dim_deltas={"tone": -0.01}))
    assert decision.passed


def test_cold_start_skips_regression_check() -> None:
    """No baseline → only the overall floor applies."""
    decision = apply_gate(_comparison(baseline_overall=None))
    assert decision.passed


def test_cold_start_still_enforces_overall_floor() -> None:
    decision = apply_gate(_comparison(candidate_overall=0.5, baseline_overall=None))
    assert not decision.passed


def test_custom_thresholds_override_defaults() -> None:
    """Explicit thresholds in agent_config.eval_thresholds beat the defaults."""
    cmp = _comparison(candidate_overall=0.90, dim_deltas={"tone": -0.06})
    # With looser thresholds, this should pass.
    decision = apply_gate(
        cmp,
        thresholds={"min_aggregate_score": 0.5, "max_regression_delta": 0.10},
    )
    assert decision.passed


def test_decision_to_json_round_trip() -> None:
    decision = apply_gate(_comparison())
    dumped = decision.to_json()
    assert dumped["passed"] is True
    assert dumped["reasons"] == []
    assert dumped["thresholds"]["min_aggregate_score"] == DEFAULT_MIN_AGGREGATE_SCORE
    assert dumped["thresholds"]["max_regression_delta"] == DEFAULT_MAX_REGRESSION_DELTA


def test_overall_delta_is_not_treated_as_per_dimension_regression() -> None:
    """``deltas["overall"]`` is the overall improvement, not a per-dim score
    to penalize. The gate must skip it when checking per-dim regressions."""
    decision = apply_gate(_comparison(dim_deltas={"overall": -0.10}))
    # Overall floor is still met (0.94) and no per-dim regression beyond
    # tolerance. The overall delta should not register as a regression.
    assert decision.passed
