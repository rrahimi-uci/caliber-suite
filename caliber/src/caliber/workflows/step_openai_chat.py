"""One governed agent step through OpenAI chat completions (B08.openai-chat, #729).

:meth:`caliber.workflows.runtime.OpenAIChatWorkflowExecutor.run_step` calls :func:`run_openai_chat_step`. It runs the same
bounded tool-calling loop as the ungoverned ``run_agent`` (same message building, same reasoning-model and prompt-cache
handling, the same tool-call execution helper), but as a *proposing* adapter (see :mod:`caliber.workflows.agent_step`):

* **Read tools only run from** :attr:`~caliber.workflows.agent_step.AgentStepInput.read_tools`. That is the platform's own
  classification; the model's tool call never widens it. A tool the model calls that is *not* in it is never run: the model is
  asked to *propose* it, and the step stops with ``proposed_action``.
* **A proposal is the model's claim.** A protected tool is offered to the model with an envelope as its parameters
  (``connector``, ``connector_version``, ``effect_class``, ``resource``, ``args`` and optionally ``expected_resource_version``).
  The envelope is read strictly (an unknown key, such as ``state`` or ``receipt_id``, a wrong type or an invalid action is
  ``malformed_result``), so a model that says an effect happened is unreadable, never recorded. ``effect_class`` and
  ``connector_version`` are the model's claims: :attr:`ProposedAction.platform_bound` stays false until the platform binds
  them (B08.2). At most one action per step: if the model calls several protected tools in one response, the first is
  proposed, nothing else from that response runs (the step ends at a proposal) and the rest is dropped.
* **Limits come from the request.** ``max_turns`` counts provider responses (a retried request the provider rejected with no
  usage is not a turn), ``max_tool_calls`` counts read tools run, ``max_tokens`` is checked against the provider's reported
  total before each new provider call and again after each response (a step that spent *more* than its limit stops with
  ``max_tokens`` whatever the model wanted next), and the deadline is checked before each call. Each call is also bounded:
  the SDK's own retries are turned off and its timeout is the time left to the deadline (at most the configured provider
  timeout), and the remaining token budget is sent as ``max_completion_tokens``. A response the provider cut off
  (``finish_reason`` ``length``) is ``max_tokens``; the tool calls of a truncated response are never acted on.
* **Usage is the provider's.** The figures are summed from each response's usage object and reported with ``usage_source``
  ``provider``; a response with no usage object is estimated from word counts (the same rule ``run_agent`` uses) and the result
  says ``estimated``. A usage object that is not plain non-negative integers makes the step ``malformed_result``. The provider's
  usage is kept on every stop and on a failure after at least one response (a failure is reported as ``unknown`` source, so
  the reservation is kept conservatively).
* **Failures are typed results with fixed codes.** A provider timeout (the builtin, ``httpx.TimeoutException`` and
  ``openai.APITimeoutError``) is ``adapter_timeout``; any other provider exception is ``provider_error``; output that cannot be
  read is ``malformed_result``. Exception text, the provider's response, tool arguments and tool names are never placed in a
  result or a log line: only the exception's class name and the step id are logged. ``run_step`` never raises for these.

Not here: handoffs (``run_agent`` ignores them too), structured output (a step result has no output field), the token
reservation (B08.4) and binding a proposal to the protected-effect inventory (B08.2). The platform enforces the limits again
in ``run_agent_step`` whatever this adapter reports (B08.3).
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
from caliber.effects.inventory import EFFECT_CLASSES
from caliber.effects.records import ProposalError
from caliber.workflows.agent_step import (
    ADAPTER_TIMEOUT,
    MALFORMED_RESULT,
    PROVIDER_ERROR,
    AgentStepInput,
    answer,
    failure,
)
from caliber.workflows.ir import IRAgent, IRToolBinding

logger = logging.getLogger("caliber.workflows.step_openai_chat")

#: The model refused (``message.refusal``) or the provider filtered its answer: nothing was proposed.
MODEL_REFUSED: Final = "model_refused"
CONTENT_FILTERED: Final = "content_filtered"
#: The most any usage figure may be: the contract's own maximum (a larger one is unreadable, never clamped).
_MAX_COUNT: Final = 2147483647
_REQUIRED_ENVELOPE: Final = ("connector", "connector_version", "effect_class", "resource", "args")
_OPTIONAL_ENVELOPE: Final = ("expected_resource_version",)
_ENVELOPE: Final = frozenset(_REQUIRED_ENVELOPE + _OPTIONAL_ENVELOPE)
_TIMEOUT_TYPES: Final = (("openai", "APITimeoutError"), ("httpx", "TimeoutException"))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ChatStepConfig:
    """What the executor lends one step: its client and its settings, nothing else of the executor."""

    client: Any
    default_model: str
    reasoning_effort: str
    parallel_tool_calls: bool = False
    prompt_cache_enabled: bool = False
    prompt_cache_retention: str | None = None
    #: The clock the deadline is read from (an aware ``datetime``); ``None`` is the real UTC clock, tests inject a fake one.
    clock: Callable[[], Any] | None = None


class _Unreadable(Exception):  # noqa: N818 - the provider's output could not be read; never carries its text
    """The provider's response is not something this adapter can read (mapped to ``malformed_result``)."""


