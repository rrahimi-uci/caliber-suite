"""Tests for the LLM provider factory and shared types."""

from __future__ import annotations

import pytest

from caliber.config import CaliberConfig
from caliber.llm import Diagnosis, build_provider
from caliber.llm.circuit_breaker import CircuitBreakerLLMProvider
from caliber.llm.fake import FakeLLMProvider
from caliber.llm.provider import LLMProviderError


def _config(**overrides: object) -> CaliberConfig:
    return CaliberConfig.load(
        environ={
            "CALIBER_DATABASE_URL": "sqlite:///:memory:",
            **{f"CALIBER_{k.upper()}": str(v) for k, v in overrides.items()},
        }
    )


def test_build_provider_defaults_to_fake_under_breaker() -> None:
    """``llm_provider`` defaults to fake, wrapped in the circuit breaker
    (the breaker is on by default)."""
    provider = build_provider(_config())
    assert isinstance(provider, CircuitBreakerLLMProvider)


def test_build_provider_fake_unwrapped_when_breaker_disabled() -> None:
    """With the breaker disabled, ``build_provider`` returns the raw inner provider."""
    provider = build_provider(_config(llm_circuit_breaker_enabled="false"))
    assert isinstance(provider, FakeLLMProvider)


def test_build_provider_openai_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Selecting the openai provider without the API-key env var is a fail-fast error."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(LLMProviderError, match=r"OPENAI_API_KEY"):
        build_provider(_config(llm_provider="openai"))


def test_build_provider_unknown_raises() -> None:
    with pytest.raises(LLMProviderError, match=r"unknown llm_provider"):
        build_provider(_config(llm_provider="bogus"))


def test_openai_provider_overrides_inherited_openai_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deep-review V2 Finding 7: ``OPENAI_API_KEY`` already in the
    process env must NOT win over the explicitly-configured key
    resolved from ``llm_api_key_env``. Without this, a deployment
    that intentionally sources its key via ``file://`` or a custom
    env-var name would silently use whatever the shell exported."""
    from caliber.llm.openai_agents import OpenAIAgentsLLMProvider

    monkeypatch.setenv("OPENAI_API_KEY", "sk-stale-inherited")
    # Construct the provider directly with the resolved key.
    OpenAIAgentsLLMProvider(api_key="sk-explicit-config", diagnosis_model="gpt-4o-mini")
    # The unconditional assignment in ``_set_api_key`` wins.
    import os

    assert os.environ["OPENAI_API_KEY"] == "sk-explicit-config"


def test_build_provider_threads_flagged_dspy_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live-test-key")
    provider = build_provider(
        _config(
            llm_provider="openai",
            llm_circuit_breaker_enabled="false",
            allow_flagged_dspy_optimizers="true",
        )
    )
    from caliber.llm.openai_agents import OpenAIAgentsLLMProvider

    assert isinstance(provider, OpenAIAgentsLLMProvider)
    assert provider._allow_flagged_dspy_optimizers is True


def test_diagnosis_rejects_out_of_range_confidence() -> None:
    """The Diagnosis schema enforces ``0 ≤ confidence ≤ 1``."""
    with pytest.raises(Exception, match=r"(?i)confidence"):
        Diagnosis(root_cause="x", confidence=1.5)


def test_diagnosis_rejects_empty_root_cause() -> None:
    with pytest.raises(Exception, match=r"(?i)root_cause"):
        Diagnosis(root_cause="", confidence=0.5)
