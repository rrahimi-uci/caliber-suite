"""Tests for the assistant engine default posture — real providers, no fake auto.

Production defaults the assistant to ``auto``, which picks a real provider
(OpenAI or Claude) by available API key and never resolves to the fake stub.
"""

from __future__ import annotations

import pytest

from caliber.config import resolve_assistant_engine
from caliber.server import _assistant_model_kwargs, _resolve_assistant_engine_name


class TestAutoResolution:
    def test_prefers_openai_when_key_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-y")
        assert _resolve_assistant_engine_name("auto") == "openai"

    def test_uses_anthropic_when_only_claude_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-y")
        assert _resolve_assistant_engine_name("auto") == "anthropic"

    def test_defaults_to_openai_with_no_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        # Never fake — a live deployment always points at a real provider.
        assert _resolve_assistant_engine_name("auto") == "openai"

    def test_explicit_values_pass_through(self) -> None:
        assert _resolve_assistant_engine_name("anthropic") == "anthropic"
        assert _resolve_assistant_engine_name("ollama") == "ollama"
        assert _resolve_assistant_engine_name("fake") == "fake"


class TestCreateAppWiring:
    def test_auto_with_claude_key_builds_anthropic_engine(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-claude")
        from caliber.assistant.anthropic_engine import AnthropicAssistantEngine
        from caliber.config import CaliberConfig
        from caliber.server import create_app

        cfg = CaliberConfig.load(
            environ={
                "CALIBER_DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'a.db'}",
                "CALIBER_ASSISTANT_ENGINE": "auto",
            }
        )
        app = create_app(config=cfg)
        engine = app.state.assistant_service._engine
        assert isinstance(engine, AnthropicAssistantEngine)


class TestSharedResolver:
    """One definition of ``auto`` for the server, the assistant routes and the KB answerer."""

    def test_reads_the_mapping_it_is_given_not_the_process_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-process")
        assert resolve_assistant_engine("auto", {"ANTHROPIC_API_KEY": "sk-ant"}) == "anthropic"
        assert resolve_assistant_engine("auto", {}) == "openai"

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_a_blank_key_is_not_a_key(self, blank: str) -> None:
        env = {"OPENAI_API_KEY": blank, "ANTHROPIC_API_KEY": "sk-ant"}
        assert resolve_assistant_engine("auto", env) == "anthropic"

    def test_is_case_and_whitespace_insensitive_and_never_returns_fake_for_auto(self) -> None:
        assert resolve_assistant_engine(" AUTO ", {}) == "openai"
        assert resolve_assistant_engine("Fake", {}) == "fake"
        assert resolve_assistant_engine("", {}) == ""


class TestAssistantModelKwargs:
    def test_empty_model_means_the_engines_own_default(self) -> None:
        assert _assistant_model_kwargs("openai", "") == {}
        assert _assistant_model_kwargs("anthropic", "") == {}

    def test_a_model_is_passed_through_to_a_matching_engine(self) -> None:
        assert _assistant_model_kwargs("openai", "gpt-5.6-luna") == {"model": "gpt-5.6-luna"}
        assert _assistant_model_kwargs("anthropic", "claude-sonnet-4") == {
            "model": "claude-sonnet-4"
        }

    def test_an_openai_model_name_is_dropped_for_the_anthropic_engine(self) -> None:
        """An Anthropic-only deployment inherits the shipped OpenAI default; Claude rejects it."""
        assert _assistant_model_kwargs("anthropic", "gpt-5.6-luna") == {}