@dataclass
class _Tally:
    """What the step has used so far, from the provider's own usage objects."""

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
        """``provider`` when every figure was reported; ``estimated`` otherwise (and for a step that made no call)."""
        return "estimated" if self.estimated or self.turns == 0 else "provider"

    def add(
        self, prompt: int, completion: int, total: int, cached: int | None, *, estimated: bool
    ) -> None:
        figures = (self.prompt + prompt, self.completion + completion, self.total + total)
        if max(figures) > _MAX_COUNT or figures[0] + figures[1] > _MAX_COUNT:
            raise _Unreadable
        self.prompt, self.completion, self.total = figures
        if (
            cached is not None
        ):  # each turn's cached tokens are part of that turn's prompt, so the sum is part of the sum
            self.cached = (self.cached or 0) + cached
        self.turns += 1
        self.estimated = self.estimated or estimated


@dataclass(frozen=True)
class _ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class _Turn:
    """One provider response, read: nothing in it is used before it has been checked."""

    message: Any
    calls: tuple[_ToolCall, ...]
    raw_calls: list[Any]
    finish_reason: str | None
    refused: bool
    text: str
    usage: tuple[int, int, int, int | None] | None


@dataclass
class _Reasoning:
    """Some reasoning models take function tools or a reasoning effort, not both (see ``run_agent``)."""

    tools_need_no_reasoning: bool = False


@dataclass
class _Loop:
    config: ChatStepConfig
    request: AgentStepRequest
    step_input: AgentStepInput
    tally: _Tally = field(default_factory=_Tally)
    reasoning: _Reasoning = field(default_factory=_Reasoning)


# --------------------------------------------------------------------------------------------- failures


def _is_timeout(exc: Exception) -> bool:
    """A provider or transport timeout. The SDKs are optional extras, so each is looked up only if installed."""
    if isinstance(exc, TimeoutError | asyncio.TimeoutError):
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
        return ADAPTER_TIMEOUT
    if isinstance(exc, _Unreadable | ProposalError | ContractValidationError):
        return MALFORMED_RESULT
    return PROVIDER_ERROR


# ------------------------------------------------------------------------------------------ tool specs


def _proposal_spec(binding: IRToolBinding, parameters: dict[str, Any]) -> dict[str, Any]:
    """A protected tool as the model sees it: calling it *proposes* the action, whose arguments are ``args``."""
    return {
        "type": "function",
        "function": {
            "name": binding.local_name,
            "description": (
                f"{binding.registry_ref or binding.local_name}. Calling this does not run the tool: it proposes the "
                "action for approval."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": list(_REQUIRED_ENVELOPE),
                "properties": {
                    "connector": {"type": "string", "minLength": 1, "maxLength": 128},
                    "connector_version": {"type": "string", "minLength": 1, "maxLength": 64},
                    "effect_class": {"type": "string", "enum": list(EFFECT_CLASSES)},
                    "resource": {"type": "string", "minLength": 1, "maxLength": 512},
                    "args": parameters,
                    "expected_resource_version": {
                        "type": ["string", "null"],
                        "minLength": 1,
                        "maxLength": 256,
                    },
                },
            },
        },
    }


