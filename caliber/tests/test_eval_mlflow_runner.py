"""Unit tests for :mod:`caliber.eval.mlflow_runner`.

Integration tests (against a real ``mlflow.genai.evaluate`` run) live in
``test_integration_mlflow.py`` and skip when ``CALIBER_INTEGRATION_TESTS``
is unset. These tests cover the *non-MLflow* logic — the registration
surface, the metrics-to-ScoreSet folding, and the explicit error paths
that fire before the backend is touched.
"""

from __future__ import annotations

import builtins
import sys
import types
from typing import Any, ClassVar

import pytest

from caliber.eval.mlflow_runner import (
    MLflowEvalProvider,
    _coerce_float,
    _compute_deltas,
    _count_examples,
    _metrics_to_score_set,
)
from caliber.eval.provider import EvalProviderError, EvalRequest, ScoreSet


def test_coerce_float_rejects_non_finite() -> None:
    """Regression: NaN/inf must be dropped (treated as non-numeric) so a scorer
    that errored on every row can't slip a non-finite mean into the gate."""
    assert _coerce_float(float("nan")) is None
    assert _coerce_float(float("inf")) is None
    assert _coerce_float(float("-inf")) is None
    assert _coerce_float(0.5) == 0.5
    # A NaN metric folds to the empty→0.0 fail-closed branch.
    assert _metrics_to_score_set({"Correctness/mean": float("nan")}).overall == 0.0


def test_resolve_dataset_internal_typeerror_is_loud_not_silent_fallback() -> None:
    """Regression (#13): an internal TypeError in a version-aware loader must
    surface as EvalProviderError, not silently re-resolve the active set."""
    calls: list[int | None] = []

    def loader(eval_dataset_id: str, version: int | None = None) -> list[dict[str, Any]]:
        calls.append(version)
        if version is not None:
            raise TypeError("internal failure inside the version-aware loader")
        return [{"inputs": {"set": "active"}}]

    provider = MLflowEvalProvider(load_dataset=loader)
    with pytest.raises(EvalProviderError):
        provider._resolve_dataset("ds-1", 5)
    # It must NOT have fallen back to a version-less call (which would have
    # returned the active set for a pinned request).
    assert calls == [5]


def _request(**overrides: object) -> EvalRequest:
    defaults: dict[str, object] = {
        "agent_id": "support-agent",
        "job_id": "RFN-1",
        "artifact_type": "prompt",
        "candidate_content": "new prompt",
        "baseline_content": "old prompt",
        "eval_dataset_id": "default",
    }
    defaults.update(overrides)
    return EvalRequest(**defaults)  # type: ignore[arg-type]


def _install_fake_mlflow(monkeypatch: pytest.MonkeyPatch, evaluate_fn: Any | None = None) -> None:
    """Install a minimal ``mlflow`` module shape so the lazy import inside
    :meth:`MLflowEvalProvider.evaluate` resolves to our test double."""
    mlflow_stub = types.ModuleType("mlflow")
    mlflow_genai_stub = types.ModuleType("mlflow.genai")
    if evaluate_fn is not None:
        mlflow_genai_stub.evaluate = evaluate_fn  # type: ignore[attr-defined]
    mlflow_stub.genai = mlflow_genai_stub  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlflow", mlflow_stub)
    monkeypatch.setitem(sys.modules, "mlflow.genai", mlflow_genai_stub)


def _install_fake_mlflow_with_judge(
    monkeypatch: pytest.MonkeyPatch, make_judge_fn: Any
) -> None:
    """Install an ``mlflow.genai`` shape carrying both ``scorers`` (empty) and a
    fake ``make_judge`` so the custom-judge resolution path can be exercised."""
    mlflow_stub = types.ModuleType("mlflow")
    genai = types.ModuleType("mlflow.genai")
    scorers_mod = types.ModuleType("mlflow.genai.scorers")
    genai.scorers = scorers_mod  # type: ignore[attr-defined]
    genai.make_judge = make_judge_fn  # type: ignore[attr-defined]
    mlflow_stub.genai = genai  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlflow", mlflow_stub)
    monkeypatch.setitem(sys.modules, "mlflow.genai", genai)
    monkeypatch.setitem(sys.modules, "mlflow.genai.scorers", scorers_mod)


