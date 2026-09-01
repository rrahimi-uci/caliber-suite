"""OpenAI Agents SDK assistant engine.

Uses ``gpt-5.6-luna`` by default with high reasoning effort. The API key is read
from the environment variable pointed to by ``CaliberConfig.llm_api_key_env``
(typically ``OPENAI_API_KEY``).
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
from caliber.config import provider_request_timeout
from caliber.llm.models import (
    DEFAULT_OPENAI_MODEL,
    DEFAULT_OPENAI_REASONING_EFFORT,
    reasoning_effort_for_model,
)

if TYPE_CHECKING:
    from caliber.assistant.tools import AssistantToolDispatcher

logger = logging.getLogger(__name__)


class OpenAIAssistantEngine:
    """Production engine wrapping the OpenAI Agents SDK.

    Lazy-imports the ``openai`` and ``agents`` packages so the server
    can start without them installed (falling back to ``FakeAssistantEngine``).

    When a ``tool_dispatcher`` is supplied, ``run_turn`` runs a real
    tool-calling loop: the model can call the dispatcher's read-only registry
    tools (list skills/tools, get a skill) mid-turn to ground its reply, with
    results fed back until it produces a final answer. Without a dispatcher it
    is a single-shot completion (unchanged), so existing callers/tests are
    unaffected.
    """

    # Bounded so a model that keeps requesting tools can't loop forever. Set
    # high enough for a real run -> observe -> fix -> re-run loop within a turn.
    _MAX_TOOL_ITERATIONS = 8

    def __init__(
        self,
        *,
        model: str = DEFAULT_OPENAI_MODEL,
        reasoning: str = DEFAULT_OPENAI_REASONING_EFFORT,
        api_key: str | None = None,
        api_key_env: str = "OPENAI_API_KEY",
        tool_dispatcher: AssistantToolDispatcher | None = None,
    ) -> None:
        self._model = model
        self._reasoning = reasoning
        self._api_key = api_key or os.environ.get(api_key_env, "")
        self._tool_dispatcher = tool_dispatcher
        if not self._api_key:
            logger.warning(
                "OpenAI API key not found in environment variable %s. "
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
            from openai import OpenAI  # noqa: PLC0415
        except ImportError:
            return AssistantTurnResult(
                reply="OpenAI SDK is not installed. Please install 'openai' to use this engine.",
                error="openai package not installed",
            )

        client = OpenAI(api_key=self._api_key, timeout=provider_request_timeout())

        system_prompt = self._build_system_prompt(request)
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        for msg in request.history:
            messages.append({"role": msg.get("role", "user"), "content": msg.get("content", "")})
        messages.append({"role": "user", "content": request.user_message})

        # A per-turn toolset (context-bound, permissioned) takes precedence over
        # the constructor-time read-only dispatcher.
        dispatcher = toolset or self._tool_dispatcher
        tools = dispatcher.specs() if dispatcher is not None else None
        executed: list[AssistantToolCall] = []

        try:
            # Luna/high cannot combine function tools with Chat Completions.
            # OpenAI requires the Responses API for that combination. Explicit
            # non-reasoning model overrides retain the established chat path.
            if tools and reasoning_effort_for_model(self._model, self._reasoning):
                return self._run_responses_tool_loop(
                    client,
                    messages,
                    tools=tools,
                    dispatcher=dispatcher,
                    executed=executed,
                )
            # Tool-calling loop: keep going while the model requests tools, up to
            # the iteration cap. With no dispatcher, ``tools`` is None and this is
            # a single completion (the original single-shot behaviour).
            for _ in range(self._MAX_TOOL_ITERATIONS):
                message = self._create(client, messages, tools=tools)
                tool_calls = list(getattr(message, "tool_calls", None) or []) if tools else []
                if not tool_calls:
                    result = self._parse_response(message.content or "")
                    result.tool_calls = executed
                    return result
                messages.append(_assistant_tool_call_message(message))
                for call in tool_calls:
                    tool_msg, record = self._run_tool_call(dispatcher, call)
                    messages.append(tool_msg)
                    executed.append(record)
            # Iterations exhausted — force a final, tool-free reply.
            final = self._create(client, messages, tools=None)
            result = self._parse_response(final.content or "")
            result.tool_calls = executed
            return result
        except Exception as exc:
            logger.error("OpenAI API error: %s", exc)
            return AssistantTurnResult(
                reply=f"I encountered an error: {type(exc).__name__}",
                error=str(exc)[:500],
                tool_calls=executed,
            )

    def _run_responses_tool_loop(
        self,
        client: Any,
        input_items: list[Any],
        *,
        tools: list[dict[str, Any]],
        dispatcher: AssistantToolDispatcher | None,
        executed: list[AssistantToolCall],
    ) -> AssistantTurnResult:
        """Run a Luna/high tool loop through OpenAI's Responses API."""
        response_tools = [_responses_tool_spec(spec) for spec in tools]
        items = list(input_items)
        previous_response_id: str | None = None
        effort = reasoning_effort_for_model(self._model, self._reasoning)
        for _ in range(self._MAX_TOOL_ITERATIONS):
            request: dict[str, Any] = {
                "model": self._model,
                "input": items,
                "reasoning": {"effort": effort},
                "tools": response_tools,
            }
            if previous_response_id:
                request["previous_response_id"] = previous_response_id
            response = client.responses.create(**request)
            calls = [
                item
                for item in list(getattr(response, "output", None) or [])
                if getattr(item, "type", None) == "function_call"
            ]
            if not calls:
                result = self._parse_response(_responses_text(response))
                result.tool_calls = executed
                return result
            previous_response_id = getattr(response, "id", None)
            items = []
            for call in calls:
                tool_msg, record = self._run_tool_call(dispatcher, call)
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": getattr(call, "call_id", "") or getattr(call, "id", ""),
                        "output": tool_msg["content"],
                    }
                )
                executed.append(record)

        final_request: dict[str, Any] = {
            "model": self._model,
            "input": items,
            "reasoning": {"effort": effort},
        }
        if previous_response_id:
            final_request["previous_response_id"] = previous_response_id
        final = client.responses.create(**final_request)
        result = self._parse_response(_responses_text(final))
        result.tool_calls = executed
        return result

    def _create(self, client: Any, messages: list[dict[str, Any]], *, tools: Any) -> Any:
        """One chat-completions call; returns the response message."""
        kwargs: dict[str, Any] = {"model": self._model, "messages": messages}
        # Chat Completions takes a top-level ``reasoning_effort`` *string*; the
        # nested ``reasoning={"effort": ...}`` object is the Responses-API shape
        # and a 400 here. Only send a recognized effort so a misconfigured value
        # is omitted rather than rejected by the server.
        effort = reasoning_effort_for_model(self._model, self._reasoning)
        if effort is not None:
            kwargs["reasoning_effort"] = effort
        if tools:
            kwargs["tools"] = tools
        response = client.chat.completions.create(**kwargs)
        return response.choices[0].message

    def _run_tool_call(
        self, dispatcher: AssistantToolDispatcher | None, call: Any
    ) -> tuple[dict[str, Any], AssistantToolCall]:
        """Execute one model tool call → (``tool`` message, surfacing record)."""
        function = getattr(call, "function", None)
        name = getattr(function, "name", "") or getattr(call, "name", "") or ""
        raw_args = getattr(function, "arguments", "") or getattr(call, "arguments", "") or "{}"
        try:
            args = json.loads(raw_args)
        except (json.JSONDecodeError, TypeError):
            args = {}
        args = args if isinstance(args, dict) else {}
        ok = True
        if dispatcher is None:
            content = json.dumps({"error": "no tool dispatcher configured"})
            ok = False
        else:
            try:
                content = dispatcher.dispatch(name, args)
            except Exception as exc:
                content = json.dumps({"error": f"{type(exc).__name__}: {exc}"})
                ok = False
        if ok and content.lstrip().startswith('{"error"'):
            ok = False
        record = AssistantToolCall(
            name=name,
            arguments=args,
            result_summary=content[:500],
            ok=ok,
        )
        call_id = getattr(call, "id", "") or getattr(call, "call_id", "")
        tool_msg = {"role": "tool", "tool_call_id": call_id, "content": content}
        return tool_msg, record

    def _build_system_prompt(self, request: AssistantTurnRequest) -> str:
        return build_assistant_system_prompt(request)

    def _parse_response(self, content: str) -> AssistantTurnResult:
        # Try JSON first. Require a ``reply`` key so a model that returns plain
        # JSON *content* (e.g. a JSON sample the user asked for) isn't misread as
        # a structured turn envelope — matching the Anthropic/Ollama engines.
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