def _tool_specs(
    rt: Any, agent: IRAgent, read_tools: Mapping[str, Callable[..., Any]]
) -> list[dict[str, Any]]:
    """The agent's own tool specs for its read tools; every other tool is offered as a proposal, never as a callable."""
    plain = {spec["function"]["name"]: spec for spec in rt._openai_tool_specs(agent)}
    return [
        plain[binding.local_name]
        if binding.local_name in read_tools
        else _proposal_spec(binding, rt._tool_parameters(binding))
        for binding in agent.tools
    ]


def _proposal(arguments: str) -> ProposedAction:
    """The model's protected tool call, read strictly; whatever is wrong with it, it is ``malformed_result``."""
    try:
        envelope = json.loads(arguments)
    except (ValueError, RecursionError) as exc:
        raise _Unreadable from exc
    if (
        type(envelope) is not dict
        or not _ENVELOPE.issuperset(envelope)
        or not set(_REQUIRED_ENVELOPE).issubset(envelope)
    ):
        raise _Unreadable
    return ProposedAction(
        connector=envelope["connector"],
        connector_version=envelope["connector_version"],
        effect_class=envelope["effect_class"],
        resource=envelope["resource"],
        args=envelope["args"],
        expected_resource_version=envelope.get("expected_resource_version"),
    )


# -------------------------------------------------------------------------------------- the provider


def _create(loop: _Loop, kwargs: dict[str, Any], timeout: float) -> Any:
    """One provider call with the SDK's retries off and a timeout that cannot outlast the deadline."""
    client = loop.config.client
    with_options = getattr(client, "with_options", None)
    scoped = with_options(max_retries=0, timeout=timeout) if callable(with_options) else client
    return scoped.chat.completions.create(**kwargs)


def _call_provider(
    rt: Any,
    loop: _Loop,
    *,
    model: str,
    messages: list[dict[str, Any]],
    specs: list[dict[str, Any]],
    cache_key: str | None,
    max_completion_tokens: int,
    timeout: float,
) -> Any:
    config = loop.config
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_completion_tokens": max_completion_tokens,
    }
    if not rt._is_reasoning_model(model):
        kwargs["temperature"] = 0.2
    elif effort := rt.reasoning_effort_for_model(model, config.reasoning_effort):
        kwargs["reasoning_effort"] = (
            "none" if loop.reasoning.tools_need_no_reasoning and specs else effort
        )
    if specs:
        kwargs["tools"] = specs
        kwargs["tool_choice"] = "auto"
        if config.parallel_tool_calls:
            kwargs["parallel_tool_calls"] = True
    if cache_key:
        kwargs["prompt_cache_key"] = cache_key
        if config.prompt_cache_retention:
            kwargs["prompt_cache_retention"] = config.prompt_cache_retention
    try:
        return _create(loop, kwargs, timeout)
    except Exception as exc:
        if not (
            specs
            and kwargs.get("reasoning_effort") not in (None, "none")
            and rt._rejects_tools_with_reasoning(exc)
        ):
            raise
        # One bounded retry of a request the provider rejected outright (no usage): effort "none" from here on.
        loop.reasoning.tools_need_no_reasoning = True
        kwargs["reasoning_effort"] = "none"
        return _create(loop, kwargs, timeout)


def _count(value: object) -> int:
    if type(value) is not int or value < 0 or value > _MAX_COUNT:
        raise _Unreadable
    return value


def _cached_tokens(rt: Any, raw: Any) -> int | None:
    """The cached prompt tokens the provider reported (either of the SDK's spellings), or ``None`` when it did not."""
    for details_key in ("prompt_tokens_details", "input_tokens_details"):
        cached = rt._usage_field(rt._usage_field(raw, details_key), "cached_tokens")
        if cached is not None:
            return _count(cached)
    direct = rt._usage_field(raw, "cached_input_tokens")
    return None if direct is None else _count(direct)


