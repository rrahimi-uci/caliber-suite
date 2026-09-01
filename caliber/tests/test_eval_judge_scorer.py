"""Unit tests for the shared judge path (``caliber.eval.judge_scorer``).

No mlflow needed except for :func:`build_judge`, which we exercise with a fake
``mlflow.genai.make_judge`` installed into ``sys.modules`` (mirrors
``test_eval_mlflow_runner._install_fake_mlflow_with_judge``).
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from caliber.eval.judge_scorer import (
    JudgeError,
    JudgeOutcome,
    build_judge,
    coerce_feedback_value,
    score_with_judge,
)


class _Feedback:
    """Minimal stand-in for ``mlflow.entities.Feedback``."""

    def __init__(self, value: Any, rationale: str | None = None) -> None:
        self.value = value
        self.rationale = rationale


def test_coerce_bool() -> None:
    assert coerce_feedback_value(True) == 1.0
    assert coerce_feedback_value(False) == 0.0


def test_coerce_numbers_clamp_to_unit() -> None:
    assert coerce_feedback_value(0.8) == 0.8
    assert coerce_feedback_value(5) == 1.0  # out-of-range clamps, not Likert-normalized
    assert coerce_feedback_value(-2) == 0.0


def test_coerce_string_verdicts_and_numbers() -> None:
    assert coerce_feedback_value("pass") == 1.0
    assert coerce_feedback_value("FAIL") == 0.0
    assert coerce_feedback_value("0.75") == 0.75
    assert coerce_feedback_value("85%") == 0.85


def test_coerce_unscoreable_raises() -> None:
    with pytest.raises(JudgeError):
        coerce_feedback_value("somewhat polite, mostly")
    with pytest.raises(JudgeError):
        coerce_feedback_value({"not": "scalar"})


def test_score_with_judge_passes_fields_and_coerces() -> None:
    captured: dict[str, Any] = {}

    def judge(**kwargs: Any) -> _Feedback:
        captured.update(kwargs)
        return _Feedback(value=True, rationale="polite and on-topic")

    outcome = score_with_judge(
        judge,
        inputs={"q": "hi"},
        outputs="hello there",
        expectations={"a": "greeting"},
    )
    assert isinstance(outcome, JudgeOutcome)
    assert outcome.score == 1.0
    assert outcome.value is True
    assert outcome.rationale == "polite and on-topic"
    assert captured == {
        "inputs": {"q": "hi"},
        "outputs": "hello there",
        "expectations": {"a": "greeting"},
    }


def test_score_with_judge_omits_expectations_when_absent() -> None:
    captured: dict[str, Any] = {}

    def judge(**kwargs: Any) -> _Feedback:
        captured.update(kwargs)
        return _Feedback(value=0.5)

    score_with_judge(judge, inputs={"q": "hi"}, outputs="out")
    assert "expectations" not in captured


def test_score_with_judge_wraps_judge_exception() -> None:
    def judge(**_kwargs: Any) -> _Feedback:
        raise RuntimeError("model exploded")

    with pytest.raises(JudgeError, match="judge invocation failed"):
        score_with_judge(judge, inputs={}, outputs="x")


def _install_fake_make_judge(monkeypatch: pytest.MonkeyPatch, fn: Any) -> None:
    mlflow_stub = types.ModuleType("mlflow")
    genai = types.ModuleType("mlflow.genai")
    genai.make_judge = fn  # type: ignore[attr-defined]
    mlflow_stub.genai = genai  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlflow", mlflow_stub)
    monkeypatch.setitem(sys.modules, "mlflow.genai", genai)


def test_build_judge_maps_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_make_judge(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "built"

    _install_fake_make_judge(monkeypatch, fake_make_judge)
    result = build_judge(
        "tone",
        "Rate whether {{ outputs }} is polite.",
        model="openai:/gpt-4o-mini",
        feedback_value_type="bool",
    )
    assert result == "built"
    assert captured["name"] == "tone"
    assert captured["model"] == "openai:/gpt-4o-mini"
    assert captured["feedback_value_type"] is bool


def test_build_judge_defaults_to_luna_with_high_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_make_judge(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "built"

    _install_fake_make_judge(monkeypatch, fake_make_judge)
    assert build_judge("tone", "Rate {{ outputs }}.") == "built"
    assert captured["model"] == "openai:/gpt-5.6-luna"
    assert captured["inference_params"] == {"reasoning_effort": "high"}


def test_build_judge_requires_instructions(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_make_judge(monkeypatch, lambda **_kw: object())
    with pytest.raises(JudgeError, match="instructions"):
        build_judge("tone", "   ")
