"""Offline conformance and boundary tests for the OpenAI Agents step adapter."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from caliber.contracts.agent_step import AgentStepResult, parse_step_result
from caliber.testing.adapter_conformance import (
    MALFORMED_USAGE,
    AdapterUnderTest,
    Call,
    Fault,
    Garbage,
    ProviderProbe,
    ProviderUsage,
    Reply,
    Script,
    fault_exception,
)
from caliber.workflows import step_openai_agents as adapter
from caliber.workflows.agent_step import run_agent_step
from caliber.workflows.ir import IRToolBinding
from caliber.workflows.runtime import OpenAIAgentsWorkflowExecutor
from tests.agent_step_conformance import (
    PROTECTED_CLASS,
    PROTECTED_TOOL,
    READ_TOOL,
    SCENARIOS,
    SECRET_MARKER,
    FakeStepClock,
    Scenario,
    StepContext,
    make_context,
    run_scenario,
)

NOW = datetime(2026, 10, 26, 9, 0, 0, tzinfo=timezone.utc)


class _Item:
    def __init__(self, text: str = "", refusal: str = "") -> None:
        self.text = text
        self.refusal = refusal


class _Usage:
    def __init__(self, prompt: Any, completion: Any, total: Any, cached: Any = 0) -> None:
        self.input_tokens = prompt
        self.output_tokens = completion
        self.total_tokens = total
        self.input_tokens_details = SimpleNamespace(cached_tokens=cached)


class _Response:
    def __init__(
        self,
        *,
        text: str = "done",
        usage: tuple[Any, Any, Any, Any] = (100, 20, 120, 0),
    ) -> None:
        self.output = [_Item(text=text)]
        self.usage = _Usage(*usage)


class _FunctionTool:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


class _Agent:
    instances: list[_Agent] = []

    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)
        self.tools = list(kwargs.get("tools") or [])
        self.handoffs = list(kwargs.get("handoffs") or [])
        _Agent.instances.append(self)


class _ItemHelpers:
    @staticmethod
    def extract_text(item: Any) -> str | None:
        return getattr(item, "text", None)

    @staticmethod
    def extract_refusal(item: Any) -> str | None:
        return getattr(item, "refusal", None)

    @staticmethod
    def extract_last_content(item: Any) -> str:
        return getattr(item, "text", "") or getattr(item, "refusal", "")


class _MaxTurnsExceededError(Exception):
    def __init__(self, raw_responses: list[Any] | None = None) -> None:
        self.run_data = SimpleNamespace(raw_responses=raw_responses or [])


class _ModelSettings:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class _Provider:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class _RunConfig:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


class _Runner:
    handler: Any = None
    calls: list[dict[str, Any]] = []

    @classmethod
    def run_sync(cls, agent: Any, input: Any, **kwargs: Any) -> Any:
        cls.calls.append({"agent": agent, "input": input, **kwargs})
        return cls.handler(agent, input, **kwargs)


def sdk() -> tuple[Any, ...]:
    return (
        _Agent,
        _FunctionTool,
        _ItemHelpers,
        _MaxTurnsExceededError,
        _ModelSettings,
        _Provider,
        _RunConfig,
        _Runner,
        lambda agent, **kwargs: agent,
    )


def _response_for(ctx: StepContext, *, text: str = "done") -> _Response:
    usage = ctx.staged_usage
    return _Response(
        text=text,
        usage=(
            usage.prompt_tokens,
            usage.completion_tokens,
            usage.total_tokens,
            usage.cached_prompt_tokens or 0,
        ),
    )


def _envelope(ctx: StepContext, **changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "connector": "document-request",
        "connector_version": "1.0.0",
        "effect_class": PROTECTED_CLASS,
        "resource": "cases/CASE-1/document-requests",
        "args": ctx.staged_args,
    }
    value.update(changes)
    return value


def _invoke(tool: Any, payload: Any) -> str:
    return asyncio.run(tool.on_invoke_tool(None, json.dumps(payload)))


class _Driver:
    name = "openai_agents"
    unsupported = frozenset({Scenario.ANOTHER_STEPS_RESULT})

    @staticmethod
    def _executor(ctx: StepContext, scenario: Scenario) -> OpenAIAgentsWorkflowExecutor:
        def handler(agent: Any, _input: Any, **_kwargs: Any) -> Any:
            ctx.provider_called()
            if scenario is Scenario.PROVIDER_ERROR:
                raise RuntimeError(f"upstream said {SECRET_MARKER}")
            if scenario is Scenario.PROVIDER_TIMEOUT:
                raise httpx.ReadTimeout(SECRET_MARKER)
            if scenario is Scenario.UNREADABLE_OUTPUT:
                return SimpleNamespace(
                    final_output="done",
                    raw_responses=[_Response(usage=("bad", 20, 120))],
                    last_agent=agent,
                )
            if scenario is Scenario.CLAIMS_COMMIT:
                _invoke(
                    agent.tools[1],
                    {**_envelope(ctx), "state": "committed", "receipt_id": SECRET_MARKER},
                )
                return SimpleNamespace(
                    final_output="done", raw_responses=[_response_for(ctx)], last_agent=agent
                )
            if scenario is Scenario.BUDGET_EXHAUSTED:
                raise _MaxTurnsExceededError([_Response(usage=(900, 100, 1000))])
            if scenario is Scenario.READ_TOOL_CALL:
                _invoke(agent.tools[0], ctx.staged_read_args)
                return SimpleNamespace(
                    final_output="case is open",
                    raw_responses=[
                        _Response(usage=(70, 10, 80)),
                        _Response(usage=(80, 20, 100)),
                    ],
                    last_agent=agent,
                )
            if scenario is Scenario.PROPOSES_PROTECTED_TOOL:
                _invoke(agent.tools[1], _envelope(ctx))
            elif scenario is Scenario.MODEL_LIES_ABOUT_EFFECT_CLASS:
                _invoke(agent.tools[1], _envelope(ctx, effect_class="read"))
            return SimpleNamespace(
                final_output="done", raw_responses=[_response_for(ctx)], last_agent=agent
            )

        _Runner.handler = handler

        class StubExecutor(OpenAIAgentsWorkflowExecutor):
            @staticmethod
            def _sdk() -> tuple[Any, ...]:
                return sdk()

        return StubExecutor(
            api_key="unused",
            default_model="gpt-4o",
            step_clock=lambda: NOW,
        )

    def adapter_for(self, scenario: Scenario, ctx: StepContext) -> object:
        return self._executor(ctx, scenario)


#: The SDK owns the provider loop and takes ``max_turns`` and nothing else: the adapter cannot stop it between provider calls
#: for a proposal, the tool-call limit, the token budget or the deadline. The platform still holds the result to each limit.
AGENTS_UNBOUNDED_IN_LOOP = frozenset({"proposal", "tool_calls", "tokens", "deadline"})


def _agents_run(script: Script, probe: ProviderProbe) -> Any:
    """The SDK's own loop over a conformance script: one model turn per script item until the model answers.

    ``Runner.run_sync`` calls the model, runs the tools it asked for and calls it again; it stops for ``max_turns`` and for
    nothing else the adapter can set, so a limit the adapter cannot tell the SDK about (tokens, tool calls, the deadline) is not
    enforced between turns (see ``AGENTS_UNBOUNDED_IN_LOOP``).
    """
    items = list(script)

    def handler(agent: Any, _input: Any, **kwargs: Any) -> Any:
        raw_responses: list[Any] = []
        tools = {tool.name: tool for tool in agent.tools}
        while items:
            if len(raw_responses) >= kwargs["max_turns"]:
                raise _MaxTurnsExceededError(raw_responses)
            probe.provider_called()
            item = items.pop(0)
            if isinstance(item, Fault):
                raise (
                    httpx.ReadTimeout(item.text)
                    if item.kind == "timeout"
                    else fault_exception(item)
                )
            if isinstance(item, Garbage):
                return SimpleNamespace(
                    final_output=item.text,
                    raw_responses=[_Response(usage=("many", 20, 120))],
                    last_agent=agent,
                )
            if item.usage is None:
                response = _Response(text=item.text or "")
                response.usage = None
            elif item.usage is MALFORMED_USAGE:
                response = _Response(text=item.text or "", usage=("many", "some", "lots", 0))
            else:
                assert isinstance(item.usage, ProviderUsage)
                usage = item.usage
                response = _Response(
                    text=item.text or "",
                    usage=(usage.prompt, usage.completion, usage.total_tokens, 0),
                )
            raw_responses.append(response)
            if item.cut_off:
                raise _MaxTurnsExceededError(raw_responses)
            for call in item.calls:
                tool = tools.get(call.name)
                if tool is None:
                    raise RuntimeError(f"the SDK found no tool {call.name}")
                payload = (
                    call.arguments
                    if isinstance(call.arguments, str)
                    else json.dumps(call.arguments)
                )
                asyncio.run(tool.on_invoke_tool(None, payload))
            if not item.calls or not items:
                # A reply with no tool call is the model's answer. A script that ends on a tool call ends the run there.
                return SimpleNamespace(
                    final_output=item.text or "", raw_responses=raw_responses, last_agent=agent
                )
        raise RuntimeError("the script has no reply left")

    return handler


def agents_subject() -> AdapterUnderTest:
    """The conformance suite's subject for ``OpenAIAgentsWorkflowExecutor``: the real executor over a stubbed SDK."""

    def build(script: Script, probe: ProviderProbe) -> object:
        _Runner.handler = _agents_run(script, probe)

        class StubExecutor(OpenAIAgentsWorkflowExecutor):
            @staticmethod
            def _sdk() -> tuple[Any, ...]:
                return sdk()

        return StubExecutor(api_key="unused", default_model="gpt-4o", step_clock=probe.clock.now)

    return AdapterUnderTest(
        name="openai_agents", build=build, unbounded_in_loop=AGENTS_UNBOUNDED_IN_LOOP
    )


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_openai_agents_executor_conforms_to_shared_harness(scenario: Scenario) -> None:
    result, ctx = run_scenario(_Driver(), scenario)
    assert result.step_request_id == ctx.request.step_request_id


