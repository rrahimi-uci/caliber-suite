"""A key written after the engine was built must reach its next turn.

``PATCH /settings/llm`` and Settings -> Providers write into the process environment. The
workflow executor resolves its key per run, but the Aria engines used to read theirs once in
``__init__``, so rotating a key left Aria running on the key (or the *absence* of a key) it booted
with until the server restarted.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from caliber.assistant.anthropic_engine import AnthropicAssistantEngine
from caliber.assistant.openai_engine import OpenAIAssistantEngine


def test_the_openai_engine_sees_a_key_set_after_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    engine = OpenAIAssistantEngine()
    assert engine._api_key == ""

    monkeypatch.setenv("OPENAI_API_KEY", "sk-rotated-in")
    assert engine._api_key == "sk-rotated-in"

    monkeypatch.setenv("OPENAI_API_KEY", "sk-rotated-again")
    assert engine._api_key == "sk-rotated-again"


def test_the_anthropic_engine_sees_a_key_set_after_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    engine = AnthropicAssistantEngine()
    assert engine._api_key == ""

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-rotated-in")
    assert engine._api_key == "sk-ant-rotated-in"


def test_an_explicit_constructor_key_still_wins_over_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    assert OpenAIAssistantEngine(api_key="sk-explicit")._api_key == "sk-explicit"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
    assert AnthropicAssistantEngine(api_key="sk-ant-explicit")._api_key == "sk-ant-explicit"


def test_a_custom_key_variable_is_honoured_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = OpenAIAssistantEngine(api_key_env="MY_TEAM_OPENAI_KEY")
    monkeypatch.delenv("MY_TEAM_OPENAI_KEY", raising=False)
    assert engine._api_key == ""
    monkeypatch.setenv("MY_TEAM_OPENAI_KEY", "sk-team")
    assert engine._api_key == "sk-team"


def test_the_setter_remains_available_for_callers_that_assign_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    engine = OpenAIAssistantEngine()
    engine._api_key = "sk-assigned"
    assert engine._api_key == "sk-assigned"


def test_a_turn_uses_the_key_in_the_environment_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end through ``run_turn``: the client is built with the *current* key."""
    import openai

    seen: list[str] = []

    class _Client:
        def __init__(self, *, api_key: str, **_: Any) -> None:
            seen.append(api_key)
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(
                    create=lambda **__: SimpleNamespace(
                        choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))]
                    )
                )
            )

    monkeypatch.setattr(openai, "OpenAI", _Client)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-at-boot")
    engine = OpenAIAssistantEngine()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-after-rotation")

    from caliber.assistant.models import AssistantTurnRequest

    engine.run_turn(AssistantTurnRequest(session_id="s", user_message="hi"))

    assert seen == ["sk-after-rotation"]
