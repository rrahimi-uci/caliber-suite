"""Tests for the OpenAI assistant engine (SDK faked — no network)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from caliber.assistant.models import AssistantTurnRequest
from caliber.assistant.openai_engine import OpenAIAssistantEngine


def _request() -> AssistantTurnRequest:
    return AssistantTurnRequest(
        session_id="s1",
        user_message="help me",
        history=[{"role": "user", "content": "hi"}],
    )


def _patch_openai(
    monkeypatch: pytest.MonkeyPatch, *, content: str = "", error: Exception | None = None
) -> None:
    import openai

    class _FakeOpenAI:
        def __init__(self, **_: Any) -> None:
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        def _create(self, **_: Any) -> Any:
            if error is not None:
                raise error
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)


def test_init_warns_without_key(caplog: pytest.LogCaptureFixture) -> None:
    engine = OpenAIAssistantEngine(api_key="", api_key_env="NO_SUCH_KEY_ENV_VAR")
    assert engine._api_key == ""


def test_run_turn_parses_structured_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_openai(
        monkeypatch,
        content='{"reply": "done", "questions": [{"question": "q?"}], "draft_deltas": [{}], "done": true}',
    )
    engine = OpenAIAssistantEngine(api_key="sk-x")
    result = engine.run_turn(_request())
    assert result.reply == "done"
    assert result.done is True
    assert len(result.questions) == 1
    assert len(result.draft_deltas) == 1


def test_run_turn_plain_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_openai(monkeypatch, content="just text")
    engine = OpenAIAssistantEngine(api_key="sk-x")
    result = engine.run_turn(_request())
    assert result.reply == "just text"
    assert result.done is False


def test_run_turn_reasoning_model(monkeypatch: pytest.MonkeyPatch) -> None:
    # o-series models also take ``reasoning_effort``; assert the kwarg shape so a
    # regression in is_reasoning_model's o-series detection is caught.
    import openai

    seen: dict[str, Any] = {}

    class _FakeOpenAI:
        def __init__(self, **_: Any) -> None:
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        def _create(self, **kwargs: Any) -> Any:
            seen.update(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)

    engine = OpenAIAssistantEngine(api_key="sk-x", model="o3-mini", reasoning="high")
    assert engine.run_turn(_request()).reply == "ok"
    assert seen["reasoning_effort"] == "high"
    assert "reasoning" not in seen


def test_run_turn_gpt5_sends_reasoning_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    import openai

    seen: dict[str, Any] = {}

    class _FakeOpenAI:
        def __init__(self, **_: Any) -> None:
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        def _create(self, **kwargs: Any) -> Any:
            seen.update(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)

    engine = OpenAIAssistantEngine(api_key="sk-x", model="gpt-5.2", reasoning="high")
    assert engine.run_turn(_request()).reply == "ok"
    # Chat Completions takes the top-level string ``reasoning_effort`` — NOT the
    # nested ``reasoning={"effort": ...}`` Responses-API object (which 400s here).
    assert seen["reasoning_effort"] == "high"
    assert "reasoning" not in seen


def test_run_turn_omits_blank_reasoning_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    import openai

    seen: dict[str, Any] = {}

    class _FakeOpenAI:
        def __init__(self, **_: Any) -> None:
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        def _create(self, **kwargs: Any) -> Any:
            seen.update(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)

    engine = OpenAIAssistantEngine(api_key="sk-x", model="gpt-5.2", reasoning="")
    assert engine.run_turn(_request()).reply == "ok"
    assert "reasoning" not in seen
    assert "reasoning_effort" not in seen


def test_run_turn_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_openai(monkeypatch, error=RuntimeError("rate limited"))
    engine = OpenAIAssistantEngine(api_key="sk-x")
    result = engine.run_turn(_request())
    assert result.error is not None and "rate limited" in result.error
    assert "error" in result.reply.lower()


def test_parse_response_invalid_json_falls_back() -> None:
    engine = OpenAIAssistantEngine(api_key="sk-x")
    assert engine._parse_response("not json {").reply == "not json {"
    # JSON that isn't an object → fall through to plain reply
    assert engine._parse_response("[1, 2, 3]").reply == "[1, 2, 3]"


def test_parse_response_json_object_without_reply_is_plain_text() -> None:
    # A JSON *object* the model returns as content (e.g. a JSON sample the user
    # asked for) must not be misread as a turn envelope — only ``{"reply": ...}``
    # is treated as structured. The raw text is preserved as the reply.
    engine = OpenAIAssistantEngine(api_key="sk-x")
    payload = '{"name": "widget", "questions": ["not clarifying questions"]}'
    result = engine._parse_response(payload)
    assert result.reply == payload
    assert result.questions == []


# ───────────────────── tool-calling loop ─────────────────────


class _ToolCall:
    def __init__(self, call_id: str, name: str, args: str) -> None:
        self.id = call_id
        self.function = SimpleNamespace(name=name, arguments=args)


class _FakeDispatcher:
    """Minimal AssistantToolDispatcher recording dispatched calls."""

    def __init__(self) -> None:
        self.dispatched: list[tuple[str, dict[str, Any]]] = []

    def specs(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "list_skills",
                    "description": "list skills",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            }
        ]

    def dispatch(self, name: str, arguments: dict[str, Any]) -> str:
        self.dispatched.append((name, arguments))
        return '[{"name": "alpha"}, {"name": "beta"}]'


def test_run_turn_drives_tool_calling_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    import openai

    state = {"create": 0, "saw_tools": []}

    class _FakeOpenAI:
        def __init__(self, **_: Any) -> None:
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        def _create(self, **kwargs: Any) -> Any:
            state["create"] += 1
            state["saw_tools"].append("tools" in kwargs)
            if state["create"] == 1:
                # First round: the model requests a tool call.
                msg = SimpleNamespace(
                    content=None, tool_calls=[_ToolCall("tc1", "list_skills", "{}")]
                )
            else:
                # Second round (after the tool result): the final reply.
                msg = SimpleNamespace(content="You have 2 skills: alpha, beta.", tool_calls=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)

    dispatcher = _FakeDispatcher()
    engine = OpenAIAssistantEngine(api_key="sk-x", model="gpt-4o", tool_dispatcher=dispatcher)
    result = engine.run_turn(_request())

    assert state["create"] == 2  # tool round + final reply
    assert dispatcher.dispatched == [("list_skills", {})]
    assert result.reply == "You have 2 skills: alpha, beta."
    assert all(state["saw_tools"])  # tools advertised on every call in the loop


def test_run_turn_without_dispatcher_is_single_shot(monkeypatch: pytest.MonkeyPatch) -> None:
    import openai

    state = {"create": 0, "saw_tools": False}

    class _FakeOpenAI:
        def __init__(self, **_: Any) -> None:
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        def _create(self, **kwargs: Any) -> Any:
            state["create"] += 1
            state["saw_tools"] = "tools" in kwargs
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="hi"))])

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)

    engine = OpenAIAssistantEngine(api_key="sk-x")  # no dispatcher
    result = engine.run_turn(_request())

    assert state["create"] == 1  # single shot
    assert state["saw_tools"] is False  # no tools advertised
    assert result.reply == "hi"


def test_run_turn_tool_loop_caps_iterations(monkeypatch: pytest.MonkeyPatch) -> None:
    import openai

    state = {"create": 0}

    class _FakeOpenAI:
        def __init__(self, **_: Any) -> None:
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        def _create(self, **kwargs: Any) -> Any:
            state["create"] += 1
            # The model never stops requesting tools while tools are offered;
            # the final (capped) call has no tools and must return content.
            if "tools" in kwargs:
                msg = SimpleNamespace(
                    content=None, tool_calls=[_ToolCall("tc", "list_skills", "{}")]
                )
            else:
                msg = SimpleNamespace(content="forced final reply", tool_calls=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)

    engine = OpenAIAssistantEngine(
        api_key="sk-x", model="gpt-4o", tool_dispatcher=_FakeDispatcher()
    )
    result = engine.run_turn(_request())

    # 5 capped tool rounds + 1 forced tool-free final call.
    assert state["create"] == OpenAIAssistantEngine._MAX_TOOL_ITERATIONS + 1
    assert result.reply == "forced final reply"