def _read_usage(rt: Any, raw: Any) -> tuple[int, int, int, int | None] | None:
    """``(prompt, completion, total, cached)`` from the provider's usage object, or ``None`` when it gave none."""
    if raw is None:
        return None
    figures = {
        key: rt._usage_field(raw, key)
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    }
    if all(value is None for value in figures.values()):
        return None
    # A figure the provider left out is 0; one it gave must be a plain count (falsy garbage is not 0).
    prompt, completion, total = (
        _count(0 if value is None else value)
        for value in (
            figures["prompt_tokens"],
            figures["completion_tokens"],
            figures["total_tokens"],
        )
    )
    # Never less than the parts: the contract refuses a total that understates the charge.
    total = max(total, prompt + completion)
    cached = _cached_tokens(rt, raw)
    return prompt, completion, total, None if cached is None else min(cached, prompt)


def _read_turn(rt: Any, response: Any) -> _Turn:
    """Read one response; anything that is not the shape the SDK returns is :class:`_Unreadable`."""
    try:
        choices = response.choices
        if not isinstance(choices, list | tuple) or not choices:
            raise _Unreadable
        choice = choices[0]
        message = choice.message
        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason is not None and not isinstance(finish_reason, str):
            raise _Unreadable
        raw_calls = list(getattr(message, "tool_calls", None) or [])
        calls = []
        for raw in raw_calls:
            name = raw.function.name
            arguments = raw.function.arguments
            call_id = getattr(raw, "id", "")
            if not (isinstance(name, str) and name and isinstance(call_id, str)):
                raise _Unreadable
            if arguments is None:
                arguments = ""
            if not isinstance(arguments, str):
                raise _Unreadable
            calls.append(_ToolCall(id=call_id, name=name, arguments=arguments))
        content = getattr(message, "content", None)
        refusal = getattr(message, "refusal", None)
        usage = _read_usage(rt, getattr(response, "usage", None))
    except (AttributeError, TypeError, IndexError, KeyError, ValueError) as exc:
        raise _Unreadable from exc
    return _Turn(
        message=message,
        calls=tuple(calls),
        raw_calls=raw_calls,
        finish_reason=finish_reason,
        refused=isinstance(refusal, str) and bool(refusal.strip()),
        text=content if isinstance(content, str) else "",
        usage=usage,
    )


def _words(*texts: str) -> int:
    return sum(len(text.split()) for text in texts)


def _record_usage(loop: _Loop, turn: _Turn, messages: list[dict[str, Any]]) -> None:
    if turn.usage is not None:
        loop.tally.add(*turn.usage, estimated=False)
        return
    # No usage object (a gateway that omits it): the same word-count estimate run_agent uses, said to be an estimate.
    prompt = _words(*(m["content"] for m in messages if isinstance(m.get("content"), str)))
    completion = max(1, _words(turn.text, *(call.arguments for call in turn.calls)))
    loop.tally.add(prompt, completion, prompt + completion, None, estimated=True)


# ----------------------------------------------------------------------------------------- the step


def _stop(request: AgentStepRequest, reason: str, tally: _Tally) -> AgentStepResult:
    return answer(request, reason, usage=tally.usage(), usage_source=tally.source)


def _read_calls(
    rt: Any,
    loop: _Loop,
    turn: _Turn,
    messages: list[dict[str, Any]],
    bindings: dict[str, IRToolBinding],
) -> AgentStepResult | None:
    """Run the response's read tools through ``read_tools`` (all or none, within ``max_tool_calls``); ``None`` to go on."""
    tally, limits = loop.tally, loop.request.limits
    if tally.tool_calls + len(turn.calls) > limits.max_tool_calls:
        return _stop(loop.request, "max_tool_calls", tally)
    tools = dict(loop.step_input.read_tools)
    messages.append(rt._openai_assistant_message(turn.message, turn.raw_calls))
    for call in turn.calls:
        _, text = rt._execute_model_tool_call(
            name=call.name,
            arguments=rt._parse_tool_arguments(call.arguments),
            tool_callables=tools,
            bindings=bindings,
            fallback_input=loop.step_input.input_text,
        )
        tally.tool_calls += 1
        messages.append({"role": "tool", "tool_call_id": call.id, "content": text})
    return None


