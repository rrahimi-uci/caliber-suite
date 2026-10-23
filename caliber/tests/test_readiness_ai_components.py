"""``/readiness`` says, per AI-backed component, whether a real model stands behind it.

The pre-existing ``simulated`` / ``all_real`` fields report four *selectors* (``llm``, ``eval``,
``promoter``, ``artifact_store``). A deployment can pass that and still have a fake Aria engine, a
knowledge-base answerer returning canned text, a judge that is off, or an evaluator set to
``mlflow`` beside a simulated LLM that therefore cannot score anything. ``ai_components`` is the
per-component answer to "is anything still fake?", and it reads configuration and key *presence*
only, never a secret value.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from starlette.testclient import TestClient

from caliber.observability.readiness import AIComponent, ai_components, all_ai_real

REAL_ENV = {"OPENAI_API_KEY": "sk-test", "ANTHROPIC_API_KEY": "sk-ant-test"}


def _config(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "llm_provider": "openai",
        "eval_provider": "mlflow",
        "llm_api_key_env": "OPENAI_API_KEY",
        "workflow_llm_judge_enabled": True,
        "feature_aria_enabled": True,
        "assistant_enabled": True,
        "assistant_engine": "auto",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _modes(config: SimpleNamespace, environ: dict[str, str]) -> dict[str, str]:
    return {c.name: c.mode for c in ai_components(config, environ)}


def test_a_fully_real_deployment_reports_every_component_real() -> None:
    components = ai_components(_config(), REAL_ENV)
    assert _modes(_config(), REAL_ENV) == {
        "workflow_agents": "real",
        "evaluation": "real",
        "workflow_judge": "real",
        "aria": "real",
        "knowledge_answers": "real",
    }
    assert all_ai_real(components) is True


def test_the_shipped_simulated_defaults_are_reported_as_simulated() -> None:
    config = _config(
        llm_provider="fake",
        eval_provider="fake",
        workflow_llm_judge_enabled=False,
        assistant_engine="fake",
    )
    modes = _modes(config, {})
    assert modes["workflow_agents"] == "simulated"
    assert modes["evaluation"] == "simulated"
    assert modes["aria"] == "simulated"
    assert modes["knowledge_answers"] == "simulated"
    assert all_ai_real(ai_components(config, {})) is False


def test_a_real_evaluator_beside_a_simulated_llm_is_unconfigured_not_real() -> None:
    """The old readiness called ``mlflow`` real even though it refuses to score with a fake LLM."""
    config = _config(llm_provider="fake", eval_provider="mlflow")
    assert _modes(config, REAL_ENV)["evaluation"] == "unconfigured"


def test_a_missing_key_is_unconfigured_for_each_component_that_needs_it() -> None:
    modes = _modes(_config(), {})
    assert modes["workflow_agents"] == "unconfigured"
    assert modes["evaluation"] == "unconfigured"
    assert modes["aria"] == "unconfigured"
    assert modes["knowledge_answers"] == "unconfigured"
    assert all_ai_real(ai_components(_config(), {})) is False


def test_the_text_never_names_a_key_variable_or_value() -> None:
    """The probe is unauthenticated, and a route test forbids these words in its body."""
    secret_env = {"OPENAI_API_KEY": "sk-super-secret-value"}
    for environ in ({}, secret_env, REAL_ENV):
        text = " ".join(c.detail for c in ai_components(_config(), environ)).lower()
        for needle in ("api_key", "secret", "sk-"):
            assert needle not in text


def test_auto_engine_with_only_an_anthropic_key_is_a_real_claude_aria() -> None:
    env = {"ANTHROPIC_API_KEY": "sk-ant-test"}
    components = {c.name: c for c in ai_components(_config(llm_provider="fake"), env)}
    assert components["aria"].mode == "real"
    assert "anthropic" in components["aria"].detail
    # ...and the honest other half: the workflow agents cannot use the Anthropic key.
    assert components["workflow_agents"].mode == "simulated"


def test_a_secret_uri_key_source_counts_as_configured() -> None:
    config = _config(llm_api_key_env="secret://llm-key")
    assert _modes(config, {})["workflow_agents"] == "real"


def test_the_judge_is_off_not_simulated_when_disabled_and_does_not_fail_all_ai_real() -> None:
    config = _config(workflow_llm_judge_enabled=False)
    components = ai_components(config, REAL_ENV)
    judge = next(c for c in components if c.name == "workflow_judge")
    assert judge.mode == "off"
    assert "structural proxy" in judge.detail
    assert all_ai_real(components) is True  # a deliberate choice, but visible


def test_the_judge_needs_a_real_llm_even_when_enabled() -> None:
    config = _config(llm_provider="fake", workflow_llm_judge_enabled=True)
    assert _modes(config, REAL_ENV)["workflow_judge"] == "off"


def test_disabling_aria_turns_off_aria_but_not_the_knowledge_answerer() -> None:
    config = _config(feature_aria_enabled=False)
    modes = _modes(config, REAL_ENV)
    assert modes["aria"] == "off"
    assert modes["knowledge_answers"] == "real"  # same engine setting, independent of the flag


def test_assistant_disabled_turns_aria_off() -> None:
    assert _modes(_config(assistant_enabled=False), REAL_ENV)["aria"] == "off"


def test_ollama_needs_no_key() -> None:
    modes = _modes(_config(assistant_engine="ollama"), {})
    assert modes["aria"] == "real"


def test_to_dict_is_json_ready() -> None:
    assert AIComponent("aria", "real", "x").to_dict() == {
        "name": "aria",
        "mode": "real",
        "detail": "x",
    }


def test_the_readiness_route_carries_the_component_report(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    body = client.get("/ajax-api/2.0/mlflow/caliber/readiness").json()["data"]
    names = [c["name"] for c in body["ai_components"]]
    assert names == [
        "workflow_agents",
        "evaluation",
        "workflow_judge",
        "aria",
        "knowledge_answers",
    ]
    assert {c["mode"] for c in body["ai_components"]} <= {
        "real",
        "simulated",
        "unconfigured",
        "off",
    }
    assert isinstance(body["all_ai_real"], bool)
    # The pre-existing contract is untouched.
    assert "simulated" in body and "all_real" in body and "providers" in body
