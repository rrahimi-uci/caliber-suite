"""Tests for the in-memory eval provider used by the rest of the suite."""

from __future__ import annotations

from caliber.eval.fake import FakeEvalProvider
from caliber.eval.provider import EvalComparison, EvalRequest, ScoreSet


def _request(**overrides: object) -> EvalRequest:
    defaults: dict[str, object] = {
        "agent_id": "support-agent",
        "job_id": "RFN-1",
        "artifact_type": "prompt",
        "candidate_content": "you are a helpful agent",
        "baseline_content": "older prompt",
        "eval_dataset_id": "default",
    }
    defaults.update(overrides)
    return EvalRequest(**defaults)  # type: ignore[arg-type]


def test_default_returns_candidate_better_than_baseline() -> None:
    provider = FakeEvalProvider()
    comparison = provider.evaluate(_request())

    assert comparison.candidate.overall == 0.94
    assert comparison.baseline is not None
    assert comparison.baseline.overall == 0.88
    assert comparison.deltas["overall"] == 0.06  # 0.94 - 0.88


def test_default_includes_per_dimension_deltas() -> None:
    comparison = FakeEvalProvider().evaluate(_request())
    assert comparison.deltas["factual"] == 0.09
    assert comparison.deltas["tool_use"] == 0.09
    assert comparison.deltas["tone"] == 0.0
    assert comparison.deltas["safety"] == 0.0


def test_cold_start_skips_baseline_when_baseline_content_missing() -> None:
    """A request with ``baseline_content=None`` reflects a deployment with
    no prior prompt — the fake produces a comparison with baseline=None."""
    provider = FakeEvalProvider()
    comparison = provider.evaluate(_request(baseline_content=None))
    assert comparison.baseline is None
    assert comparison.deltas == {}


def test_overrides_candidate_and_baseline_scores() -> None:
    candidate = ScoreSet(overall=0.7, dimensions={"factual": 0.6})
    baseline = ScoreSet(overall=0.8, dimensions={"factual": 0.85})
    provider = FakeEvalProvider(candidate_scores=candidate, baseline_scores=baseline)
    comparison = provider.evaluate(_request())
    assert comparison.candidate.overall == 0.7
    assert comparison.baseline is not None
    assert comparison.baseline.overall == 0.8
    assert comparison.deltas["overall"] == -0.1


def test_records_calls() -> None:
    provider = FakeEvalProvider()
    provider.evaluate(_request(job_id="RFN-1"))
    provider.evaluate(_request(job_id="RFN-2"))
    assert [c.job_id for c in provider.calls] == ["RFN-1", "RFN-2"]


def test_callable_override_sees_request() -> None:
    """Tests that need request-dependent behavior inject a callable."""

    def callable_fn(request: EvalRequest) -> EvalComparison:
        return EvalComparison(
            candidate=ScoreSet(overall=request.candidate_content.count("a"), dimensions={}),
            baseline=None,
            deltas={},
            eval_dataset_id=request.eval_dataset_id,
            n_examples=42,
        )

    provider = FakeEvalProvider(eval_callable=callable_fn)
    comparison = provider.evaluate(_request(candidate_content="aaa"))
    assert comparison.candidate.overall == 3
    assert comparison.n_examples == 42
