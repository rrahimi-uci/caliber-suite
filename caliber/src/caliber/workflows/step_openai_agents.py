"""One governed agent step through the OpenAI Agents SDK (B08.openai-agents, #728).

The legacy OpenAI Agents executor may run resolved tools. This module is the
governed AgentStepAdapter boundary: only read tools are callable, while every
protected tool is represented by a proposal-only callback.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final

from caliber.contracts.agent_step import (
    AgentStepRequest,
    AgentStepResult,
    ProposedAction,
    StepUsage,
)
from caliber.contracts.registry import ContractValidationError
from caliber.effects.inventory import EFFECT_CLASSES
from caliber.effects.records import ProposalError
from caliber.llm.models import reasoning_effort_for_model
from caliber.workflows import agent_step as _step
from caliber.workflows.agent_step import AgentStepInput, answer, failure, refusal
from caliber.workflows.ir import IRToolBinding

logger = logging.getLogger("caliber.workflows.step_openai_agents")

MODEL_REFUSED: Final = "model_refused"
PROVIDER_UNAVAILABLE: Final = "provider_unavailable"
_MAX_COUNT: Final = 2_147_483_647
_REQUIRED_ENVELOPE: Final = (
    "connector",
    "connector_version",
    "effect_class",
    "resource",
    "args",
)
_OPTIONAL_ENVELOPE: Final = ("expected_resource_version",)
_ENVELOPE = frozenset(_REQUIRED_ENVELOPE + _OPTIONAL_ENVELOPE)
_TIMEOUT_CLASSES: Final = frozenset({"APITimeoutError", "TimeoutException"})
_TIMEOUT_MODULES: Final = frozenset({"openai", "httpx", "httpcore"})
_DEFAULT_SYSTEM_PROMPT: Final = "You are a helpful workflow agent."


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class AgentsStepConfig:
    """Settings and the lazy SDK loader lent to one governed step."""

    default_model: str
    reasoning_effort: str
    api_key: str
    base_url: str | None = None
    parallel_tool_calls: bool = False
    prompt_cache_enabled: bool = False
    prompt_cache_retention: str | None = None
    clock: Callable[[], datetime] | None = None
    sdk_loader: Callable[[], tuple[Any, ...]] | None = None


class _Unreadable(Exception):  # noqa: N818 - fixed internal decoding marker
    """The SDK result or a model proposal cannot be read."""


@dataclass
class _State:
    read_calls: int = 0
    protected_raw: str | None = None
    tool_error: bool = False
    tool_limit: bool = False
    deadline: bool = False
    tokens: bool = False


@dataclass
class _Usage:
    prompt: int = 0
    completion: int = 0
    total: int = 0
    cached: int | None = None
    turns: int = 0
    tool_calls: int = 0

    def result(self) -> StepUsage:
        return StepUsage(
            prompt_tokens=self.prompt,
            completion_tokens=self.completion,
            total_tokens=self.total,
            turns=self.turns,
            tool_calls=self.tool_calls,
            cached_prompt_tokens=self.cached,
        )


def _is_timeout(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return True
    for module_name in _TIMEOUT_MODULES:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        for class_name in _TIMEOUT_CLASSES:
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


def _tool_parameters(binding: IRToolBinding) -> dict[str, Any]:
    schema = binding.input_schema
    if isinstance(schema, dict) and schema.get("type") == "object":
        return schema
    return {
        "type": "object",
        "properties": {"input": {"type": "string", "description": "Tool input."}},
        "required": [],
    }


def _proposal_schema(binding: IRToolBinding) -> dict[str, Any]:
    text = {"type": "string", "minLength": 1}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(_REQUIRED_ENVELOPE),
        "properties": {
            "connector": {**text, "description": "The connector that performs the effect."},
            "connector_version": {**text, "description": "Your claimed connector version."},
            "effect_class": {
                "type": "string",
                "enum": list(EFFECT_CLASSES),
                "description": "Your claimed effect class; the platform decides the real one.",
            },
            "resource": {**text, "description": "The resource the effect changes."},
            "args": _tool_parameters(binding),
            "expected_resource_version": {
                "type": ["string", "null"],
                "description": "The version expected by the proposal, or null.",
            },
        },
    }


def _proposal(raw: str) -> ProposedAction:
    try:
        envelope = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise _Unreadable from exc
    if (
        type(envelope) is not dict
        or not _ENVELOPE.issuperset(envelope)
        or not set(_REQUIRED_ENVELOPE).issubset(envelope)
    ):
        raise _Unreadable
    try:
        return ProposedAction(
            connector=envelope["connector"],
            connector_version=envelope["connector_version"],
            effect_class=envelope["effect_class"],
            resource=envelope["resource"],
            args=envelope["args"],
            expected_resource_version=envelope.get("expected_resource_version"),
        )
    except (ProposalError, ContractValidationError, TypeError, ValueError) as exc:
        raise _Unreadable from exc


def _count(value: object) -> int:
    if type(value) is not int or value < 0 or value > _MAX_COUNT:
        raise _Unreadable
    return value


def _usage(raw: Any, *, turns: int, tool_calls: int) -> _Usage:
    if raw is None:
        raise _Unreadable
    prompt = getattr(raw, "input_tokens", getattr(raw, "prompt_tokens", None))
    completion = getattr(raw, "output_tokens", getattr(raw, "completion_tokens", None))
    total = getattr(raw, "total_tokens", None)
    if prompt is None or completion is None:
        raise _Unreadable
    prompt_count = _count(prompt)
    completion_count = _count(completion)
    total_count = max(_count(0 if total is None else total), prompt_count + completion_count)
    details = getattr(raw, "input_tokens_details", None)
    cached = getattr(details, "cached_tokens", None) if details is not None else None
    if cached is None:
        cached = getattr(raw, "cached_input_tokens", None)
    cached_count = None if cached in (None, 0) else min(_count(cached), prompt_count)
    return _Usage(
        prompt=prompt_count,
        completion=completion_count,
        total=total_count,
        cached=cached_count,
        turns=turns,
        tool_calls=tool_calls,
    )


def _provider_usage(raw_responses: list[Any], *, tool_calls: int) -> _Usage:
    if not raw_responses:
        raise _Unreadable
    total = _Usage(tool_calls=tool_calls)
    for response in raw_responses:
        current = _usage(getattr(response, "usage", None), turns=1, tool_calls=tool_calls)
        for attr in ("prompt", "completion", "total", "cached"):
            current_value = getattr(current, attr)
            if attr == "cached" and current_value is None:
                continue
            value = (getattr(total, attr) or 0) + current_value
            if value > _MAX_COUNT:
                raise _Unreadable
            setattr(total, attr, value)
        total.turns += 1
    return total


def _estimated_usage(step_input: AgentStepInput, *, tool_calls: int) -> _Usage:
    prompt = len(step_input.input_text.split())
    return _Usage(prompt=prompt, completion=1, total=prompt + 1, turns=1, tool_calls=tool_calls)


def _output_text(result: Any, item_helpers: Any, raw_responses: list[Any]) -> str:
    from caliber.workflows import runtime as rt  # noqa: PLC0415

    text = rt._openai_agents_output_text(
        result=getattr(result, "final_output", None),
        raw_responses=raw_responses,
        item_helpers=item_helpers,
    )
    if not isinstance(text, str):
        raise _Unreadable
    return text


def _has_refusal(item_helpers: Any, raw_responses: list[Any]) -> bool:
    for response in raw_responses:
        for item in list(getattr(response, "output", None) or []):
            with contextlib.suppress(Exception):
                refusal = item_helpers.extract_refusal(item)
                if isinstance(refusal, str) and refusal.strip():
                    return True
    return False


@dataclass
class _Verdict:
    """Stands in for the SDK's ``ToolsToFinalOutputResult`` where the SDK is not importable (a stubbed one in tests)."""

    is_final_output: bool
    final_output: Any = None


