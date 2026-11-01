"""One governed agent step through the OpenAI Responses API (B08.openai-responses, #730).

The Responses API is deliberately adapted at the same boundary as the chat and Anthropic
adapters: read tools are the only callables this module receives, protected tools are offered
as proposals, and every provider or decoding failure becomes a bounded typed result.  The
transport is kept in this module so ordinary tests can use a deterministic client stub and do
not need OpenAI credentials or network access.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Final

from caliber.config import provider_request_timeout
from caliber.contracts.agent_step import (
    AgentStepRequest,
    AgentStepResult,
    ProposedAction,
    StepUsage,
)
from caliber.contracts.registry import ContractValidationError
from caliber.effects.records import ProposalError
from caliber.workflows import agent_step as _step
from caliber.workflows.agent_step import (
    PROPOSAL_DESCRIPTION,
    AgentStepInput,
    answer,
    failure,
    proposal_from_call,
    proposal_parameters,
)
from caliber.workflows.ir import IRToolBinding

logger = logging.getLogger("caliber.workflows.step_openai_responses")

MODEL_REFUSED: Final = "model_refused"
_MAX_COUNT: Final = 2_147_483_647
_TIMEOUT_TYPES: Final = (("openai", "APITimeoutError"), ("httpx", "TimeoutException"))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ResponsesStepConfig:
    client: Any
    default_model: str
    reasoning_effort: str
    parallel_tool_calls: bool = False
    prompt_cache_enabled: bool = False
    prompt_cache_retention: str | None = None
    clock: Callable[[], datetime] | None = None


class _Unreadable(Exception):  # noqa: N818 - a fixed internal decode-failure marker
    """The provider response is not a supported Responses API result."""


@dataclass
class _Tally:
    prompt: int = 0
    completion: int = 0
    total: int = 0
    cached: int | None = None
    turns: int = 0
    tool_calls: int = 0
    estimated: bool = False

    def usage(self) -> StepUsage:
        return StepUsage(
            prompt_tokens=self.prompt,
            completion_tokens=self.completion,
            total_tokens=self.total,
            turns=self.turns,
            tool_calls=self.tool_calls,
            cached_prompt_tokens=self.cached,
        )

    @property
    def source(self) -> str:
        return "estimated" if self.estimated or self.turns == 0 else "provider"

    def add(
        self, prompt: int, completion: int, total: int, cached: int | None, *, estimated: bool
    ) -> None:
        values = (self.prompt + prompt, self.completion + completion, self.total + total)
        if max(values) > _MAX_COUNT or values[0] + values[1] > _MAX_COUNT:
            raise _Unreadable
        self.prompt, self.completion, self.total = values
        if cached is not None:
            self.cached = (self.cached or 0) + cached
        self.turns += 1
        self.estimated = self.estimated or estimated


@dataclass(frozen=True)
class _Call:
    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class _Turn:
    calls: tuple[_Call, ...]
    text: str
    refused: bool
    incomplete_reason: str | None
    usage: tuple[int, int, int, int | None] | None


@dataclass
class _Loop:
    config: ResponsesStepConfig
    request: AgentStepRequest
    step_input: AgentStepInput
    tally: _Tally = field(default_factory=_Tally)


def _value(item: Any, key: str, default: Any = None) -> Any:
    return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)


def _is_timeout(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return True
    for module_name, class_name in _TIMEOUT_TYPES:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        timeout_type = getattr(module, class_name, None)
        if isinstance(timeout_type, type) and isinstance(exc, timeout_type):
            return True
    return False


def _failure_code(exc: Exception) -> str:
    if _is_timeout(exc):
        return _step.ADAPTER_TIMEOUT
    if isinstance(exc, (_Unreadable, ProposalError, ContractValidationError)):
        return _step.MALFORMED_RESULT
    return _step.PROVIDER_ERROR


def _proposal_spec(binding: IRToolBinding, parameters: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "name": binding.local_name,
        "description": f"{binding.registry_ref or binding.local_name}. {PROPOSAL_DESCRIPTION}",
        "parameters": proposal_parameters(parameters),
    }


def _tool_specs(
    rt: Any, agent: Any, read_tools: Mapping[str, Callable[..., Any]]
) -> list[dict[str, Any]]:
    plain = {spec["name"]: spec for spec in rt._openai_responses_tool_specs(agent)}
    return [
        plain[binding.local_name]
        if binding.local_name in read_tools
        else _proposal_spec(binding, rt._tool_parameters(binding))
        for binding in agent.tools
    ]


def _proposal(tool: str, arguments: str) -> ProposedAction:
    """The model's call of the protected tool ``tool`` as a proposal that names it (no claims); unreadable otherwise."""
    try:
        return proposal_from_call(tool, json.loads(arguments))
    except (ValueError, RecursionError, TypeError) as exc:
        raise _Unreadable from exc


