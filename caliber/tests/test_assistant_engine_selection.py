"""Tests for the assistant engine default posture — real providers, no fake auto.

Production defaults the assistant to ``auto``, which picks a real provider
(OpenAI or Claude) by available API key and never resolves to the fake stub.
"""

from __future__ import annotations

import pytest

from caliber.server import _resolve_assistant_engine_name


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