def _verdict_class() -> Any:
    """The SDK's own ``ToolsToFinalOutputResult``: newer SDKs refuse a ``tool_use_behavior`` function that returns anything else."""
    try:
        from agents import ToolsToFinalOutputResult  # noqa: PLC0415
    except ImportError:
        return _Verdict
    return ToolsToFinalOutputResult


def _stop_between_model_calls(
    state: _State, request: AgentStepRequest, clock: Callable[[], datetime]
) -> Callable[[Any, list[Any]], Any]:
    """The SDK's hook between model calls (``tool_use_behavior`` as a function; the declared minimum SDK has it).

    After the tools of a turn have run it decides whether the run goes on, so a proposal, the tool-call limit, the token budget
    and the deadline end the run *before the model is asked again* instead of after another provider call. ``max_turns`` is the
    only other limit the SDK takes. A proposal ends the step first (the platform drops it when the usage passed a limit); the
    others are recorded for :func:`_run` to name.
    """
    limits = request.limits

    verdict = _verdict_class()

    def decide(context_wrapper: Any, tool_results: list[Any]) -> Any:
        del tool_results
        if state.protected_raw is not None:
            return verdict(is_final_output=True, final_output="proposal recorded")
        spent = getattr(getattr(context_wrapper, "usage", None), "total_tokens", 0)
        if type(spent) is int and spent >= limits.max_tokens:
            state.tokens = True
        if state.deadline or clock() >= limits.deadline_at:
            state.deadline = True
        if state.tool_limit or state.tokens or state.deadline:
            return verdict(is_final_output=True, final_output="limit reached")
        return verdict(is_final_output=False, final_output=None)

    return decide