def test_resolve_scorers_builds_custom_judge_via_make_judge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_make_judge(**kwargs: Any) -> tuple[str, str]:
        captured.update(kwargs)
        return ("judge-scorer", kwargs["name"])

    _install_fake_mlflow_with_judge(monkeypatch, fake_make_judge)
    provider = MLflowEvalProvider()
    resolved = provider._resolve_scorers(
        ["Judge.tone"],
        {
            "Judge.tone": {
                "instructions": "Rate whether {{ outputs }} is polite.",
                "model": "openai:/gpt-4o-mini",
                "feedback_value_type": "bool",
            }
        },
    )
    assert resolved == [("judge-scorer", "tone")]
    # The ``Judge.`` prefix is stripped for the MLflow judge name.
    assert captured["name"] == "tone"
    assert captured["model"] == "openai:/gpt-4o-mini"
    # The value-type string is mapped to the Python type make_judge expects.
    assert captured["feedback_value_type"] is bool


def test_resolve_scorers_custom_judge_requires_instructions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_mlflow_with_judge(monkeypatch, lambda **_kw: object())
    provider = MLflowEvalProvider()
    with pytest.raises(EvalProviderError, match="instructions"):
        provider._resolve_scorers(["Judge.tone"], {"Judge.tone": {}})


def test_evaluate_without_registered_factory_raises_actionable_error() -> None:
    provider = MLflowEvalProvider()
    with pytest.raises(EvalProviderError, match=r"no predict_fn for agent_id"):
        provider.evaluate(_request())


def test_register_predict_fn_is_idempotent_per_agent() -> None:
    provider = MLflowEvalProvider()
    provider.register_predict_fn("a", lambda _prompt: lambda **_kw: "x")
    provider.register_predict_fn("a", lambda _prompt: lambda **_kw: "y")
    assert "a" in provider._factories
    # Replacing the factory is intentional: hot-reload in development.
    assert provider._factories["a"]("p")() == "y"


def test_metrics_to_score_set_handles_slash_mean_keys() -> None:
    scores = _metrics_to_score_set(
        {
            "Correctness/mean": 0.91,
            "Correctness/p90": 0.99,
            "Safety/mean": 0.95,
        }
    )
    assert scores.dimensions == {"Correctness": 0.91, "Safety": 0.95}
    # Overall is mean of dimensions: (0.91 + 0.95) / 2 = 0.93
    assert scores.overall == 0.93


def test_metrics_to_score_set_honors_explicit_overall_when_present() -> None:
    scores = _metrics_to_score_set(
        {
            "overall": 0.87,
            "Correctness/mean": 0.91,
            "Safety/mean": 0.95,
        }
    )
    assert scores.overall == 0.87  # explicit wins
    assert scores.dimensions == {"Correctness": 0.91, "Safety": 0.95}


def test_metrics_to_score_set_rejects_boolean_values() -> None:
    """Some MLflow scorers emit per-example pass/fail; aggregating those
    as floats would put 0.0/1.0 into a dimension by accident."""
    scores = _metrics_to_score_set({"passed": True, "Correctness/mean": 0.9})
    assert "passed" not in scores.dimensions
    assert scores.dimensions == {"Correctness": 0.9}


def test_metrics_to_score_set_empty_returns_zero_overall() -> None:
    scores = _metrics_to_score_set({})
    assert scores.overall == 0.0
    assert scores.dimensions == {}


def test_metrics_to_score_set_accepts_plain_dimension_keys() -> None:
    scores = _metrics_to_score_set({"fluency": 0.5, "debug/raw": 1.0, "bad": "x"})
    assert scores.dimensions == {"fluency": 0.5}
    assert scores.overall == 0.5


def test_compute_deltas_returns_empty_for_cold_start() -> None:
    candidate = ScoreSet(overall=0.9, dimensions={"x": 0.9})
    assert _compute_deltas(candidate, None) == {}


def test_compute_deltas_includes_overall_and_per_dimension() -> None:
    candidate = ScoreSet(overall=0.94, dimensions={"factual": 0.97, "tone": 0.89})
    baseline = ScoreSet(overall=0.88, dimensions={"factual": 0.88, "tone": 0.89})
    deltas = _compute_deltas(candidate, baseline)
    assert deltas["overall"] == 0.06
    assert deltas["factual"] == 0.09
    assert deltas["tone"] == 0.0


def test_count_examples_falls_back_to_iteration() -> None:
    class NoLen:
        def __iter__(self) -> Any:
            return iter([{"q": "a"}, {"q": "b"}, {"q": "c"}])

    assert _count_examples(NoLen()) == 3
    assert _count_examples([1, 2, 3, 4]) == 4
    assert _count_examples(object()) == 0