def test_agents_builds_read_and_proposal_tools_without_dispatching_protected_effects() -> None:
    ctx = make_context()
    _Driver._executor(ctx, Scenario.PROPOSES_PROTECTED_TOOL).run_step(ctx.request, ctx.step_input)
    tools = _Agent.instances[-1].tools
    assert {tool.name for tool in tools} == {READ_TOOL, PROTECTED_TOOL}
    assert tools[0].params_json_schema["type"] == "object"
    assert tools[1].params_json_schema["additionalProperties"] is False
    assert tools[1].needs_approval is False
    assert _Runner.calls[-1]["run_config"].tracing_disabled is True


def test_agents_preserves_cache_and_parallel_settings() -> None:
    ctx = make_context()
    executor = _Driver._executor(ctx, Scenario.COMPLETES)
    executor._parallel_tool_calls = True
    executor._prompt_cache_enabled = True
    executor._prompt_cache_retention = "24h"
    result = executor.run_step(ctx.request, ctx.step_input)
    assert result.stop_reason == "completed"
    settings = _Agent.instances[-1].model_settings.kwargs
    assert settings["extra_args"]["parallel_tool_calls"] is True
    assert settings["prompt_cache_retention"] == "24h"
    assert settings["extra_args"]["prompt_cache_key"].startswith("caliber:agents:")