def _build_tools(
    *,
    function_tool_cls: Any,
    agent: Any,
    step_input: AgentStepInput,
    state: _State,
    call_tool: Callable[[Callable[..., Any], dict[str, Any]], Any],
    max_tool_calls: int,
) -> list[Any]:
    """Create SDK tools whose protected callbacks only record a proposal payload."""

    async def read_callback(name: str, raw: str) -> str:
        try:
            arguments = json.loads(raw)
            if type(arguments) is not dict:
                raise ValueError
            tool = step_input.read_tools.get(name)
            if tool is None:
                raise _Unreadable
            if state.read_calls >= max_tool_calls:
                state.tool_limit = True
                return '{"_caliber":"tool-call limit reached"}'
            state.read_calls += 1
            return str(call_tool(tool, arguments))
        except _step.StepDeadlineReached:
            # The platform refused a tool called at or after the deadline: the step's stop, not a tool failure (which
            # would end it ``error``). ``_stop_between_model_calls`` ends the run before the model is asked again.
            state.deadline = True
            return '{"_caliber":"deadline reached"}'
        except Exception as exc:
            state.tool_error = True
            return json.dumps({"_error": type(exc).__name__})

    async def protected_callback(raw: str) -> str:
        if state.protected_raw is None:
            state.protected_raw = raw if isinstance(raw, str) else ""
        return '{"_caliber":"proposal recorded; no effect was executed"}'

    def make_read_callback(name: str) -> Callable[[Any, str], Any]:
        async def invoke(_ctx: Any, raw: str) -> str:
            return await read_callback(name, raw)

        return invoke

    def make_protected_callback() -> Callable[[Any, str], Any]:
        async def invoke(_ctx: Any, raw: str) -> str:
            return await protected_callback(raw)

        return invoke

    tools: list[Any] = []
    for binding in agent.tools:
        is_read = binding.local_name in step_input.read_tools and not binding.requires_approval
        callback = make_read_callback(binding.local_name) if is_read else make_protected_callback()

        tools.append(
            function_tool_cls(
                name=binding.local_name,
                description=(
                    (binding.registry_ref or binding.local_name)
                    if is_read
                    else (
                        f"{binding.registry_ref or binding.local_name}. Calling this only proposes "
                        "an action; it never runs the effect."
                    )
                ),
                params_json_schema=(
                    _tool_parameters(binding) if is_read else _proposal_schema(binding)
                ),
                on_invoke_tool=callback,
                strict_json_schema=True,
                needs_approval=False,
                timeout_seconds=binding.timeout_seconds,
                timeout_behavior="error_as_result",
            )
        )
    return tools