def test_evaluate_with_injected_dataset_and_scorers_runs_a_full_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end shape test with ``mlflow.genai.evaluate`` mocked out.

    Verifies the provider:

    * Calls the candidate factory with the candidate prompt content.
    * Calls the baseline factory with the baseline prompt content.
    * Returns an :class:`EvalComparison` with deltas, dataset id, and
      example count threaded through.
    """
    seen_prompts: list[str] = []

    def factory(prompt: str) -> object:
        seen_prompts.append(prompt)
        return lambda **_kw: f"response-for:{prompt}"

    call_log: list[dict[str, Any]] = []

    class _MockEvalResult:
        def __init__(self, metrics: dict[str, float]) -> None:
            self.metrics = metrics

    def fake_evaluate(*, data: object, predict_fn: Any, scorers: list[Any]) -> _MockEvalResult:
        _ = predict_fn
        # First call (candidate) gets higher scores than second (baseline).
        is_first = len(call_log) == 0
        call_log.append(
            {
                "data_len": len(data),  # type: ignore[arg-type]
                "scorer_count": len(scorers),
            }
        )
        if is_first:
            return _MockEvalResult({"Correctness/mean": 0.95, "Safety/mean": 0.99})
        return _MockEvalResult({"Correctness/mean": 0.88, "Safety/mean": 0.99})

    _install_fake_mlflow(monkeypatch, evaluate_fn=fake_evaluate)

    dataset = [{"input": "q1"}, {"input": "q2"}, {"input": "q3"}]
    provider = MLflowEvalProvider(
        load_dataset=lambda _id: dataset,
        scorers=[object(), object()],  # opaque to the provider; just counted
    )
    provider.register_predict_fn("support-agent", factory)

    comparison = provider.evaluate(_request())

    assert seen_prompts == ["new prompt", "old prompt"]
    assert len(call_log) == 2
    assert call_log[0] == {"data_len": 3, "scorer_count": 2}
    assert comparison.candidate.overall == 0.97  # mean(0.95, 0.99)
    assert comparison.baseline is not None
    assert comparison.baseline.overall == 0.935  # mean(0.88, 0.99)
    assert comparison.deltas["overall"] == 0.035
    assert comparison.deltas["Correctness"] == 0.07
    assert comparison.n_examples == 3
    assert comparison.eval_dataset_id == "default"


def test_evaluate_cold_start_skips_baseline_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """When baseline_content is None, the provider must not call the
    factory or mlflow a second time."""
    factory_calls: list[str] = []

    def factory(prompt: str) -> object:
        factory_calls.append(prompt)
        return lambda **_kw: "response"

    eval_calls = 0

    class _MockEvalResult:
        metrics: ClassVar[dict[str, float]] = {"Correctness/mean": 0.9}

    def fake_evaluate(**_kw: Any) -> _MockEvalResult:
        nonlocal eval_calls
        eval_calls += 1
        return _MockEvalResult()

    _install_fake_mlflow(monkeypatch, evaluate_fn=fake_evaluate)

    provider = MLflowEvalProvider(
        load_dataset=lambda _id: [{"q": "a"}],
        scorers=[object()],
    )
    provider.register_predict_fn("support-agent", factory)

    comparison = provider.evaluate(_request(baseline_content=None))

    assert factory_calls == ["new prompt"]
    assert eval_calls == 1
    assert comparison.baseline is None
    assert comparison.deltas == {}


def test_evaluate_wraps_backend_failures_in_eval_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def factory(_prompt: str) -> object:
        return lambda **_kw: "response"

    def fake_evaluate(**_kw: Any) -> None:
        raise RuntimeError("scorer crashed")

    _install_fake_mlflow(monkeypatch, evaluate_fn=fake_evaluate)

    provider = MLflowEvalProvider(
        load_dataset=lambda _id: [{"q": "a"}],
        scorers=[object()],
    )
    provider.register_predict_fn("support-agent", factory)

    with pytest.raises(EvalProviderError, match=r"mlflow.genai.evaluate failed"):
        provider.evaluate(_request(baseline_content=None))


def test_evaluate_wraps_load_dataset_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def loader(_id: str) -> Any:
        raise FileNotFoundError("dataset missing")

    _install_fake_mlflow(monkeypatch)

    provider = MLflowEvalProvider(load_dataset=loader, scorers=[object()])
    provider.register_predict_fn("support-agent", lambda _p: lambda **_kw: "r")

    with pytest.raises(EvalProviderError, match=r"failed to load eval dataset"):
        provider.evaluate(_request())


def test_evaluate_missing_mlflow_raises_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def _missing_mlflow(name: str, *args: object, **kwargs: object) -> object:
        if name == "mlflow":
            raise ImportError("missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _missing_mlflow)
    provider = MLflowEvalProvider(
        load_dataset=lambda _id: [{"q": "a"}],
        scorers=[object()],
    )
    provider.register_predict_fn("support-agent", lambda _p: lambda **_kw: "r")

    with pytest.raises(EvalProviderError, match="mlflow is not installed"):
        provider.evaluate(_request())


def test_default_dataset_registry_loader_success_and_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mlflow_stub = types.ModuleType("mlflow")
    genai_stub = types.ModuleType("mlflow.genai")
    genai_stub.datasets = types.SimpleNamespace(  # type: ignore[attr-defined]
        get_dataset=lambda dataset_id: [{"id": dataset_id}]
    )
    mlflow_stub.genai = genai_stub  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlflow", mlflow_stub)
    monkeypatch.setitem(sys.modules, "mlflow.genai", genai_stub)

    provider = MLflowEvalProvider(scorers=[object()])
    assert provider._resolve_dataset("golden") == [{"id": "golden"}]

    genai_stub.datasets = types.SimpleNamespace(  # type: ignore[attr-defined]
        get_dataset=lambda _dataset_id: (_ for _ in ()).throw(RuntimeError("down"))
    )
    with pytest.raises(EvalProviderError, match="failed to load eval dataset"):
        provider._resolve_dataset("golden")


def test_default_scorer_resolution_skips_missing_or_configured_scorers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    genai_stub = types.ModuleType("mlflow.genai")
    scorers_stub = types.ModuleType("mlflow.genai.scorers")

    class Correctness:
        pass

    class Guidelines:
        def __init__(self) -> None:
            raise TypeError("requires guidelines")

    scorers_stub.Correctness = Correctness  # type: ignore[attr-defined]
    scorers_stub.Guidelines = Guidelines  # type: ignore[attr-defined]
    genai_stub.scorers = scorers_stub  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlflow.genai", genai_stub)
    monkeypatch.setitem(sys.modules, "mlflow.genai.scorers", scorers_stub)

    resolved = MLflowEvalProvider()._resolve_scorers()

    assert len(resolved) == 1
    assert isinstance(resolved[0], Correctness)


def test_default_scorer_resolution_errors_when_none_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    genai_stub = types.ModuleType("mlflow.genai")
    scorers_stub = types.ModuleType("mlflow.genai.scorers")
    genai_stub.scorers = scorers_stub  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlflow.genai", genai_stub)
    monkeypatch.setitem(sys.modules, "mlflow.genai.scorers", scorers_stub)

    with pytest.raises(EvalProviderError, match="no scorers resolved"):
        MLflowEvalProvider()._resolve_scorers()


def test_explicit_scorer_selection_supports_per_scorer_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    genai_stub = types.ModuleType("mlflow.genai")
    scorers_stub = types.ModuleType("mlflow.genai.scorers")

    class Guidelines:
        def __init__(self, *, guidelines: list[str]) -> None:
            self.guidelines = list(guidelines)

    class Safety:
        pass

    scorers_stub.Guidelines = Guidelines  # type: ignore[attr-defined]
    scorers_stub.Safety = Safety  # type: ignore[attr-defined]
    genai_stub.scorers = scorers_stub  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlflow.genai", genai_stub)
    monkeypatch.setitem(sys.modules, "mlflow.genai.scorers", scorers_stub)

    resolved = MLflowEvalProvider()._resolve_scorers(
        ["Guidelines", "Safety"],
        {"Guidelines": {"guidelines": ["be factual"]}},
    )

    assert len(resolved) == 2
    assert isinstance(resolved[0], Guidelines)
    assert resolved[0].guidelines == ["be factual"]
    assert isinstance(resolved[1], Safety)


def test_explicit_scorer_selection_supports_deepeval_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    genai_stub = types.ModuleType("mlflow.genai")
    scorers_stub = types.ModuleType("mlflow.genai.scorers")
    deepeval_stub = types.ModuleType("mlflow.genai.scorers.deepeval")

    class Faithfulness:
        pass

    deepeval_stub.Faithfulness = Faithfulness  # type: ignore[attr-defined]
    scorers_stub.deepeval = deepeval_stub  # type: ignore[attr-defined]
    genai_stub.scorers = scorers_stub  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlflow.genai", genai_stub)
    monkeypatch.setitem(sys.modules, "mlflow.genai.scorers", scorers_stub)
    monkeypatch.setitem(sys.modules, "mlflow.genai.scorers.deepeval", deepeval_stub)

    resolved = MLflowEvalProvider()._resolve_scorers(["DeepEval.Faithfulness"], {})

    assert len(resolved) == 1
    assert isinstance(resolved[0], Faithfulness)


def test_explicit_deepeval_scorer_requires_optional_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    genai_stub = types.ModuleType("mlflow.genai")
    scorers_stub = types.ModuleType("mlflow.genai.scorers")
    genai_stub.scorers = scorers_stub  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlflow.genai", genai_stub)
    monkeypatch.setitem(sys.modules, "mlflow.genai.scorers", scorers_stub)

    with pytest.raises(EvalProviderError, match=r"pip install -U deepeval"):
        MLflowEvalProvider()._resolve_scorers(["DeepEval.Faithfulness"], {})
