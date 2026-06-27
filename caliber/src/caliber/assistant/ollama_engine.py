"""Ollama-backed assistant engine."""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

from caliber.assistant.models import (
    AssistantTurnRequest,
    AssistantTurnResult,
    ClarifyingQuestion,
    DraftDelta,
)
from caliber.assistant.prompt_builder import build_assistant_system_prompt

logger = logging.getLogger(__name__)


class OllamaAssistantEngine:
    """Production engine wrapping the local Ollama chat API."""

    def __init__(
        self,
        *,
        model: str = "qwen2.5:7b",
        base_url: str | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        self._model = model
        self._base_url = (
            base_url or os.environ.get("OLLAMA_BASE_URL") or "http://127.0.0.1:11434"
        ).rstrip("/")
        self._timeout_seconds = timeout_seconds

    def run_turn(
        self,
        request: AssistantTurnRequest,
        *,
        toolset: object | None = None,  # noqa: ARG002 — single-shot; no tool loop yet
    ) -> AssistantTurnResult:
        messages: list[dict[str, str]] = [
            {"role": "system", "content": build_assistant_system_prompt(request)}
        ]
        for msg in request.history:
            role = msg.get("role", "user")
            if role not in {"system", "user", "assistant"}:
                role = "user"
            messages.append({"role": role, "content": str(msg.get("content", ""))})

        body = json.dumps(
            {"model": self._model, "stream": False, "messages": messages}
        ).encode("utf-8")
        req = urllib.request.Request(  # noqa: S310 - fixed http(s) Ollama base_url from config, not user input
            f"{self._base_url}/api/chat",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self._timeout_seconds) as response:  # noqa: S310 - trusted internal Ollama endpoint
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            finally:
                exc.close()
            logger.error("Ollama API error: %s %s", exc.code, detail)
            return AssistantTurnResult(
                reply="I encountered an error: HTTPError",
                error=f"HTTP {exc.code}: {detail[:500]}",
            )
        except Exception as exc:
            logger.error("Ollama API error: %s", exc)
            return AssistantTurnResult(
                reply=f"I encountered an error: {type(exc).__name__}",
                error=str(exc)[:500],
            )

        try:
            payload = json.loads(raw)
            content = payload.get("message", {}).get("content", "")
            if not isinstance(content, str):
                content = str(content)
        except (json.JSONDecodeError, AttributeError, TypeError):
            content = raw
        return self._parse_response(content)

    def _parse_response(self, content: str) -> AssistantTurnResult:
        try:
            data = json.loads(content)
            if isinstance(data, dict) and "reply" in data:
                questions = [
                    ClarifyingQuestion(**q)
                    if isinstance(q, dict)
                    else ClarifyingQuestion(question=str(q))
                    for q in data.get("questions", [])
                ]
                deltas = [
                    DraftDelta(**d) if isinstance(d, dict) else DraftDelta()
                    for d in data.get("draft_deltas", [])
                ]
                return AssistantTurnResult(
                    reply=data.get("reply", content),
                    questions=questions,
                    draft_deltas=deltas,
                    done=data.get("done", False),
                )
        except (json.JSONDecodeError, TypeError):
            pass

        return AssistantTurnResult(reply=content)