def _after_response(
    rt: Any,
    loop: _Loop,
    turn: _Turn,
    messages: list[dict[str, Any]],
    bindings: dict[str, IRToolBinding],
) -> AgentStepResult | None:
    """The step's answer to one response, or ``None`` when the model asked for read tools and the loop goes on."""
    request, tally = loop.request, loop.tally
    if turn.finish_reason == "length" or tally.total > request.limits.max_tokens:
        return _stop(request, "max_tokens", tally)
    if turn.finish_reason == "content_filter" or (turn.refused and not turn.calls):
        code = CONTENT_FILTERED if turn.finish_reason == "content_filter" else MODEL_REFUSED
        return answer(
            request, "refused", usage=tally.usage(), usage_source=tally.source, error_code=code
        )
    if not turn.calls:
        return _stop(request, "completed", tally)
    read_tools = loop.step_input.read_tools
    protected = next((call for call in turn.calls if call.name not in read_tools), None)
    if protected is not None:
        if protected.name not in {binding.local_name for binding in loop.step_input.agent.tools}:
            raise _Unreadable  # a tool the agent does not have: not read, not proposed
        return answer(
            request,
            "proposed_action",
            usage=tally.usage(),
            proposed_actions=(_proposal(protected.arguments),),
            usage_source=tally.source,
        )
    return _read_calls(rt, loop, turn, messages, bindings)


def _run(loop: _Loop) -> AgentStepResult:
    from caliber.workflows import runtime as rt  # noqa: PLC0415 - runtime imports this module lazily

    config, request, step_input, tally = loop.config, loop.request, loop.step_input, loop.tally
    agent, limits = step_input.agent, request.limits
    deadline = limits.deadline_at
    model = agent.model if agent.model and agent.model != "inherit" else config.default_model
    system_prompt = rt._agent_instruction_text(agent) or "You are a helpful workflow agent."
    specs = _tool_specs(rt, agent, step_input.read_tools)
    bindings = {b.local_name: b for b in agent.tools if b.local_name in step_input.read_tools}
    cache_key = (
        rt._openai_prompt_cache_key(
            api_surface="chat",
            model=model,
            agent=agent,
            instructions=system_prompt,
            tools=specs,
            output_format=None,
        )
        if config.prompt_cache_enabled and rt._supports_openai_prompt_cache(model)
        else None
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        *step_input.history,
        {"role": "user", "content": step_input.input_text},
    ]
    while True:
        if tally.turns >= limits.max_turns:
            return _stop(request, "max_turns", tally)
        if tally.total >= limits.max_tokens:
            return _stop(request, "max_tokens", tally)
        remaining = (deadline - (config.clock or _utc_now)()).total_seconds()
        if remaining <= 0:
            return _stop(request, "deadline", tally)
        response = _call_provider(
            rt,
            loop,
            model=model,
            messages=messages,
            specs=specs,
            cache_key=cache_key,
            max_completion_tokens=limits.max_tokens - tally.total,
            timeout=min(remaining, provider_request_timeout()),
        )
        turn = _read_turn(rt, response)
        _record_usage(loop, turn, messages)
        result = _after_response(rt, loop, turn, messages, bindings)
        if result is not None:
            return result


def run_openai_chat_step(
    config: ChatStepConfig, request: AgentStepRequest, step_input: AgentStepInput
) -> AgentStepResult:
    """Run one step through chat completions; a provider failure is a typed result, never an exception.

    ``BaseException`` (``KeyboardInterrupt``, ``asyncio.CancelledError``) is not caught: the provider call may have
    happened, and the caller keeps the step's reservation.
    """
    loop = _Loop(config=config, request=request, step_input=step_input)
    try:
        return _run(loop)
    except Exception as exc:
        # Only the class name and the step id: the exception's text and the provider's payload stay out of the log.
        logger.warning(
            "agent step %s: openai chat step failed (%s)",
            request.step_request_id,
            type(exc).__name__,
        )
        # What earlier responses used is kept (reported as ``unknown``, so the reservation stays conservative).
        usage = loop.tally.usage() if loop.tally.turns else None
        return failure(request, _failure_code(exc), usage=usage)


__all__ = ["CONTENT_FILTERED", "MODEL_REFUSED", "ChatStepConfig", "run_openai_chat_step"]