def _count(value: object) -> int:
    if type(value) is not int or value < 0 or value > _MAX_COUNT:
        raise _Unreadable
    return value


def _usage_field(raw: Any, key: str) -> Any:
    return _value(raw, key)


def _read_usage(raw: Any) -> tuple[int, int, int, int | None] | None:
    if raw is None:
        return None
    values = [_usage_field(raw, key) for key in ("input_tokens", "output_tokens", "total_tokens")]
    if all(value is None for value in values):
        return None
    prompt, completion, total = (_count(0 if value is None else value) for value in values)
    total = max(total, prompt + completion)
    details = _usage_field(raw, "input_tokens_details")
    cached = (
        _usage_field(details, "cached_tokens")
        if details is not None
        else _usage_field(raw, "cached_input_tokens")
    )
    return prompt, completion, total, None if cached is None else min(_count(cached), prompt)


def _read_response(response: Any) -> _Turn:  # noqa: PLR0912 - strict provider-shape decoder
    try:
        output = list(_value(response, "output", []) or [])
        calls: list[_Call] = []
        text_parts: list[str] = []
        refused = False
        for item in output:
            item_type = _value(item, "type")
            if item_type == "function_call":
                name = _value(item, "name")
                arguments = _value(item, "arguments", "")
                call_id = _value(item, "call_id", "")
                if not (
                    isinstance(name, str)
                    and name
                    and isinstance(arguments, str)
                    and isinstance(call_id, str)
                    and call_id
                ):
                    raise _Unreadable
                calls.append(_Call(call_id, name, arguments))
            elif item_type == "message":
                for content in list(_value(item, "content", []) or []):
                    content_type = _value(content, "type")
                    if content_type == "output_text":
                        value = _value(content, "text", "")
                        if not isinstance(value, str):
                            raise _Unreadable
                        text_parts.append(value)
                    elif content_type == "refusal":
                        refusal = _value(content, "refusal", "")
                        if not isinstance(refusal, str):
                            raise _Unreadable
                        refused = refused or bool(refusal.strip())
                    else:
                        raise _Unreadable
            elif item_type != "reasoning":
                raise _Unreadable
        direct = _value(response, "output_text")
        text = direct if isinstance(direct, str) and direct.strip() else "".join(text_parts).strip()
        incomplete = _value(_value(response, "incomplete_details"), "reason")
        if incomplete is not None and not isinstance(incomplete, str):
            raise _Unreadable
        usage = _read_usage(_value(response, "usage"))
    except (AttributeError, TypeError, IndexError, KeyError, ValueError) as exc:
        raise _Unreadable from exc
    return _Turn(tuple(calls), text, refused, incomplete, usage)


def _words(*texts: str) -> int:
    return sum(len(text.split()) for text in texts)


def _record_usage(loop: _Loop, turn: _Turn, inputs: list[dict[str, Any]]) -> None:
    if turn.usage is not None:
        loop.tally.add(*turn.usage, estimated=False)
        return
    prompt = _words(*(str(item.get("content", "")) for item in inputs))
    completion = max(1, _words(turn.text, *(call.arguments for call in turn.calls)))
    loop.tally.add(prompt, completion, prompt + completion, None, estimated=True)


def _stop(request: AgentStepRequest, reason: str, tally: _Tally) -> AgentStepResult:
    return answer(request, reason, usage=tally.usage(), usage_source=tally.source)


def _create(loop: _Loop, kwargs: dict[str, Any], timeout: float) -> Any:
    client = loop.config.client
    with_options = getattr(client, "with_options", None)
    scoped = with_options(max_retries=0, timeout=timeout) if callable(with_options) else client
    return scoped.responses.create(**kwargs)


def _call_provider(
    rt: Any,
    loop: _Loop,
    *,
    model: str,
    instructions: str,
    inputs: list[dict[str, Any]],
    previous_id: str | None,
    specs: list[dict[str, Any]],
    cache_key: str | None,
    max_output_tokens: int,
    timeout: float,
) -> Any:
    config = loop.config
    kwargs: dict[str, Any] = {
        "model": model,
        "instructions": instructions,
        "input": inputs,
        "max_output_tokens": max_output_tokens,
    }
    if previous_id:
        kwargs["previous_response_id"] = previous_id
    if not rt._is_reasoning_model(model):
        kwargs["temperature"] = 0.2
    elif effort := rt.reasoning_effort_for_model(model, config.reasoning_effort):
        kwargs["reasoning"] = {"effort": effort}
    if specs:
        kwargs["tools"] = specs
        kwargs["tool_choice"] = "auto"
        if config.parallel_tool_calls:
            kwargs["parallel_tool_calls"] = True
    if cache_key:
        kwargs["prompt_cache_key"] = cache_key
        if config.prompt_cache_retention:
            kwargs["prompt_cache_retention"] = config.prompt_cache_retention
    return _create(loop, kwargs, timeout)


