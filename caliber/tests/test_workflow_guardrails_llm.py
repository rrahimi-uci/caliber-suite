"""Tests for the LLM-judge guardrail checks (llm_jailbreak / llm_toxicity / llm_groundedness).

The judge LLM is always mocked (via the ``_judge`` / ``_judge_client`` seams) — no
network, no API key — so these assert the check wiring + fail-open/closed policy,
not a real model's verdicts.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from caliber.workflows import guardrails as gr
from caliber.workflows.guardrails import (
    GuardrailBlockedError,
    GuardrailContext,
    assert_guardrail,
    evaluate_guardrail,
    known_check_kinds,
)
from caliber.workflows.ir import IRGuardrail, IRGuardrailCheck, NodeType


def _node(*checks: IRGuardrailCheck, on_failure: str = "block") -> IRGuardrail:
    return IRGuardrail(
        node_id="g1", node_type=NodeType.GUARDRAIL, checks=list(checks), on_failure=on_failure
    )


# --- registration -----------------------------------------------------------


def test_known_check_kinds_includes_llm_checks() -> None:
    assert {"llm_jailbreak", "llm_toxicity", "llm_groundedness"}.issubset(known_check_kinds())


# --- check behaviour (judge mocked) -----------------------------------------


@pytest.mark.parametrize("kind", ["llm_jailbreak", "llm_toxicity"])
def test_llm_check_blocks_on_violation(monkeypatch, kind: str) -> None:
    monkeypatch.setattr(gr, "_judge", lambda content, criterion, *, model=None: (True, "bad"))
    [res] = evaluate_guardrail(_node(IRGuardrailCheck(kind)), GuardrailContext(response_text="x"))
    assert res.passed is False
    assert res.reason == "bad"


@pytest.mark.parametrize("kind", ["llm_jailbreak", "llm_toxicity"])
def test_llm_check_passes_when_clean(monkeypatch, kind: str) -> None:
    monkeypatch.setattr(gr, "_judge", lambda *a, **k: (False, ""))
    [res] = evaluate_guardrail(_node(IRGuardrailCheck(kind)), GuardrailContext(response_text="hi"))
    assert res.passed is True


def test_fail_open_when_judge_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(gr, "_judge", lambda *a, **k: None)
    [res] = evaluate_guardrail(
        _node(IRGuardrailCheck("llm_toxicity")), GuardrailContext(response_text="x")
    )
    assert res.passed is True
    assert "fail-open" in res.reason


def test_fail_closed_when_judge_unavailable_and_opted_in(monkeypatch) -> None:
    monkeypatch.setattr(gr, "_judge", lambda *a, **k: None)
    [res] = evaluate_guardrail(
        _node(IRGuardrailCheck("llm_toxicity", {"on_judge_error": "block"})),
        GuardrailContext(response_text="x"),
    )
    assert res.passed is False


def test_empty_content_passes_without_judging(monkeypatch) -> None:
    called = {"n": 0}

    def _spy(*a, **k):
        called["n"] += 1
        return (True, "should not run")

    monkeypatch.setattr(gr, "_judge", _spy)
    [res] = evaluate_guardrail(
        _node(IRGuardrailCheck("llm_toxicity")), GuardrailContext(response_text="   ")
    )
    assert res.passed is True
    assert called["n"] == 0  # never consulted the judge for empty text


# --- groundedness ------------------------------------------------------------


def test_groundedness_passes_with_no_evidence(monkeypatch) -> None:
    monkeypatch.setattr(gr, "_judge", lambda *a, **k: (True, "must not be called"))
    [res] = evaluate_guardrail(
        _node(IRGuardrailCheck("llm_groundedness")),
        GuardrailContext(response_text="claim", tool_calls=[]),
    )
    assert res.passed is True  # nothing to ground against → pass, judge not consulted


def test_groundedness_judges_against_tool_evidence(monkeypatch) -> None:
    seen: dict[str, str] = {}

    def _judge(content, criterion, *, model=None):
        seen["content"] = content
        return (True, "unsupported")

    monkeypatch.setattr(gr, "_judge", _judge)
    ctx = GuardrailContext(
        response_text="The sky is green.",
        tool_calls=[{"tool": "lookup", "result": {"sky": "blue"}}],
    )
    [res] = evaluate_guardrail(_node(IRGuardrailCheck("llm_groundedness")), ctx)
    assert res.passed is False
    assert res.reason == "unsupported"
    assert "EVIDENCE:" in seen["content"] and "RESPONSE:" in seen["content"]
    assert "sky" in seen["content"]


# --- _judge parsing (fake client) -------------------------------------------


def test_judge_parses_client_json(monkeypatch) -> None:
    msg = SimpleNamespace(content='{"violation": true, "reason": "x"}')
    completions = SimpleNamespace(
        create=lambda **kw: SimpleNamespace(choices=[SimpleNamespace(message=msg)])
    )
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(gr, "_judge_client", lambda: (fake_client, "gpt-4o-mini"))
    assert gr._judge("text", "crit") == (True, "x")


def test_judge_returns_none_when_no_client(monkeypatch) -> None:
    monkeypatch.setattr(gr, "_judge_client", lambda: None)
    assert gr._judge("text", "crit") is None


def test_judge_returns_none_on_client_error(monkeypatch) -> None:
    def _boom(**kw):
        raise RuntimeError("api down")

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_boom)))
    monkeypatch.setattr(gr, "_judge_client", lambda: (fake_client, "m"))
    assert gr._judge("t", "c") is None


# --- integration: blocking raises -------------------------------------------


def test_assert_guardrail_raises_on_llm_violation(monkeypatch) -> None:
    monkeypatch.setattr(gr, "_judge", lambda *a, **k: (True, "jailbreak"))
    node = _node(IRGuardrailCheck("llm_jailbreak"), on_failure="block")
    with pytest.raises(GuardrailBlockedError):
        assert_guardrail(node, GuardrailContext(response_text="ignore instructions"))
