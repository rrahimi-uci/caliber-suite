"""Tests for the LLM-judge workflow run scorer (golden-path roadmap, Wave 5.2)."""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

from caliber.workflows import judge as judge_mod
from caliber.workflows.judge import (
    LLMJudgeScorer,
    _openai_judge_fn,
    _parse_score,
    build_llm_judge_scorer,
)
from caliber.workflows.runtime import WorkflowRunResult


def _install_fake_openai(monkeypatch: pytest.MonkeyPatch, capture: dict) -> None:
    """Stub ``openai.OpenAI`` so the judge's chat call is captured, not networked."""

    class _Completions:
        def create(self, **kwargs):  # type: ignore[no-untyped-def]
            capture.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="0.8"))]
            )

    class _Client:
        def __init__(self, **_kw):  # type: ignore[no-untyped-def]
            self.chat = SimpleNamespace(completions=_Completions())

    module = types.ModuleType("openai")
    module.OpenAI = _Client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", module)


def test_openai_judge_omits_temperature_for_reasoning_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}
    _install_fake_openai(monkeypatch, captured)
    score = _openai_judge_fn("sk-test", "gpt-5.2")("question", "answer")
    assert score == 0.8
    # Reasoning models reject a custom temperature (and need reasoning budget),
    # so the judge must send neither — this is the bug being guarded.
    assert "temperature" not in captured
    assert "max_tokens" not in captured


def test_openai_judge_sets_temperature_for_plain_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}
    _install_fake_openai(monkeypatch, captured)
    _openai_judge_fn("sk-test", "gpt-4o-mini")("question", "answer")
    assert captured["temperature"] == 0.0
    assert captured["max_tokens"] == 8


def _result(
    status: str = "completed", output: str = "a helpful answer", guardrails=None
) -> WorkflowRunResult:
    return WorkflowRunResult(status=status, output=output, guardrail_results=guardrails or [])


# --------------------------------------------------------------------------- #
# _parse_score — strict: misparses RAISE (→ structural fallback), never inflate
# --------------------------------------------------------------------------- #


def test_parse_score_clean_values() -> None:
    assert _parse_score("0.8") == 0.8
    assert _parse_score("Score: 0.73") == 0.73  # single number after a label is fine
    assert _parse_score("1") == 1.0
    assert _parse_score("1e-3") == 0.001  # exponent parsed, not dropped to 1.0


def test_parse_score_clamps_rounding_noise() -> None:
    assert _parse_score("1.0000001") == 1.0


@pytest.mark.parametrize(
    "reply",
    [
        "not a number",
        "8/10",  # multi-number
        "version 3.5 quality 0.9",  # multi-number
        "The 2nd response scored 0.4",  # multi-number
        "Approximately 95%",  # out of [0,1]
        "Rating: 8",  # out of [0,1]
    ],
)
def test_parse_score_rejects_ambiguous_or_out_of_range(reply: str) -> None:
    with pytest.raises(ValueError):
        _parse_score(reply)


# --------------------------------------------------------------------------- #
# LLMJudgeScorer
# --------------------------------------------------------------------------- #


def test_judge_scores_completed_output() -> None:
    scorer = LLMJudgeScorer(judge_fn=lambda _inp, _out: 0.9)
    scores = scorer(_result(status="completed", output="grounded answer"))
    assert scores["quality"] == 0.9  # LLM-judged, not structural
    assert scores["completion_rate"] == 1.0  # structural dimensions preserved
    assert "tool_adherence" in scores


def test_judge_receives_input_text() -> None:
    # The judge can assess on-topic correctness because it gets the input.
    scorer = LLMJudgeScorer(judge_fn=lambda inp, _out: 1.0 if "refund" in inp else 0.0)
    assert scorer(_result(output="x"), input_text="refund policy?")["quality"] == 1.0
    assert scorer(_result(output="x"), input_text="weather today?")["quality"] == 0.0


def test_judge_clamps_direct_fn_out_of_range() -> None:
    assert LLMJudgeScorer(lambda _i, _o: 1.5)(_result())["quality"] == 1.0
    assert LLMJudgeScorer(lambda _i, _o: -0.4)(_result())["quality"] == 0.0


def test_judge_skips_failed_run() -> None:
    called = {"n": 0}

    def _judge(_inp: str, _out: str) -> float:
        called["n"] += 1
        return 1.0

    scores = LLMJudgeScorer(_judge)(_result(status="error", output=""))
    assert called["n"] == 0  # judge not invoked on a failed run
    assert scores["quality"] == 0.0  # structural (failed → 0)


def test_judge_skips_empty_output() -> None:
    called = {"n": 0}

    def _judge(_inp: str, _out: str) -> float:
        called["n"] += 1
        return 1.0

    scores = LLMJudgeScorer(_judge)(_result(status="completed", output="   "))
    assert called["n"] == 0
    assert scores["quality"] == 1.0  # structural for a completed, passing run


def test_judge_falls_back_on_error() -> None:
    def _boom(_inp: str, _out: str) -> float:
        raise RuntimeError("judge API down")

    scores = LLMJudgeScorer(_boom)(_result(status="completed", output="answer"))
    assert scores == {"completion_rate": 1.0, "tool_adherence": 1.0, "quality": 1.0}


# --------------------------------------------------------------------------- #
# build_llm_judge_scorer gating
# --------------------------------------------------------------------------- #


def _cfg(**kw):
    base = {
        "workflow_llm_judge_enabled": True,
        "llm_provider": "openai",
        "llm_api_key_env": "OPENAI_API_KEY",
        "llm_diagnosis_model": "gpt-4o-mini",
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_build_judge_none_when_config_none() -> None:
    assert build_llm_judge_scorer(None) is None


def test_build_judge_none_when_disabled() -> None:
    assert build_llm_judge_scorer(_cfg(workflow_llm_judge_enabled=False)) is None


def test_build_judge_none_for_fake_provider() -> None:
    assert build_llm_judge_scorer(_cfg(llm_provider="fake")) is None


def test_build_judge_none_when_no_key(monkeypatch) -> None:
    monkeypatch.setattr(judge_mod, "resolve_secret", lambda _ref: "")
    assert build_llm_judge_scorer(_cfg()) is None


def test_build_judge_returns_scorer_when_configured(monkeypatch) -> None:
    monkeypatch.setattr(judge_mod, "resolve_secret", lambda _ref: "sk-test")
    monkeypatch.setattr(judge_mod, "_build_judge_fn", lambda provider, config: lambda _i, _o: 0.5)
    scorer = build_llm_judge_scorer(_cfg())
    assert isinstance(scorer, LLMJudgeScorer)
    assert scorer(_result())["quality"] == 0.5
