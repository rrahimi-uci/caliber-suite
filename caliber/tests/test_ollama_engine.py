"""Tests for the optional Ollama assistant engine."""

from __future__ import annotations

import json
import urllib.error

from caliber.assistant.models import AssistantTurnRequest
from caliber.assistant.ollama_engine import OllamaAssistantEngine


def _request() -> AssistantTurnRequest:
    return AssistantTurnRequest(
        session_id="s1",
        user_message="help me",
        history=[
            {"role": "developer", "content": "normalize me"},
            {"role": "assistant", "content": "ready"},
        ],
    )


def test_run_turn_posts_chat_request_and_parses_json(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {
                    "message": {
                        "content": '{"reply":"Use a queue trigger.","questions":[{"question":"Which queue?","field":"queue"}],"done":true}'
                    }
                }
            ).encode("utf-8")

    def _urlopen(req, timeout: float):
        calls["url"] = req.full_url
        calls["timeout"] = timeout
        calls["payload"] = json.loads(req.data.decode("utf-8"))
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)

    result = OllamaAssistantEngine(model="qwen2.5:7b").run_turn(_request())

    assert calls["url"] == "http://127.0.0.1:11434/api/chat"
    assert calls["timeout"] == 60.0
    assert calls["payload"]["model"] == "qwen2.5:7b"
    assert calls["payload"]["stream"] is False
    assert calls["payload"]["messages"][0]["role"] == "system"
    assert calls["payload"]["messages"][1:] == [
        {"role": "user", "content": "normalize me"},
        {"role": "assistant", "content": "ready"},
    ]
    assert result.reply == "Use a queue trigger."
    assert result.questions[0].question == "Which queue?"
    assert result.done is True


def test_run_turn_returns_http_errors(monkeypatch) -> None:
    class _FakeHTTPError(urllib.error.HTTPError):
        def __init__(self, url: str) -> None:
            super().__init__(url, 500, "boom", hdrs=None, fp=None)

        def read(self) -> bytes:
            return b"provider unavailable"

    def _urlopen(req, timeout: float):
        raise _FakeHTTPError(req.full_url)

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)

    result = OllamaAssistantEngine(model="qwen2.5:7b").run_turn(_request())

    assert result.reply == "I encountered an error: HTTPError"
    assert result.error is not None
    assert "provider unavailable" in result.error


def test_parse_response_falls_back_to_plain_text() -> None:
    result = OllamaAssistantEngine(model="qwen2.5:7b")._parse_response("plain answer")

    assert result.reply == "plain answer"
    assert result.questions == []