def _read_calls(
    rt: Any, loop: _Loop, turn: _Turn, bindings: dict[str, IRToolBinding]
) -> list[dict[str, Any]] | AgentStepResult:
    if loop.tally.tool_calls + len(turn.calls) > loop.request.limits.max_tool_calls:
        return _stop(loop.request, "max_tool_calls", loop.tally)
    messages: list[dict[str, Any]] = []
    for call in turn.calls:
        args = rt._parse_tool_arguments(call.arguments)
        _, text = rt._execute_model_tool_call(
            name=call.name,
            arguments=args,
            tool_callables=dict(loop.step_input.read_tools),
            bindings=bindings,
            fallback_input=loop.step_input.input_text,
            error_detail=False,
        )
        loop.tally.tool_calls += 1
        messages.append({"type": "function_call_output", "call_id": call.call_id, "output": text})
    return messages


def _run(loop: _Loop) -> AgentStepResult:  # noqa: PLR0911 - bounded step state machine
    from caliber.workflows import runtime as rt  # noqa: PLC0415

    config, request, step_input, tally = loop.config, loop.request, loop.step_input, loop.tally
    agent, limits = step_input.agent, request.limits
    model = agent.model if agent.model and agent.model != "inherit" else config.default_model
    instructions = rt._agent_instruction_text(agent) or "You are a helpful workflow agent."
    specs = _tool_specs(rt, agent, step_input.read_tools)
    bindings = {
        binding.local_name: binding
        for binding in agent.tools
        if binding.local_name in step_input.read_tools
    }
    cache_key = (
        rt._openai_prompt_cache_key(
            api_surface="responses",
            model=model,
            agent=agent,
            instructions=instructions,
            tools=specs,
            output_format=None,
        )
        if config.prompt_cache_enabled and rt._supports_openai_prompt_cache(model)
        else None
    )
    inputs: list[dict[str, Any]] = [
        *[
            {"role": str(item.get("role") or "user"), "content": item.get("content") or ""}
            for item in step_input.history
        ],
        {"role": "user", "content": step_input.input_text},
    ]
    previous_id: str | None = None
    while True:
        if tally.turns >= limits.max_turns:
            return _stop(request, "max_turns", tally)
        if tally.total >= limits.max_tokens:
            return _stop(request, "max_tokens", tally)
        remaining = (limits.deadline_at - (config.clock or _utc_now)()).total_seconds()
        if remaining <= 0:
            return _stop(request, "deadline", tally)
        response = _call_provider(
            rt,
            loop,
            model=model,
            instructions=instructions,
            inputs=inputs,
            previous_id=previous_id,
            specs=specs,
            cache_key=cache_key,
            max_output_tokens=limits.max_tokens - tally.total,
            timeout=min(remaining, provider_request_timeout()),
        )
        turn = _read_response(response)
        _record_usage(loop, turn, inputs)
        if turn.incomplete_reason == "max_output_tokens" or tally.total > limits.max_tokens:
            return _stop(request, "max_tokens", tally)
        if turn.refused and not turn.calls:
            return answer(
                request,
                "refused",
                usage=tally.usage(),
                usage_source=tally.source,
                error_code=MODEL_REFUSED,
            )
        if not turn.calls:
            return _stop(request, "completed", tally)
        protected = next(
            (call for call in turn.calls if call.name not in step_input.read_tools), None
        )
        if protected is not None:
            if protected.name not in {binding.local_name for binding in agent.tools}:
                raise _Unreadable
            return answer(
                request,
                "proposed_action",
                usage=tally.usage(),
                proposed_actions=(_proposal(protected.name, protected.arguments),),
                usage_source=tally.source,
            )
        next_inputs = _read_calls(rt, loop, turn, bindings)
        if isinstance(next_inputs, AgentStepResult):
            return next_inputs
        inputs = next_inputs
        previous_id = str(_value(response, "id", "") or "")
        if not previous_id:
            raise _Unreadable


def run_openai_responses_step(
    config: ResponsesStepConfig, request: AgentStepRequest, step_input: AgentStepInput
) -> AgentStepResult:
    """Run one Responses step; provider failures never escape as provider payloads or exception text."""
    loop = _Loop(config=config, request=request, step_input=step_input)
    try:
        return _run(loop)
    except Exception as exc:
        logger.warning(
            "agent step %s: openai responses step failed (%s)",
            request.step_request_id,
            type(exc).__name__,
        )
        usage = loop.tally.usage() if loop.tally.turns else None
        return failure(request, _failure_code(exc), usage=usage)


__all__ = ["MODEL_REFUSED", "ResponsesStepConfig", "run_openai_responses_step"]