def test_agents_honors_deadline_before_provider_call() -> None:
    ctx = make_context(
        limits={
            "max_turns": 8,
            "max_tool_calls": 4,
            "max_tokens": 20000,
            "deadline": "2026-10-26T08:59:00Z",
        }
    )
    executor = _Driver._executor(ctx, Scenario.COMPLETES)
    result = executor.run_step(ctx.request, ctx.step_input)
    assert result.stop_reason == "deadline"
    assert result.usage_source == "estimated"


def test_agents_stops_before_a_read_tool_exceeds_the_limit() -> None:
    ctx = make_context(
        limits={
            "max_turns": 8,
            "max_tool_calls": 1,
            "max_tokens": 20000,
            "deadline": "2026-10-26T09:10:00Z",
        }
    )

    def handler(agent: Any, _input: Any, **_kwargs: Any) -> Any:
        _invoke(agent.tools[0], ctx.staged_read_args)
        _invoke(agent.tools[0], ctx.staged_read_args)
        return SimpleNamespace(final_output="done", raw_responses=[_Response()], last_agent=agent)

    executor = _Driver._executor(ctx, Scenario.COMPLETES)
    _Runner.handler = handler
    result = executor.run_step(ctx.request, ctx.step_input)
    assert result.stop_reason == "max_tool_calls"
    assert ctx.read_calls == [ctx.staged_read_args]


