"""Tests for the in-memory eval provider used by the rest of the suite."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from caliber.eval.fake import FakeEvalProvider
from caliber.eval.provider import (
    EvalComparison,
    EvalProviderError,
    EvalRequest,
    ScoreSet,
    apply_scorer_weights,
    build_provider,
    describe_provider,
)


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


def test_custom_evidence_rows_are_carried_into_the_comparison() -> None:
    rows = ({"example_id": "fixture-1", "scores": {"quality": 1.0}},)
    comparison = FakeEvalProvider(evidence_rows=rows, n_examples=1).evaluate(_request())
    assert comparison.evidence_rows == rows


def test_weighting_ignores_invalid_entries_and_recomputes_valid_weights() -> None:
    scores = ScoreSet(overall=0.2, dimensions={"quality": 0.8, "safety": 0.4})
    weighted = apply_scorer_weights(
        scores,
        {
            "quality": 3,
            "safety": 1.0,
            "missing": 2,
            "bool": True,
            "text": "2",
            "negative": -1,
        },
    )
    assert weighted.overall == 0.7
    assert weighted.dimensions == scores.dimensions
    assert apply_scorer_weights(scores, {"missing": 1}) is scores


def test_provider_factory_and_description_are_explicit() -> None:
    assert isinstance(build_provider("FAKE"), FakeEvalProvider)
    assert describe_provider(FakeEvalProvider()) == {"provider": "fake", "simulated": True}
    assert describe_provider(SimpleNamespace(provider_name="custom", is_simulated=False)) == {
        "provider": "custom",
        "simulated": False,
    }
    assert build_provider("mlflow").__class__.__name__ == "MLflowEvalProvider"
    with pytest.raises(EvalProviderError, match="unknown eval_provider"):
        build_provider("missing")


def test_fake_provider_supports_requested_scorers_and_dataset_loader_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested = FakeEvalProvider(n_examples=None)
    comparison = requested.evaluate(_request(scorer_names=["quality"]))
    assert comparison.candidate.dimensions == {"quality": 0.94}

    def loader_factory(_session_factory: object):
        def load(_dataset_id: str, _version: int | None = None) -> list[dict[str, object]]:
            return [{"example_id": "one"}]

        return load

    monkeypatch.setattr("caliber.eval.predict.build_db_load_dataset", loader_factory)
    provider = FakeEvalProvider(session_factory=object())
    assert provider.evaluate(_request()).n_examples == 1

    def empty_loader_factory(_session_factory: object):
        def load(_dataset_id: str, _version: int | None = None) -> list[dict[str, object]]:
            raise ValueError("empty")

        return load

    monkeypatch.setattr("caliber.eval.predict.build_db_load_dataset", empty_loader_factory)
    assert FakeEvalProvider(session_factory=object()).evaluate(_request()).n_examples == 0


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