def _assistant_tool_call_message(message: Any) -> dict[str, Any]:
    """Echo the model's tool-call request back into the message list.

    OpenAI requires the assistant turn that requested tools to be replayed
    (with its ``tool_calls``) before the matching ``tool`` results. Built from
    attributes so it works with both real SDK message objects and test doubles.
    """
    tool_calls = []
    for call in getattr(message, "tool_calls", None) or []:
        fn = getattr(call, "function", None)
        tool_calls.append(
            {
                "id": getattr(call, "id", ""),
                "type": "function",
                "function": {
                    "name": getattr(fn, "name", "") or "",
                    "arguments": getattr(fn, "arguments", "") or "{}",
                },
            }
        )
    return {
        "role": "assistant",
        "content": getattr(message, "content", None),
        "tool_calls": tool_calls,
    }


def _responses_tool_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Convert a Chat Completions function tool to the Responses shape."""
    raw_function = spec.get("function")
    function: dict[str, Any] = raw_function if isinstance(raw_function, dict) else {}
    return {
        "type": "function",
        "name": function.get("name", ""),
        "description": function.get("description", ""),
        "parameters": function.get("parameters", {"type": "object", "properties": {}}),
    }


def _responses_text(response: Any) -> str:
    """Extract final text from real Responses objects and lightweight doubles."""
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str):
        return output_text
    chunks: list[str] = []
    for item in list(getattr(response, "output", None) or []):
        for part in list(getattr(item, "content", None) or []):
            text = getattr(part, "text", None)
            if isinstance(text, str):
                chunks.append(text)
    return "".join(chunks)