def test_agents_maps_missing_sdk_and_timeout_without_payload() -> None:
    ctx = make_context()
    config = adapter.AgentsStepConfig(
        default_model="gpt-4o",
        reasoning_effort="medium",
        api_key="unused",
        sdk_loader=lambda: (_ for _ in ()).throw(
            RuntimeError("openai-agents is not installed: " + SECRET_MARKER)
        ),
    )
    result = adapter.run_openai_agents_step(config, ctx.request, ctx.step_input)
    assert result.stop_reason == "refused"
    assert result.error_code == adapter.PROVIDER_UNAVAILABLE
    assert SECRET_MARKER not in json.dumps(result.record())

    timeout_config = adapter.AgentsStepConfig(
        default_model="gpt-4o",
        reasoning_effort="medium",
        api_key="unused",
        sdk_loader=lambda: (_ for _ in ()).throw(httpx.ReadTimeout(SECRET_MARKER)),
    )
    result = adapter.run_openai_agents_step(timeout_config, ctx.request, ctx.step_input)
    assert result.error_code == "adapter_timeout"
    assert SECRET_MARKER not in json.dumps(result.record())


def test_agents_estimates_usage_when_sdk_omits_raw_responses() -> None:
    ctx = make_context()

    def handler(agent: Any, _input: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(final_output="done", raw_responses=[], last_agent=agent)

    executor = _Driver._executor(ctx, Scenario.COMPLETES)
    _Runner.handler = handler
    result = executor.run_step(ctx.request, ctx.step_input)
    assert result.stop_reason == "completed"
    assert result.usage_source == "estimated"
    assert result.usage.total_tokens > 0


def test_agents_rejects_empty_final_output_and_invalid_proposals() -> None:
    ctx = make_context()

    def empty_handler(agent: Any, _input: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(
            final_output="",
            raw_responses=[_Response(text="", usage=(100, 20, 120))],
            last_agent=agent,
        )

    executor = _Driver._executor(ctx, Scenario.COMPLETES)
    _Runner.handler = empty_handler
    result = executor.run_step(ctx.request, ctx.step_input)
    assert (result.stop_reason, result.error_code) == ("error", "malformed_result")

    def bad_handler(agent: Any, _input: Any, **_kwargs: Any) -> Any:
        _invoke(agent.tools[1], '{"connector":"missing"}')
        return SimpleNamespace(
            final_output="done", raw_responses=[_response_for(ctx)], last_agent=agent
        )

    executor = _Driver._executor(ctx, Scenario.PROPOSES_PROTECTED_TOOL)
    _Runner.handler = bad_handler
    result = executor.run_step(ctx.request, ctx.step_input)
    assert (result.stop_reason, result.error_code) == ("error", "malformed_result")


def test_agents_maps_a_provider_refusal_to_a_fixed_typed_result() -> None:
    ctx = make_context()

    def refusal_handler(agent: Any, _input: Any, **_kwargs: Any) -> Any:
        response = _Response(text="")
        response.output = [_Item(refusal=SECRET_MARKER)]
        return SimpleNamespace(final_output="", raw_responses=[response], last_agent=agent)

    executor = _Driver._executor(ctx, Scenario.COMPLETES)
    _Runner.handler = refusal_handler
    result = executor.run_step(ctx.request, ctx.step_input)
    assert (result.stop_reason, result.error_code) == ("refused", adapter.MODEL_REFUSED)
    assert SECRET_MARKER not in json.dumps(result.record())


def test_agents_reasoning_settings_and_provider_are_configured() -> None:
    ctx = make_context()
    executor = _Driver._executor(ctx, Scenario.COMPLETES)
    executor._default_model = "o1"
    executor._reasoning_effort = "medium"
    result = executor.run_step(ctx.request, ctx.step_input)
    assert result.stop_reason == "completed"
    settings = _Agent.instances[-1].model_settings.kwargs
    assert settings["reasoning"].effort == "medium"
    provider = _Runner.calls[-1]["run_config"].model_provider
    assert provider.kwargs["use_responses"] is True


def test_agents_adapter_result_is_plain_contract_result() -> None:
    ctx = make_context()
    result = run_agent_step(
        _Driver().adapter_for(Scenario.COMPLETES, ctx), ctx.request, ctx.step_input
    )
    assert type(result) is AgentStepResult
    assert parse_step_result(result.record(), ctx.request) == result


def test_agents_defensive_decoders_and_unconfigured_loader(  # noqa: PLR0915 - decoder branch matrix
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = IRToolBinding(
        local_name="read",
        registry_ref="read",
        version_constraint="*",
        requires_approval=False,
        side_effect_level="read",
        allow_in_preview=False,
        module_path=__name__,
        callable_name="read",
        input_schema={"type": "object", "properties": {"id": {"type": "string"}}},
    )
    assert adapter._tool_parameters(binding)["properties"]["id"]["type"] == "string"
    with pytest.raises(adapter._Unreadable):
        adapter._proposal("{")
    with pytest.raises(adapter._Unreadable):
        adapter._proposal(json.dumps({"connector": "only"}))
    with pytest.raises(adapter._Unreadable):
        adapter._proposal(
            json.dumps(
                {
                    "connector": 1,
                    "connector_version": "1",
                    "effect_class": PROTECTED_CLASS,
                    "resource": "r",
                    "args": {},
                }
            )
        )
    with pytest.raises(adapter._Unreadable):
        adapter._usage(None, turns=1, tool_calls=0)
    with pytest.raises(adapter._Unreadable):
        adapter._usage(SimpleNamespace(input_tokens=1), turns=1, tool_calls=0)
    with pytest.raises(adapter._Unreadable):
        adapter._usage(
            SimpleNamespace(
                input_tokens=1,
                output_tokens=1,
                total_tokens=2,
                cached_input_tokens="bad",
            ),
            turns=1,
            tool_calls=0,
        )
    with pytest.raises(adapter._Unreadable):
        adapter._provider_usage([], tool_calls=0)
    with pytest.raises(adapter._Unreadable):
        adapter._provider_usage(
            [_Response(usage=(2_147_483_647, 1, 2_147_483_648))],
            tool_calls=0,
        )
    with pytest.raises(adapter._Unreadable):
        adapter._provider_usage(
            [
                _Response(usage=(2_147_483_647, 0, 2_147_483_647)),
                _Response(usage=(1, 0, 1)),
            ],
            tool_calls=0,
        )
    assert adapter._is_timeout(TimeoutError("fixed"))
    monkeypatch.setattr(
        adapter.importlib,
        "import_module",
        lambda _name: (_ for _ in ()).throw(ImportError("optional SDK")),
    )
    assert not adapter._is_timeout(RuntimeError("not a timeout"))

    state = adapter._State()
    tool_context = make_context()
    tools = adapter._build_tools(
        function_tool_cls=_FunctionTool,
        agent=tool_context.step_input.agent,
        step_input=tool_context.step_input,
        state=state,
        call_tool=lambda _tool, _arguments: (_ for _ in ()).throw(RuntimeError("tool failed")),
        max_tool_calls=4,
    )
    assert json.loads(asyncio.run(tools[0].on_invoke_tool(None, "[]")))["_error"] == "ValueError"
    assert json.loads(asyncio.run(tools[0].on_invoke_tool(None, "{}")))["_error"] == "RuntimeError"
    asyncio.run(tools[1].on_invoke_tool(None, "{}"))
    asyncio.run(tools[1].on_invoke_tool(None, "{}"))
    assert state.protected_raw == "{}"
    ctx = make_context(
        limits={
            "max_turns": 8,
            "max_tool_calls": 4,
            "max_tokens": 10,
            "deadline": "2030-01-01T00:00:00Z",
        }
    )
    _Runner.handler = lambda agent, _input, **_kwargs: SimpleNamespace(
        final_output="done",
        raw_responses=[_Response(usage=(9, 3, 12))],
        last_agent=agent,
    )
    result = adapter.run_openai_agents_step(
        adapter.AgentsStepConfig(
            default_model="gpt-4o",
            reasoning_effort="medium",
            api_key="unused",
            sdk_loader=sdk,
        ),
        ctx.request,
        ctx.step_input,
    )
    assert result.stop_reason == "max_tokens"

    reasoning_executor = _Driver._executor(ctx, Scenario.COMPLETES)
    reasoning_executor._default_model = "o1"
    reasoning_executor._reasoning_effort = "none"
    reasoning_executor._prompt_cache_enabled = True
    _Runner.handler = lambda agent, _input, **_kwargs: SimpleNamespace(
        final_output="done",
        raw_responses=[_Response()],
        last_agent=agent,
    )
    assert reasoning_executor.run_step(ctx.request, ctx.step_input).stop_reason == "max_tokens"

    error_ctx = make_context()
    error_executor = _Driver._executor(error_ctx, Scenario.COMPLETES)

    def tool_error_handler(agent: Any, _input: Any, **_kwargs: Any) -> Any:
        asyncio.run(agent.tools[0].on_invoke_tool(None, "[]"))
        return SimpleNamespace(final_output="done", raw_responses=[_Response()], last_agent=agent)

    _Runner.handler = tool_error_handler
    assert (
        error_executor.run_step(error_ctx.request, error_ctx.step_input).error_code
        == "malformed_result"
    )

    unconfigured = adapter.run_openai_agents_step(
        adapter.AgentsStepConfig(
            default_model="gpt-4o",
            reasoning_effort="medium",
            api_key="unused",
        ),
        make_context(
            limits={
                "max_turns": 8,
                "max_tool_calls": 4,
                "max_tokens": 20000,
                "deadline": "2030-01-01T00:00:00Z",
            }
        ).request,
        ctx.step_input,
    )
    assert (unconfigured.stop_reason, unconfigured.error_code) == (
        "refused",
        adapter.PROVIDER_UNAVAILABLE,
    )

    original = __import__("caliber.workflows.runtime", fromlist=["_openai_agents_output_text"])
    monkeypatch.setattr(original, "_openai_agents_output_text", lambda **_kwargs: object())
    with pytest.raises(adapter._Unreadable):
        adapter._output_text(SimpleNamespace(final_output="x"), _ItemHelpers, [])


def test_a_read_tool_the_platform_refuses_for_the_deadline_ends_the_step_at_the_deadline() -> None:
    """Found by the conformance suite: the refusal was read as a tool failure, so the step ended ``error``, not ``deadline``."""
    fake = FakeStepClock()
    ctx = make_context()
    ctx.clock = fake.step_clock()
    ctx.on_provider_call = lambda: fake.advance(
        700.0
    )  # one model call outlasts the ten-minute deadline
    script = (
        Reply(
            text=None, calls=(Call(READ_TOOL, ctx.staged_read_args),), usage=ProviderUsage(70, 10)
        ),
        Reply(text="done", usage=ProviderUsage(10, 5)),
    )
    executor = agents_subject().build(script, ctx)
    result = run_agent_step(executor, ctx.request, ctx.step_input, clock=ctx.clock)
    assert (result.stop_reason, result.error_code, result.proposed_actions) == (
        "deadline",
        None,
        (),
    )
    assert ctx.read_calls == [], "the platform refused the tool; it did not run"
    assert result.usage_source == "provider" and result.usage.total_tokens >= 80, (
        "what was spent is kept"
    )