def _run(  # noqa: PLR0911, PLR0912 - bounded provider/limit state machine
    config: AgentsStepConfig,
    request: AgentStepRequest,
    step_input: AgentStepInput,
) -> AgentStepResult:
    from caliber.workflows import runtime as rt  # noqa: PLC0415

    if config.sdk_loader is None:
        raise RuntimeError("OpenAI Agents SDK loader is not configured")
    (
        agent_cls,
        function_tool_cls,
        item_helpers_cls,
        max_turns_exc_cls,
        model_settings_cls,
        provider_cls,
        run_config_cls,
        runner_cls,
        _handoff,
    ) = config.sdk_loader()
    agent = step_input.agent
    model = agent.model if agent.model and agent.model != "inherit" else config.default_model
    instructions = rt._agent_instruction_text(agent) or _DEFAULT_SYSTEM_PROMPT
    state = _State()

    def call_tool(tool: Callable[..., Any], arguments: dict[str, Any]) -> str:
        return rt._tool_result_text(
            rt._call_tool(tool, arguments, fallback_input=step_input.input_text)
        )

    tools = _build_tools(
        function_tool_cls=function_tool_cls,
        agent=agent,
        step_input=step_input,
        state=state,
        call_tool=call_tool,
        max_tool_calls=request.limits.max_tool_calls,
    )
    settings_kwargs: dict[str, Any] = {"include_usage": True}
    if not rt._is_reasoning_model(model):
        settings_kwargs["temperature"] = 0.2
    elif effort := reasoning_effort_for_model(model, config.reasoning_effort):
        from openai.types.shared.reasoning import Reasoning  # noqa: PLC0415

        settings_kwargs["reasoning"] = Reasoning(effort=effort)
    extra_args: dict[str, Any] = {"max_output_tokens": max(1, request.limits.max_tokens)}
    if config.parallel_tool_calls and tools:
        extra_args["parallel_tool_calls"] = True
    if config.prompt_cache_enabled and rt._supports_openai_prompt_cache(model):
        extra_args["prompt_cache_key"] = rt._openai_prompt_cache_key(
            api_surface="agents",
            model=model,
            agent=agent,
            instructions=instructions,
            tools=rt._openai_responses_tool_specs(agent),
            output_format=None,
        )
        if config.prompt_cache_retention:
            settings_kwargs["prompt_cache_retention"] = config.prompt_cache_retention
    settings_kwargs["extra_args"] = extra_args
    sdk_agent = agent_cls(
        name=agent.node_id or agent.name or "caliber.workflow.agent",
        instructions=instructions,
        model=model,
        tools=tools,
        handoffs=[],
        model_settings=model_settings_cls(**settings_kwargs),
        tool_use_behavior=_stop_between_model_calls(state, request, config.clock or _utc_now),
    )
    provider = provider_cls(api_key=config.api_key, base_url=config.base_url, use_responses=True)
    run_config = run_config_cls(
        model_provider=provider,
        tracing_disabled=True,
        workflow_name="CALIBER governed workflow agent step",
    )
    runner_input: list[dict[str, Any]] = [
        *[
            {"role": str(item.get("role") or "user"), "content": item.get("content") or ""}
            for item in step_input.history
        ],
        {"role": "user", "content": step_input.input_text},
    ]
    raw_responses: list[Any] = []
    try:
        clock = config.clock or _utc_now
        if request.limits.deadline_at <= clock():
            return answer(request, "deadline", usage_source="estimated")
        result = runner_cls.run_sync(
            sdk_agent,
            runner_input,
            max_turns=request.limits.max_turns,
            run_config=run_config,
        )
        raw_responses = list(getattr(result, "raw_responses", None) or [])
    except max_turns_exc_cls as exc:
        raw_responses = list(getattr(getattr(exc, "run_data", None), "raw_responses", None) or [])
        usage = (
            _provider_usage(raw_responses, tool_calls=state.read_calls)
            if raw_responses
            else _estimated_usage(step_input, tool_calls=state.read_calls)
        )
        reason = "max_tokens" if usage.total >= request.limits.max_tokens else "max_turns"
        return answer(
            request,
            reason,
            usage=usage.result(),
            usage_source="provider" if raw_responses else "estimated",
        )

    usage = (
        _provider_usage(raw_responses, tool_calls=state.read_calls)
        if raw_responses
        else _estimated_usage(step_input, tool_calls=state.read_calls)
    )
    if usage.total > request.limits.max_tokens or state.tokens:
        return answer(
            request,
            "max_tokens",
            usage=usage.result(),
            usage_source="provider" if raw_responses else "estimated",
        )
    if state.tool_limit or state.read_calls > request.limits.max_tool_calls:
        return answer(
            request,
            "max_tool_calls",
            usage=usage.result(),
            usage_source="provider" if raw_responses else "estimated",
        )
    if state.deadline:
        return answer(
            request,
            "deadline",
            usage=usage.result(),
            usage_source="provider" if raw_responses else "estimated",
        )
    if state.tool_error:
        raise _Unreadable
    if state.protected_raw is not None:
        action = _proposal(state.protected_raw)
        return answer(
            request,
            "proposed_action",
            usage=usage.result(),
            proposed_actions=(action,),
            usage_source="provider" if raw_responses else "estimated",
        )
    if raw_responses and _has_refusal(item_helpers_cls, raw_responses):
        return answer(
            request,
            "refused",
            usage=usage.result(),
            usage_source="provider",
            error_code=MODEL_REFUSED,
        )
    output = _output_text(result, item_helpers_cls, raw_responses)
    if not output:
        raise _Unreadable
    return answer(
        request,
        "completed",
        usage=usage.result(),
        usage_source="provider" if raw_responses else "estimated",
    )


def run_openai_agents_step(
    config: AgentsStepConfig,
    request: AgentStepRequest,
    step_input: AgentStepInput,
) -> AgentStepResult:
    """Run one bounded Agents SDK step; provider and decoding failures never escape."""
    try:
        return _run(config, request, step_input)
    except Exception as exc:
        logger.warning(
            "agent step %s: openai agents step failed (%s)",
            request.step_request_id,
            type(exc).__name__,
        )
        if isinstance(exc, RuntimeError) and ("SDK" in str(exc) or "openai-agents" in str(exc)):
            return refusal(request, PROVIDER_UNAVAILABLE)
        return failure(request, _failure_code(exc))


__all__ = [
    "MODEL_REFUSED",
    "PROVIDER_UNAVAILABLE",
    "AgentsStepConfig",
    "run_openai_agents_step",
]
