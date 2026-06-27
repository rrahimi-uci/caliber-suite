"""Anthropic assistant engine.

Uses Claude models via the Anthropic SDK.  The API key is read from the
``ANTHROPIC_API_KEY`` environment variable.

When a per-turn ``toolset`` is supplied, ``run_turn`` runs a real Anthropic
tool-use loop (parity with the OpenAI engine): Claude can call the toolset's
read/execute tools mid-turn, observe the results, and iterate, with results fed
back as ``tool_result`` blocks until it produces a final answer. Without a
toolset it is a single-shot completion (unchanged).
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Any

from caliber.assistant.models import (
    AssistantToolCall,
    AssistantTurnRequest,
    AssistantTurnResult,
    ClarifyingQuestion,
    DraftDelta,
)
from caliber.assistant.prompt_builder import build_assistant_system_prompt

if TYPE_CHECKING:
    from caliber.assistant.tools import AssistantToolDispatcher

logger = logging.getLogger(__name__)


class AnthropicAssistantEngine:
    """Production engine wrapping the Anthropic SDK.

    Lazy-imports ``anthropic`` so the server can start without it installed.
    """

    # Bounded so a model that keeps requesting tools can't loop forever; high
    # enough for a real run -> observe -> fix -> re-run loop within a turn.
    _MAX_TOOL_ITERATIONS = 8

    def __init__(
        self,
        *,
        model: str = "claude-sonnet-4-20250514",
        api_key: str | None = None,
        api_key_env: str = "ANTHROPIC_API_KEY",
    ) -> None:
        self._model = model
        self._api_key = api_key or os.environ.get(api_key_env, "")
        if not self._api_key:
            logger.warning(
                "Anthropic API key not found in environment variable %s. "
                "Assistant engine calls will fail until a valid key is set.",
                api_key_env,
            )

    def run_turn(
        self,
        request: AssistantTurnRequest,
        *,
        toolset: AssistantToolDispatcher | None = None,
    ) -> AssistantTurnResult:
        try:
            from anthropic import Anthropic  # type: ignore[import-not-found]  # noqa: PLC0415
        except ImportError:
            return AssistantTurnResult(
                reply="Anthropic SDK is not installed. Please install 'anthropic' to use this engine.",
                error="anthropic package not installed",
            )

        client = Anthropic(api_key=self._api_key)
        system_prompt = self._build_system_prompt(request)
        messages: list[dict[str, Any]] = []
        for msg in request.history:
            role = msg.get("role", "user")
            if role == "system":
                continue
            if role not in ("user", "assistant"):
                role = "user"
            messages.append({"role": role, "content": msg.get("content", "")})

        tools = _to_anthropic_tools(toolset.specs()) if toolset is not None else None
        executed: list[AssistantToolCall] = []

        try:
            # Tool-use loop: keep going while Claude requests tools, up to the cap.
            # With no toolset, ``tools`` is None and this is a single completion.
            for _ in range(self._MAX_TOOL_ITERATIONS):
                response = self._create(client, system_prompt, messages, tools=tools)
                blocks = list(getattr(response, "content", None) or [])
                tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
                if not tool_uses:
                    result = self._parse_response(_text_of(blocks))
                    result.tool_calls = executed
                    return result
                messages.append({"role": "assistant", "content": _assistant_blocks(blocks)})
                tool_results: list[dict[str, Any]] = []
                for use in tool_uses:
                    block, record = self._run_tool_use(toolset, use)
                    tool_results.append(block)
                    executed.append(record)
                messages.append({"role": "user", "content": tool_results})
            # Iterations exhausted — force a final, tool-free reply.
            final = self._create(client, system_prompt, messages, tools=None)
            result = self._parse_response(_text_of(list(getattr(final, "content", None) or [])))
            result.tool_calls = executed
            return result
        except Exception as exc:
            logger.error("Anthropic API error: %s", exc)
            return AssistantTurnResult(
                reply=f"I encountered an error: {type(exc).__name__}",
                error=str(exc)[:500],
                tool_calls=executed,
            )

    def _create(
        self, client: Any, system: str, messages: list[dict[str, Any]], *, tools: Any
    ) -> Any:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": 4096,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
        return client.messages.create(**kwargs)

    def _run_tool_use(
        self, toolset: AssistantToolDispatcher | None, use: Any
    ) -> tuple[dict[str, Any], AssistantToolCall]:
        """Execute one Claude tool_use block → (tool_result block, record)."""
        name = getattr(use, "name", "") or ""
        raw = getattr(use, "input", {})
        args = raw if isinstance(raw, dict) else {}
        ok = True
        if toolset is None:
            content = json.dumps({"error": "no tool dispatcher configured"})
            ok = False
        else:
            try:
                content = toolset.dispatch(name, args)
            except Exception as exc:
                content = json.dumps({"error": f"{type(exc).__name__}: {exc}"})
                ok = False
        if ok and content.lstrip().startswith('{"error"'):
            ok = False
        record = AssistantToolCall(name=name, arguments=args, result_summary=content[:500], ok=ok)
        block = {
            "type": "tool_result",
            "tool_use_id": getattr(use, "id", ""),
            "content": content,
        }
        return block, record

    def _build_system_prompt(self, request: AssistantTurnRequest) -> str:
        return build_assistant_system_prompt(request)

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


def _to_anthropic_tools(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map OpenAI-style function specs (from the toolset) to Anthropic tools."""
    tools: list[dict[str, Any]] = []
    for spec in specs:
        fn = spec.get("function", {}) if isinstance(spec, dict) else {}
        tools.append(
            {
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            }
        )
    return tools


def _text_of(blocks: list[Any]) -> str:
    """Concatenate the text from Claude content blocks."""
    return "".join(
        getattr(b, "text", "") or ""
        for b in blocks
        if getattr(b, "type", None) == "text" or hasattr(b, "text")
    )


def _assistant_blocks(blocks: list[Any]) -> list[dict[str, Any]]:
    """Reconstruct the assistant turn's content blocks for replay."""
    out: list[dict[str, Any]] = []
    for b in blocks:
        btype = getattr(b, "type", None)
        if btype == "tool_use":
            out.append(
                {
                    "type": "tool_use",
                    "id": getattr(b, "id", ""),
                    "name": getattr(b, "name", "") or "",
                    "input": getattr(b, "input", {}) or {},
                }
            )
        elif btype == "text" or hasattr(b, "text"):
            out.append({"type": "text", "text": getattr(b, "text", "") or ""})
    return out
