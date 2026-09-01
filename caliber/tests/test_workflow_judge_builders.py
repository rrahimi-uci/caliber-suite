"""Tests for the workflow LLM-judge fn builders and config gating.

Covers the OpenAI/Anthropic judge-fn closures (with the SDK clients faked, so no
network), ``_build_judge_fn``, and ``build_llm_judge_scorer`` gating — the paths
not exercised by ``test_workflow_judge.py`` (which focuses on scoring/parsing).
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from typing import Any

import pytest

from caliber.workflows import judge


def _config(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "workflow_llm_judge_enabled": True,
        "llm_provider": "openai",
        "llm_api_key_env": "JUDGE_TEST_KEY",
        "llm_diagnosis_model": "gpt-4o-mini",
        "llm_reasoning_effort": "high",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_judge_user_message() -> None:
    assert judge._judge_user_message("q", "r") == "QUESTION:\nq\n\nRESPONSE:\nr"
    assert "(no question provided)" in judge._judge_user_message("", "r")


def test_openai_judge_fn(monkeypatch: pytest.MonkeyPatch) -> None:
    import openai

    class _FakeOpenAI:
        def __init__(self, **_: Any) -> None:
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        def _create(self, **_: Any) -> Any:
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="0.8"))]
            )

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    fn = judge._openai_judge_fn("sk-x", "gpt-4o-mini")
    assert fn("question", "response") == 0.8


def test_anthropic_judge_fn(monkeypatch: pytest.MonkeyPatch) -> None:
    # Inject a fake ``anthropic`` module so this runs (and covers the closure)
    # without the optional [anthropic] extra installed — matching the lint env.
    class _FakeAnthropic:
        def __init__(self, **_: Any) -> None:
            self.messages = SimpleNamespace(create=self._create)

        def _create(self, **_: Any) -> Any:
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="0.7")])

    fake_mod = types.ModuleType("anthropic")
    fake_mod.Anthropic = _FakeAnthropic  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)
    fn = judge._anthropic_judge_fn("sk-x", "claude-x")
    assert fn("question", "response") == 0.7


def test_build_judge_fn_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JUDGE_TEST_KEY", raising=False)
    assert judge._build_judge_fn("openai", _config()) is None


def test_build_judge_fn_unknown_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JUDGE_TEST_KEY", "sk-x")
    assert judge._build_judge_fn("cohere", _config()) is None


def test_build_judge_fn_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JUDGE_TEST_KEY", "sk-x")
    assert callable(judge._build_judge_fn("openai", _config()))


def test_build_llm_judge_scorer_gating(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JUDGE_TEST_KEY", "sk-x")
    assert judge.build_llm_judge_scorer(None) is None
    assert judge.build_llm_judge_scorer(_config(workflow_llm_judge_enabled=False)) is None
    assert judge.build_llm_judge_scorer(_config(llm_provider="fake")) is None
    scorer = judge.build_llm_judge_scorer(_config(llm_provider="openai"))
    assert isinstance(scorer, judge.LLMJudgeScorer)


def test_build_llm_judge_scorer_none_when_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JUDGE_TEST_KEY", raising=False)
    assert judge.build_llm_judge_scorer(_config(llm_provider="openai")) is None


def test_llm_judge_status_reports_disabled() -> None:
    status = judge.llm_judge_status(_config(workflow_llm_judge_enabled=False))
    assert status["available"] is False
    assert "workflow_llm_judge_enabled" in status["reason"]


def test_llm_judge_status_reports_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JUDGE_TEST_KEY", "sk-x")
    status = judge.llm_judge_status(_config(llm_provider="openai"))
    assert status == {
        "available": True,
        "provider": "openai",
        "model": "gpt-4o-mini",
        "reason": None,
    }
