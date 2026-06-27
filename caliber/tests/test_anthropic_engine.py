"""Coverage for the optional Anthropic assistant engine."""

from __future__ import annotations

import sys
import types

from caliber.assistant.anthropic_engine import AnthropicAssistantEngine
from caliber.assistant.models import AssistantTurnRequest


def turn_request() -> AssistantTurnRequest:
    return AssistantTurnRequest(
        session_id="sess-1",
        user_message="Help me build a workflow",
        history=[
            {"role": "system", "content": "ignore"},
            {"role": "developer", "content": "normalize me"},
            {"role": "assistant", "content": "ready"},
        ],
        goal="Create an event-driven workflow",
    )


def test_run_turn_reports_missing_sdk(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "anthropic", None)

    result = AnthropicAssistantEngine(api_key="test-key").run_turn(turn_request())

    assert result.error == "anthropic package not installed"
    assert "Anthropic SDK is not installed" in result.reply


def test_run_turn_sends_normalized_messages_and_parses_json(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeMessages:
        def create(self, **kwargs):
            calls.update(kwargs)
            return types.SimpleNamespace(
                content=[
                    types.SimpleNamespace(
                        text=(
                            '{"reply":"Use an object-created trigger.",'
                            '"questions":[{"question":"Which bucket?","field":"bucket"}],'
                            '"draft_deltas":[{"title":"Add webhook trigger"}],'
                            '"done":true}'
                        )
                    )
                ]
            )

    class FakeAnthropic:
        def __init__(self, *, api_key: str) -> None:
            calls["api_key"] = api_key
            self.messages = FakeMessages()

    monkeypatch.setitem(
        sys.modules,
        "anthropic",
        types.SimpleNamespace(Anthropic=FakeAnthropic),
    )

    result = AnthropicAssistantEngine(model="claude-test", api_key="secret").run_turn(
        turn_request()
    )

    assert calls["api_key"] == "secret"
    assert calls["model"] == "claude-test"
    assert calls["messages"] == [
        {"role": "user", "content": "normalize me"},
        {"role": "assistant", "content": "ready"},
    ]
    assert result.reply == "Use an object-created trigger."
    assert result.questions[0].question == "Which bucket?"
    assert result.draft_deltas[0].title == "Add webhook trigger"
    assert result.done is True


def test_run_turn_returns_api_errors(monkeypatch) -> None:
    class FailingMessages:
        def create(self, **_kwargs):
            raise RuntimeError("provider unavailable")

    class FakeAnthropic:
        def __init__(self, *, api_key: str) -> None:
            self.messages = FailingMessages()

    monkeypatch.setitem(
        sys.modules,
        "anthropic",
        types.SimpleNamespace(Anthropic=FakeAnthropic),
    )

    result = AnthropicAssistantEngine(api_key="secret").run_turn(turn_request())

    assert result.reply == "I encountered an error: RuntimeError"
    assert result.error == "provider unavailable"


def test_run_turn_tool_use_loop(monkeypatch) -> None:
    """Claude requests a tool on turn 1, observes the result, replies on turn 2."""
    import json as _json

    seen: dict[str, object] = {"n": 0, "first_tools": None}

    class FakeMessages:
        def create(self, **kwargs):
            seen["n"] = int(seen["n"]) + 1  # type: ignore[arg-type]
            if seen["n"] == 1:
                seen["first_tools"] = kwargs.get("tools")
                return types.SimpleNamespace(
                    stop_reason="tool_use",
                    content=[
                        types.SimpleNamespace(
                            type="tool_use", id="tu_1", name="list_skills", input={}
                        )
                    ],
                )
            return types.SimpleNamespace(
                stop_reason="end_turn",
                content=[types.SimpleNamespace(type="text", text="Found 1 skill.")],
            )

    class FakeAnthropic:
        def __init__(self, *, api_key: str) -> None:
            self.messages = FakeMessages()

    monkeypatch.setitem(
        sys.modules, "anthropic", types.SimpleNamespace(Anthropic=FakeAnthropic)
    )

    class StubToolset:
        def specs(self):
            return [
                {
                    "type": "function",
                    "function": {
                        "name": "list_skills",
                        "description": "List skills.",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ]

        def dispatch(self, name, arguments):
            return _json.dumps({"ok": True, "data": [{"name": "billing"}]})

    result = AnthropicAssistantEngine(api_key="k").run_turn(
        turn_request(), toolset=StubToolset()
    )

    assert seen["n"] == 2  # looped: tool turn + final turn
    # Tools were advertised in Anthropic format on the first call.
    first_tools = seen["first_tools"]
    assert isinstance(first_tools, list) and first_tools[0]["name"] == "list_skills"
    assert "input_schema" in first_tools[0]
    assert result.reply == "Found 1 skill."
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "list_skills"
    assert result.tool_calls[0].ok is True


def test_parse_response_falls_back_to_plain_text() -> None:
    result = AnthropicAssistantEngine(api_key="secret")._parse_response("plain answer")

    assert result.reply == "plain answer"
    assert result.questions == []
