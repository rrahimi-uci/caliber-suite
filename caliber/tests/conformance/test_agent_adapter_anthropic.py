"""AnthropicChatWorkflowExecutor as a governed step adapter (B08.anthropic, #727).

Every test runs the real executor over a stubbed Messages transport (``tests/conformance/anthropic_stub.py``): no network, no
SDK import, no sleep (the deadline is read from an injected clock). The shared conformance harness comes first; the rest is
what is specific to this provider: the request it sends, usage and cache accounting, each limit at its boundary, every way
the provider's answer can be unreadable, provider errors and timeouts, and that nothing the provider or the model said ever
reaches a result or a log line.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import types
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest

from caliber.config import provider_request_timeout
from caliber.contracts.agent_step import StepUsage, parse_step_result
from caliber.effects.inventory import EFFECT_CLASSES
from caliber.workflows.agent_step import (
    ADAPTER_TIMEOUT,
    AgentStepAdapter,
    AgentStepInput,
    AgentStepResult,
    run_agent_step,
)
from caliber.workflows.ir import IRAgent, IRToolBinding, NodeType, PromptRef
from caliber.workflows.runtime import AnthropicChatWorkflowExecutor
from caliber.workflows.step_adapters import anthropic_chat
from tests.agent_step_conformance import (
    PROTECTED_TOOL,
    READ_TOOL,
    SCENARIOS,
    SECRET_MARKER,
    Scenario,
    StepContext,
    make_context,
    run_scenario,
)
from tests.conformance.anthropic_stub import (
    BEFORE_DEADLINE,
    AnthropicDriver,
    APITimeoutError,
    Block,
    Client,
    Response,
    Usage,
    executor_with,
    fixed_clock,
    proposal_input,
    text_response,
    tool_response,
)

LIMITS = {
    "max_turns": 8,
    "max_tool_calls": 4,
    "max_tokens": 20000,
    "deadline": "2026-10-26T09:10:00Z",
}
DEADLINE = datetime.fromisoformat("2026-10-26T09:10:00+00:00")


# ------------------------------------------------------------------------------------------------ helpers


def ctx_with(**limits: Any) -> StepContext:
    return make_context(Scenario.COMPLETES, limits={**LIMITS, **limits})


def binding(
    name: str, *, side_effect_level: str = "read", approval: bool = False, schema: Any = None
) -> IRToolBinding:
    return IRToolBinding(
        local_name=name,
        registry_ref=name,
        version_constraint="*",
        requires_approval=approval,
        side_effect_level=side_effect_level,
        allow_in_preview=False,
        module_path="tests.agent_step_conformance",
        callable_name="canary_protected_tool",
        input_schema=schema,
    )


def agent_input(
    ctx: StepContext,
    *,
    tools: tuple[IRToolBinding, ...] | None = None,
    read_tools: dict[str, Callable[..., Any]] | None = None,
    model: str = "inherit",
    instructions: str | None = None,
    history: tuple[dict[str, str], ...] = (),
    text: str = "Ask the applicant for the missing pay statement",
) -> AgentStepInput:
    bindings = (
        list(tools)
        if tools is not None
        else [
            binding(READ_TOOL),
            binding(
                PROTECTED_TOOL,
                side_effect_level="external_action",
                approval=True,
                schema={"type": "object", "properties": {"document_type": {"type": "string"}}},
            ),
        ]
    )
    agent = IRAgent(
        node_id=ctx.request.node_id,
        node_type=NodeType.AGENT,
        name="Request document",
        model=model,
        instructions=PromptRef(kind="inline", inline_text=instructions) if instructions else None,
        tools=bindings,
    )
    read = {READ_TOOL: ctx.read_tool} if read_tools is None else read_tools
    return AgentStepInput(agent=agent, input_text=text, history=history, read_tools=read)


def step(
    script: list[Response | BaseException],
    *,
    ctx: StepContext | None = None,
    step_input: AgentStepInput | None = None,
    clock: Callable[[], datetime] | None = None,
    max_tokens: int = 4096,
) -> tuple[AgentStepResult, Client, StepContext]:
    """Run one step of the real executor against ``script`` through the platform's entry; the result is returned."""
    ctx = ctx or ctx_with()
    executor, client = executor_with(script, ctx=ctx, clock=clock, max_tokens=max_tokens)
    result = run_agent_step(executor, ctx.request, step_input or agent_input(ctx))
    assert parse_step_result(result.record(), ctx.request) == result
    return result, client, ctx


def used(result: AgentStepResult) -> tuple[int, int, int, int, int]:
    u = result.usage
    return (u.prompt_tokens, u.completion_tokens, u.total_tokens, u.turns, u.tool_calls)


def read_call(args: dict[str, Any] | None = None, usage: Usage | None = None) -> Response:
    return tool_response(READ_TOOL, args or {"case_id": "c-1"}, usage)


class Clock:
    """A clock a test moves: no sleeping, so a deadline is crossed exactly where the test says."""

    def __init__(self, now: datetime = BEFORE_DEADLINE) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


# ------------------------------------------------------------------------------------ the shared harness


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_the_anthropic_executor_conforms_to_the_shared_harness(scenario: Scenario) -> None:
    result, ctx = run_scenario(AnthropicDriver(), scenario)
    assert result.step_request_id == ctx.request.step_request_id


def test_the_executor_is_an_agent_step_adapter_that_needs_no_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "anthropic", None)
    executor, client = executor_with([text_response()])
    assert isinstance(executor, AgentStepAdapter)
    ctx = ctx_with()
    result = executor.run_step(ctx.request, ctx.step_input)
    assert (result.stop_reason, used(result)) == ("completed", (10, 5, 15, 1, 0))
    assert len(client.messages.calls) == 1


def test_without_an_injected_clock_the_deadline_is_read_from_the_wall_clock() -> None:
    client = Client([text_response()])
    executor = AnthropicChatWorkflowExecutor(api_key="unused", default_model="m", client=client)
    far = ctx_with(deadline="2099-01-01T00:00:00Z")
    assert run_agent_step(executor, far.request, far.step_input).stop_reason == "completed"
    past = ctx_with(deadline="2001-01-01T00:00:00Z")
    result = run_agent_step(executor, past.request, past.step_input)
    assert result.stop_reason == "deadline" and len(client.messages.calls) == 1


# ---------------------------------------------------------------------------------------- the request sent


def test_the_request_the_provider_gets() -> None:
    ctx = ctx_with()
    history = ({"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"})
    step_input = agent_input(
        ctx, model="claude-agent", instructions="Be brief.", history=history, text="go"
    )
    result, client, _ = step([text_response()], ctx=ctx, step_input=step_input)
    assert result.stop_reason == "completed"
    (call,) = client.messages.calls
    assert call["model"] == "claude-agent"
    assert call["system"] == "Be brief."
    assert call["messages"] == [*history, {"role": "user", "content": "go"}]
    assert call["max_tokens"] == 4096
    assert call["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert call["timeout"] == provider_request_timeout()
    # The SDK's own retries are off: a retry is the next attempt of the step, never a hidden second charge.
    assert client.options == [{"max_retries": 0}]


def test_an_inherited_model_and_no_instructions_use_the_executors_defaults() -> None:
    _, client, _ = step([text_response()])
    (call,) = client.messages.calls
    assert call["model"] == "claude-test"
    assert call["system"] == "You are a helpful workflow agent."


def test_a_read_tool_is_offered_as_itself_and_a_protected_tool_as_a_proposal() -> None:
    _, client, _ = step([text_response()])
    specs = {spec["name"]: spec for spec in client.messages.calls[0]["tools"]}
    assert set(specs) == {READ_TOOL, PROTECTED_TOOL}
    assert specs[READ_TOOL]["description"] == READ_TOOL
    assert specs[READ_TOOL]["input_schema"]["properties"] == {
        "input": {"type": "string", "description": "Tool input."}
    }
    protected = specs[PROTECTED_TOOL]
    assert "never run" in protected["description"]
    schema = protected["input_schema"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == [
        "connector",
        "connector_version",
        "effect_class",
        "resource",
        "args",
    ]
    assert schema["properties"]["effect_class"]["enum"] == list(EFFECT_CLASSES)
    assert schema["properties"]["args"] == {
        "type": "object",
        "properties": {"document_type": {"type": "string"}},
    }


def test_an_agent_without_tools_sends_no_tool_definitions() -> None:
    ctx = ctx_with()
    result, client, _ = step(
        [text_response()], ctx=ctx, step_input=agent_input(ctx, tools=(), read_tools={})
    )
    assert result.stop_reason == "completed"
    assert "tools" not in client.messages.calls[0] and "tool_choice" not in client.messages.calls[0]


def test_a_tool_the_platform_did_not_classify_read_is_a_proposal_even_if_the_binding_says_read() -> (
    None
):
    """The classification is the platform's (``read_tools``), not the binding's own side-effect label."""
    ctx = ctx_with()
    step_input = agent_input(ctx, read_tools={})
    result, client, _ = step(
        [tool_response(READ_TOOL, proposal_input(ctx))], ctx=ctx, step_input=step_input
    )
    assert result.stop_reason == "proposed_action"
    assert ctx.read_calls == []
    specs = {spec["name"]: spec for spec in client.messages.calls[0]["tools"]}
    assert "never run" in specs[READ_TOOL]["description"]


def test_a_read_tool_that_requires_approval_is_never_run() -> None:
    ctx = ctx_with()
    tools = (binding(READ_TOOL, approval=True),)
    step_input = agent_input(ctx, tools=tools)
    result, _, _ = step(
        [tool_response(READ_TOOL, proposal_input(ctx))], ctx=ctx, step_input=step_input
    )
    assert result.stop_reason == "proposed_action"
    assert ctx.read_calls == [] and ctx.protected_calls == []


def test_the_executor_builds_its_client_lazily_with_the_provider_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[dict[str, Any]] = []
    stub = Client([text_response()])

    def anthropic_client(**kwargs: Any) -> Client:
        built.append(kwargs)
        return stub

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=anthropic_client))
    executor = AnthropicChatWorkflowExecutor(
        api_key="unused", default_model="claude-test", step_clock=fixed_clock()
    )
    assert built == []
    ctx = ctx_with()
    result = run_agent_step(executor, ctx.request, ctx.step_input)
    assert result.stop_reason == "completed"
    assert built == [{"api_key": "unused", "timeout": provider_request_timeout()}]


# ----------------------------------------------------------------------------------------------- usage


def test_usage_is_the_providers_with_cache_tokens_in_the_prompt() -> None:
    result, _, _ = step([text_response(usage=Usage(100, 20, cache_creation=30, cache_read=50))])
    # prompt = fresh 100 + cache creation 30 + cache read 50; the cached part of it is the read.
    assert result.usage == StepUsage(180, 20, 200, 1, 0, cached_prompt_tokens=50)
    assert result.usage_source == "provider"


def test_usage_is_summed_over_turns_and_cache_reads_with_them() -> None:
    result, _, ctx = step(
        [
            read_call(usage=Usage(100, 10, cache_read=40)),
            text_response(usage=Usage(20, 5, cache_creation=7, cache_read=60)),
        ]
    )
    assert result.usage == StepUsage(227, 15, 242, 2, 1, cached_prompt_tokens=100)
    assert ctx.read_calls == [{"case_id": "c-1"}]


def test_usage_without_cache_fields_has_no_cached_figure_and_a_reported_zero_is_zero() -> None:
    plain, _, _ = step([text_response(usage=Usage(7, 3))])
    assert plain.usage.cached_prompt_tokens is None
    zero, _, _ = step([text_response(usage=Usage(7, 3, cache_read=0))])
    assert zero.usage.cached_prompt_tokens == 0


# ------------------------------------------------------------------------------------------ the loop


def test_a_read_tool_result_goes_back_to_the_model_and_the_step_completes() -> None:
    ctx = ctx_with()
    result, client, _ = step(
        [read_call({"case_id": "c-7"}, Usage(100, 20)), text_response(usage=Usage(50, 10))],
        ctx=ctx,
    )
    assert (result.stop_reason, used(result)) == ("completed", (150, 30, 180, 2, 1))
    assert ctx.read_calls == [{"case_id": "c-7"}]
    first, second = (call["messages"] for call in client.messages.calls)
    assert len(first) == 1  # each call was sent its own snapshot, not the list as it grew
    assistant, tool_result = second[-2:]
    assert assistant["role"] == "assistant"
    assert assistant["content"][0]["type"] == "tool_use"
    assert tool_result == {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "data"}],
    }


def test_a_read_tool_that_raises_tells_the_model_the_class_and_nothing_else() -> None:
    ctx = ctx_with()

    def failing(**arguments: Any) -> str:
        raise ConnectionError(f"database said {SECRET_MARKER}")

    step_input = agent_input(ctx, read_tools={READ_TOOL: failing})
    result, client, _ = step([read_call(), text_response()], ctx=ctx, step_input=step_input)
    assert (result.stop_reason, result.usage.tool_calls) == ("completed", 1)
    sent = client.messages.calls[1]["messages"][-1]["content"][0]
    assert sent["is_error"] is True
    assert sent["content"] == '{"_error": "ConnectionError"}'
    assert SECRET_MARKER not in repr(client.messages.calls)


def test_a_read_tool_result_that_is_not_text_is_sent_as_json_and_a_long_one_is_cut() -> None:
    ctx = ctx_with()
    structured = agent_input(ctx, read_tools={READ_TOOL: lambda **a: {"rows": [1, 2]}})
    _, client, _ = step([read_call(), text_response()], ctx=ctx, step_input=structured)
    assert client.messages.calls[1]["messages"][-1]["content"][0]["content"] == '{"rows": [1, 2]}'
    huge = agent_input(ctx, read_tools={READ_TOOL: lambda **a: "x" * 200_000})
    _, client, _ = step([read_call(), text_response()], ctx=ctx, step_input=huge)
    assert len(client.messages.calls[1]["messages"][-1]["content"][0]["content"]) == 65_536


def test_a_read_tool_whose_result_cannot_be_rendered_is_reported_by_class() -> None:
    class Unrenderable:
        def __str__(self) -> str:
            raise ValueError(SECRET_MARKER)

        __repr__ = __str__

    ctx = ctx_with()
    step_input = agent_input(ctx, read_tools={READ_TOOL: lambda **a: Unrenderable()})
    result, client, _ = step([read_call(), text_response()], ctx=ctx, step_input=step_input)
    assert result.stop_reason == "completed"
    assert client.messages.calls[1]["messages"][-1]["content"][0]["content"].startswith('{"_error"')


# ---------------------------------------------------------------------------- proposals and protected tools


def test_a_protected_call_is_proposed_from_the_models_input_and_never_run() -> None:
    ctx = ctx_with()
    result, client, _ = step(
        [tool_response(PROTECTED_TOOL, proposal_input(ctx), Usage(30, 7))], ctx=ctx
    )
    assert result.stop_reason == "proposed_action"
    (action,) = result.proposed_actions
    assert action == ctx.protected_action() and action.platform_bound is False
    assert action.expected_resource_version is None
    assert used(result) == (30, 7, 37, 1, 0)
    assert ctx.protected_calls == [] and ctx.read_calls == []
    assert (
        len(client.messages.calls) == 1
    )  # the step stops: no tool result is sent back for a proposal


def test_a_proposal_may_carry_the_version_of_the_resource_it_expects() -> None:
    ctx = ctx_with()
    result, _, _ = step(
        [tool_response(PROTECTED_TOOL, {**proposal_input(ctx), "expected_resource_version": "v7"})],
        ctx=ctx,
    )
    assert result.proposed_actions[0].expected_resource_version == "v7"


def test_two_protected_calls_in_one_reply_propose_only_the_first(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctx = ctx_with()
    first = proposal_input(ctx)
    second = {**proposal_input(ctx), "args": {"document_type": "second"}}
    reply = Response(
        [
            Block("tool_use", id_="a", name=PROTECTED_TOOL, input_=first),
            Block("tool_use", id_="b", name=PROTECTED_TOOL, input_=second),
        ],
        "tool_use",
        Usage(10, 5),
    )
    with caplog.at_level(logging.DEBUG):
        result, _, _ = step([reply], ctx=ctx)
    (action,) = result.proposed_actions
    assert dict(action.args) == ctx.staged_args
    assert "2 protected tool calls" in caplog.text


def test_a_protected_call_beside_a_read_call_runs_neither_and_proposes() -> None:
    ctx = ctx_with()
    reply = Response(
        [
            Block("tool_use", id_="a", name=READ_TOOL, input_={"case_id": "c-1"}),
            Block("tool_use", id_="b", name=PROTECTED_TOOL, input_=proposal_input(ctx)),
        ],
        "tool_use",
        Usage(10, 5),
    )
    result, _, _ = step([reply], ctx=ctx)
    assert result.stop_reason == "proposed_action"
    assert ctx.read_calls == [] and ctx.protected_calls == []
    assert result.usage.tool_calls == 0


# ---------------------------------------------------------------------------------------------- limits


def test_zero_turns_stops_before_any_provider_work() -> None:
    result, client, _ = step([text_response()], ctx=ctx_with(max_turns=0))
    assert (result.stop_reason, used(result)) == ("max_turns", (0, 0, 0, 0, 0))
    # Nothing was called, so zero is exact: not "unknown" (the provider may have run) and not the provider's report.
    assert result.usage_source == "estimated"
    assert client.messages.calls == []


def test_max_turns_is_a_ceiling_the_last_turn_may_reach_and_not_pass() -> None:
    exact, client, ctx = step([read_call(), text_response()], ctx=ctx_with(max_turns=2))
    assert (exact.stop_reason, exact.usage.turns) == ("completed", 2)
    assert ctx.read_calls == [{"case_id": "c-1"}]
    over, client, ctx = step(
        [read_call(), read_call({"case_id": "c-2"}), text_response()], ctx=ctx_with(max_turns=2)
    )
    # Turn two asked for another tool, and no turn is left to use its result: it is not run, and no third call is made.
    assert (over.stop_reason, over.usage.turns, over.usage.tool_calls) == ("max_turns", 2, 1)
    assert ctx.read_calls == [{"case_id": "c-1"}] and len(client.messages.calls) == 2
    assert over.usage_source == "provider"


def test_a_tool_call_needing_a_turn_that_is_not_left_is_not_run() -> None:
    result, client, ctx = step([read_call()], ctx=ctx_with(max_turns=1))
    assert (result.stop_reason, used(result)) == ("max_turns", (10, 5, 15, 1, 0))
    assert ctx.read_calls == [] and len(client.messages.calls) == 1


@pytest.mark.parametrize(
    ("limit", "stop", "ran", "calls"),
    [(0, "max_tool_calls", 0, 1), (1, "completed", 1, 2), (2, "completed", 1, 2)],
    ids=["zero", "exact", "headroom"],
)
def test_max_tool_calls_is_checked_before_each_read_tool_runs(
    limit: int, stop: str, ran: int, calls: int
) -> None:
    result, client, ctx = step([read_call(), text_response()], ctx=ctx_with(max_tool_calls=limit))
    assert (result.stop_reason, len(ctx.read_calls), len(client.messages.calls)) == (
        stop,
        ran,
        calls,
    )
    assert result.usage.tool_calls == ran


def test_max_tool_calls_one_over_runs_the_first_and_stops_at_the_second() -> None:
    reply = Response(
        [
            Block("tool_use", id_="a", name=READ_TOOL, input_={"case_id": "c-1"}),
            Block("tool_use", id_="b", name=READ_TOOL, input_={"case_id": "c-2"}),
        ],
        "tool_use",
        Usage(10, 5),
    )
    result, _, ctx = step([reply], ctx=ctx_with(max_tool_calls=1))
    assert result.stop_reason == "max_tool_calls"
    assert ctx.read_calls == [{"case_id": "c-1"}] and result.usage.tool_calls == 1


def test_a_zero_token_budget_stops_before_any_provider_work() -> None:
    result, client, _ = step([text_response()], ctx=ctx_with(max_tokens=0))
    assert (result.stop_reason, used(result)) == ("max_tokens", (0, 0, 0, 0, 0))
    assert client.messages.calls == []


def test_the_token_budget_is_a_ceiling_a_reply_may_reach_and_not_pass() -> None:
    exact, _, _ = step([text_response(usage=Usage(95, 5))], ctx=ctx_with(max_tokens=100))
    assert (exact.stop_reason, exact.usage.total_tokens) == ("completed", 100)
    over, _, _ = step([text_response(usage=Usage(96, 5))], ctx=ctx_with(max_tokens=100))
    # The reply finished, but the provider's own figure is over the budget: that is the stop, and the usage is kept.
    assert (over.stop_reason, used(over)) == ("max_tokens", (96, 5, 101, 1, 0))
    assert over.usage_source == "provider"


def test_a_budget_spent_exactly_by_a_tool_turn_leaves_no_turn_to_use_the_tools_result() -> None:
    result, client, ctx = step([read_call(usage=Usage(60, 40))], ctx=ctx_with(max_tokens=100))
    assert (result.stop_reason, result.usage.total_tokens) == ("max_tokens", 100)
    assert ctx.read_calls == [] and len(client.messages.calls) == 1


def test_a_call_may_produce_no_more_than_the_tokens_left() -> None:
    _, client, _ = step(
        [read_call(usage=Usage(300, 100)), text_response()],
        ctx=ctx_with(max_tokens=1000),
        max_tokens=800,
    )
    assert [call["max_tokens"] for call in client.messages.calls] == [800, 600]


def test_a_reply_the_provider_cut_off_is_max_tokens_and_a_half_finished_tool_call_is_not_run() -> (
    None
):
    cut = Response(
        [Block("tool_use", id_="a", name=READ_TOOL, input_={"case_id": "c-1"})],
        "max_tokens",
        Usage(80, 20),
    )
    result, client, ctx = step([cut])
    assert (result.stop_reason, used(result)) == ("max_tokens", (80, 20, 100, 1, 0))
    assert ctx.read_calls == [] and len(client.messages.calls) == 1


def test_an_expired_deadline_stops_before_any_provider_work_and_builds_no_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        sys.modules, "anthropic", None
    )  # building a client would fail: it must not be tried
    executor = AnthropicChatWorkflowExecutor(
        api_key="unused", default_model="m", step_clock=Clock(DEADLINE)
    )
    ctx = ctx_with()
    result = run_agent_step(executor, ctx.request, ctx.step_input)
    assert (result.stop_reason, used(result)) == ("deadline", (0, 0, 0, 0, 0))
    assert result.error_code is None


def test_the_deadline_is_the_last_instant_not_a_moment_after() -> None:
    clock = Clock(DEADLINE - timedelta(microseconds=1))
    result, client, _ = step([text_response()], clock=clock)
    assert result.stop_reason == "completed"
    assert 0 < client.messages.calls[0]["timeout"] < 0.001 + 1e-6


def test_a_provider_call_is_bounded_by_the_time_left_to_the_deadline() -> None:
    clock = Clock(DEADLINE - timedelta(seconds=2))
    _, client, _ = step([text_response()], clock=clock)
    assert client.messages.calls[0]["timeout"] == pytest.approx(2.0)


def test_a_deadline_that_passes_during_a_call_runs_no_tool_for_it() -> None:
    clock = Clock()
    ctx = ctx_with()
    executor, client = executor_with([read_call(), text_response()], ctx=ctx, clock=clock)
    client.messages.on_call = lambda: setattr(clock, "now", DEADLINE + timedelta(seconds=1))
    result = run_agent_step(executor, ctx.request, ctx.step_input)
    assert (result.stop_reason, used(result)) == ("deadline", (10, 5, 15, 1, 0))
    assert ctx.read_calls == [] and len(client.messages.calls) == 1


def test_a_deadline_that_passes_during_a_tool_stops_the_next_turn() -> None:
    clock = Clock()
    ctx = ctx_with()

    def slow(**arguments: Any) -> str:
        clock.now = DEADLINE + timedelta(seconds=1)
        return "late"

    step_input = agent_input(ctx, read_tools={READ_TOOL: slow})
    result, client, _ = step(
        [read_call(), text_response()], ctx=ctx, step_input=step_input, clock=clock
    )
    assert (result.stop_reason, used(result)) == ("deadline", (10, 5, 15, 1, 1))
    assert len(client.messages.calls) == 1


def test_a_deadline_that_passes_between_two_read_calls_of_one_reply_stops_at_the_second() -> None:
    clock = Clock()
    ctx = ctx_with()

    def slow(**arguments: Any) -> str:
        clock.now = DEADLINE
        return "late"

    reply = Response(
        [
            Block("tool_use", id_="a", name=READ_TOOL, input_={"case_id": "c-1"}),
            Block("tool_use", id_="b", name=READ_TOOL, input_={"case_id": "c-2"}),
        ],
        "tool_use",
        Usage(10, 5),
    )
    step_input = agent_input(ctx, read_tools={READ_TOOL: slow})
    result, _, _ = step([reply], ctx=ctx, step_input=step_input, clock=clock)
    assert (result.stop_reason, result.usage.tool_calls) == ("deadline", 1)


# ------------------------------------------------------------------------------- refusals and failures


def test_a_model_refusal_is_a_refused_result_that_keeps_the_usage() -> None:
    result, _, _ = step([Response([Block("text", text=SECRET_MARKER)], "refusal", Usage(12, 3))])
    assert (result.stop_reason, result.error_code) == ("refused", anthropic_chat.MODEL_REFUSED)
    assert used(result) == (12, 3, 15, 1, 0) and result.usage_source == "provider"
    assert result.proposed_actions == ()


def test_a_client_that_cannot_be_built_is_refused_and_nothing_was_called(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setitem(sys.modules, "anthropic", None)
    executor = AnthropicChatWorkflowExecutor(
        api_key="unused", default_model="m", step_clock=fixed_clock()
    )
    ctx = ctx_with()
    with caplog.at_level(logging.DEBUG):
        result = run_agent_step(executor, ctx.request, ctx.step_input)
    assert (result.stop_reason, result.error_code) == (
        "refused",
        anthropic_chat.PROVIDER_UNAVAILABLE,
    )
    assert used(result) == (0, 0, 0, 0, 0) and result.usage_source == "unknown"
    assert "could not be built (RuntimeError)" in caplog.text
    assert "Install the Anthropic SDK" not in caplog.text


class RateLimitError(Exception):
    """Named like an SDK error that is not a timeout."""


RateLimitError.__module__ = "anthropic"


class LookalikeTimeoutError(Exception):
    """A ``APITimeoutError`` of some other package is not the SDK's: it is an ordinary provider error."""


LookalikeTimeoutError.__name__ = "APITimeoutError"
LookalikeTimeoutError.__module__ = "somebody_elses"


@pytest.mark.parametrize(
    ("raised", "code"),
    [
        (TimeoutError(SECRET_MARKER), "adapter_timeout"),
        (asyncio.TimeoutError(SECRET_MARKER), "adapter_timeout"),
        (APITimeoutError(SECRET_MARKER), "adapter_timeout"),
        (httpx.ReadTimeout(SECRET_MARKER), "adapter_timeout"),
        (httpx.ConnectTimeout(SECRET_MARKER), "adapter_timeout"),
        (RateLimitError(SECRET_MARKER), "provider_error"),
        (LookalikeTimeoutError(SECRET_MARKER), "provider_error"),
        (httpx.ConnectError(SECRET_MARKER), "provider_error"),
        (ValueError(SECRET_MARKER), "provider_error"),
    ],
    ids=[
        "builtin_timeout",
        "asyncio_timeout",
        "sdk_timeout",
        "httpx_read_timeout",
        "httpx_connect_timeout",
        "sdk_rate_limit",
        "foreign_lookalike",
        "httpx_connect_error",
        "value_error",
    ],
)
def test_a_provider_exception_is_a_bounded_result_with_a_code_for_its_kind(
    raised: Exception, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        result, _, ctx = step([raised])
    assert (result.stop_reason, result.error_code) == ("error", code)
    assert result.proposed_actions == () and used(result) == (0, 0, 0, 0, 0)
    assert result.usage_source == "unknown"
    assert SECRET_MARKER not in caplog.text and SECRET_MARKER not in str(result.record())
    assert type(raised).__name__ in caplog.text  # the class is logged, never the message
    assert ADAPTER_TIMEOUT == "adapter_timeout"


def test_a_failure_after_a_turn_keeps_the_usage_of_the_turns_that_returned() -> None:
    result, client, ctx = step([read_call(usage=Usage(100, 20, cache_read=10)), RuntimeError("x")])
    assert result.error_code == "provider_error" and result.usage_source == "unknown"
    assert result.usage == StepUsage(110, 20, 130, 1, 1, cached_prompt_tokens=10)
    assert ctx.read_calls == [{"case_id": "c-1"}]


@pytest.mark.parametrize(
    "interrupt", [KeyboardInterrupt(), SystemExit(1), asyncio.CancelledError()]
)
def test_an_interrupt_is_not_swallowed(interrupt: BaseException) -> None:
    ctx = ctx_with()
    executor, _ = executor_with([interrupt], ctx=ctx)
    with pytest.raises(type(interrupt)):
        run_agent_step(executor, ctx.request, ctx.step_input)


# ----------------------------------------------------------------------------------- unreadable output


def planted_proposal(ctx: StepContext, **changes: Any) -> Response:
    body = {**proposal_input(ctx), **changes}
    for name in [name for name, value in body.items() if value is ...]:
        del body[name]
    return tool_response(PROTECTED_TOOL, body)


def reply_with(*blocks: Block, stop: Any = "tool_use", usage: Any = None) -> Response:
    return Response(list(blocks), stop, Usage(10, 5) if usage is None else usage)


def tool(name: str = READ_TOOL, id_: Any = "t1", input_: Any = None) -> Block:
    return Block(
        "tool_use", id_=id_, name=name, input_={"case_id": "c-1"} if input_ is None else input_
    )


class NoType:
    """A content block with no ``type``."""


CTX = ctx_with()
UNREADABLE: dict[str, Callable[[StepContext], Response]] = {
    "stop_reason_unknown": lambda c: text_response(stop="surprise"),
    "stop_reason_missing": lambda c: Response([Block("text")], None, Usage(10, 5)),
    "stop_reason_not_text": lambda c: Response([Block("text")], 7, Usage(10, 5)),
    "pause_turn": lambda c: text_response(stop="pause_turn"),
    "content_text": lambda c: Response(SECRET_MARKER, "end_turn", Usage(10, 5)),
    "content_none": lambda c: Response(None, "end_turn", Usage(10, 5)),
    "content_mapping": lambda c: Response({"text": SECRET_MARKER}, "end_turn", Usage(10, 5)),
    "block_without_type": lambda c: Response([NoType()], "end_turn", Usage(10, 5)),
    "block_type_not_text": lambda c: Response([Block(7)], "end_turn", Usage(10, 5)),  # type: ignore[arg-type]
    "tool_use_empty_id": lambda c: reply_with(tool(id_="")),
    "tool_use_id_not_text": lambda c: reply_with(tool(id_=5)),
    "tool_use_empty_name": lambda c: reply_with(tool(name="")),
    "tool_use_name_not_text": lambda c: reply_with(tool(name=5)),  # type: ignore[arg-type]
    "tool_use_input_text": lambda c: reply_with(tool(input_=SECRET_MARKER)),
    "tool_use_input_list": lambda c: reply_with(tool(input_=[SECRET_MARKER])),
    "tool_stop_without_a_call": lambda c: text_response(SECRET_MARKER, stop="tool_use"),
    "final_stop_with_a_call": lambda c: reply_with(tool(), stop="end_turn"),
    "stop_sequence_with_a_call": lambda c: reply_with(tool(), stop="stop_sequence"),
    "unknown_tool": lambda c: reply_with(tool(name=f"unknown-{SECRET_MARKER}")),
    "proposal_without_args": lambda c: planted_proposal(c, args=...),
    "proposal_without_connector": lambda c: planted_proposal(c, connector=...),
    "proposal_with_state": lambda c: planted_proposal(c, state="committed"),
    "proposal_with_receipt": lambda c: planted_proposal(c, receipt_id=SECRET_MARKER),
    "proposal_with_approval": lambda c: planted_proposal(c, approval=SECRET_MARKER),
    "proposal_with_another_step": lambda c: planted_proposal(c, occurrence_id="occ-other"),
    "proposal_args_list": lambda c: planted_proposal(c, args=[SECRET_MARKER]),
    "proposal_args_not_json": lambda c: planted_proposal(c, args={"x": float("nan")}),
    "proposal_bad_class": lambda c: planted_proposal(c, effect_class=f"write-{SECRET_MARKER}"),
    "proposal_class_not_text": lambda c: planted_proposal(c, effect_class=["read"]),
    "proposal_blank_connector": lambda c: planted_proposal(c, connector="  "),
    "proposal_control_char": lambda c: planted_proposal(c, resource=f"a\nb{SECRET_MARKER}"),
    "proposal_resource_too_long": lambda c: planted_proposal(c, resource="r" * 513),
    "proposal_version_not_text": lambda c: planted_proposal(c, expected_resource_version=7),
}


def test_a_tool_the_agent_does_not_have_is_never_a_proposal_even_with_a_valid_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The model may name any tool; only the agent's own protected tools can become a proposal."""
    ctx = ctx_with()
    with caplog.at_level(logging.DEBUG):
        result, _, _ = step(
            [tool_response(f"invented-{SECRET_MARKER}", proposal_input(ctx))], ctx=ctx
        )
    assert (result.stop_reason, result.error_code) == ("error", "malformed_result")
    assert result.proposed_actions == () and SECRET_MARKER not in caplog.text


@pytest.mark.parametrize("name", sorted(UNREADABLE))
def test_output_that_cannot_be_read_is_malformed_with_no_proposal_and_no_leak(
    name: str, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = ctx_with()
    with caplog.at_level(logging.DEBUG):
        result, client, _ = step([UNREADABLE[name](ctx)], ctx=ctx)
    assert (result.stop_reason, result.error_code) == ("error", "malformed_result")
    assert result.proposed_actions == ()
    assert ctx.protected_calls == [] and ctx.read_calls == []
    assert len(client.messages.calls) == 1  # no retry of an unreadable answer
    assert SECRET_MARKER not in caplog.text and SECRET_MARKER not in str(result.record())
    # The usage the provider reported on a readable one is kept, with the call counted.
    assert result.usage.turns == 1 and result.usage_source == "unknown"


@pytest.mark.parametrize(
    "usage",
    [
        None,
        Usage(None, 5),
        Usage(10, None),
        Usage(-1, 5),
        Usage(10, -1),
        Usage(True, 5),
        Usage(10.0, 5),
        Usage("10", 5),
        Usage(2**31, 5),
        Usage(10, 5, cache_creation="x"),
        Usage(10, 5, cache_read=-3),
        Usage(10, 5, cache_read=True),
    ],
    ids=[
        "none",
        "input_none",
        "output_none",
        "input_negative",
        "output_negative",
        "input_bool",
        "input_float",
        "input_text",
        "input_over_schema",
        "cache_creation_text",
        "cache_read_negative",
        "cache_read_bool",
    ],
)
def test_usage_the_provider_did_not_report_readably_is_malformed_and_never_invented(
    usage: Any,
) -> None:
    result, _, _ = step([Response([Block("text", text="done")], "end_turn", usage)])
    assert (result.stop_reason, result.error_code) == ("error", "malformed_result")
    # The call happened (one turn) and nothing is known of its tokens: zero from an "unknown" source, never a made-up count.
    assert used(result) == (0, 0, 0, 1, 0) and result.usage_source == "unknown"


def test_a_reply_without_a_usage_attribute_is_malformed() -> None:
    class Bare:
        content = [Block("text", text="done")]
        stop_reason = "end_turn"

    result, _, _ = step([Bare()])  # type: ignore[list-item]
    assert (result.stop_reason, result.error_code) == ("error", "malformed_result")


def test_a_reply_whose_attributes_raise_is_malformed_not_an_exception() -> None:
    class Hostile:
        usage = Usage(10, 5)
        stop_reason = "end_turn"

        @property
        def content(self) -> Any:
            raise RuntimeError(SECRET_MARKER)

    result, _, _ = step([Hostile()])  # type: ignore[list-item]
    assert (result.stop_reason, result.error_code) == ("error", "malformed_result")
    assert SECRET_MARKER not in str(result.record())


def test_a_valid_proposal_after_readable_tool_turns_carries_all_the_usage() -> None:
    ctx = ctx_with()
    result, _, _ = step(
        [
            read_call(usage=Usage(100, 20)),
            tool_response(PROTECTED_TOOL, proposal_input(ctx), Usage(50, 10)),
        ],
        ctx=ctx,
    )
    assert (result.stop_reason, used(result)) == ("proposed_action", (150, 30, 180, 2, 1))


def test_a_total_the_contract_cannot_hold_is_malformed_not_a_crash() -> None:
    """Usage over the schema's own ceiling cannot be reported: the step is dropped as malformed, with zero usage."""
    ctx = ctx_with(max_tokens=2**31 - 1)
    executor, _ = executor_with([text_response(usage=Usage(2**31 - 1, 1))], ctx=ctx)
    # Called directly: the adapter itself, not the boundary that would catch it, returns the typed failure.
    result = executor.run_step(ctx.request, ctx.step_input)
    assert (result.stop_reason, result.error_code) == ("error", "malformed_result")
    assert used(result) == (0, 0, 0, 0, 0) and result.usage_source == "unknown"


# ----------------------------------------------------------------------------------------- no leaking


def test_nothing_the_model_or_the_provider_said_is_logged_on_any_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctx = ctx_with()
    secret_args = {"case_id": SECRET_MARKER}
    with caplog.at_level(logging.DEBUG):
        for script in (
            [read_call(secret_args), text_response(SECRET_MARKER)],
            [tool_response(PROTECTED_TOOL, proposal_input(ctx), Usage(1, 1))],
            [Response([Block("text", text=SECRET_MARKER)], "refusal", Usage(1, 1))],
            [RuntimeError(SECRET_MARKER)],
        ):
            result, _, _ = step(script, ctx=ctx)
            assert SECRET_MARKER not in str(result.record())
    assert SECRET_MARKER not in caplog.text


# ------------------------------------------------------------------------- the harness and regressions


def test_the_harness_usage_override_is_additive_and_a_driver_may_state_what_its_stub_reports() -> (
    None
):
    default = make_context(Scenario.BUDGET_EXHAUSTED)
    assert default.staged_usage == StepUsage(900, 100, 1000, 3, 0)
    stated = make_context(Scenario.BUDGET_EXHAUSTED)
    stated.usage_override = StepUsage(900, 100, 1000, 1, 0)
    assert stated.staged_usage == StepUsage(900, 100, 1000, 1, 0)
    assert make_context(Scenario.COMPLETES).staged_usage == StepUsage(100, 20, 120, 1, 0)


def test_the_ungoverned_run_agent_path_is_unchanged_by_the_step_adapter() -> None:
    """``run_agent`` still runs the model's tool through the registry callables and reports tokens as before."""
    client = Client(
        [
            tool_response(READ_TOOL, {"case_id": "c-9"}, Usage(12, 6)),
            text_response("done", Usage(4, 8)),
        ]
    )
    executor = AnthropicChatWorkflowExecutor(api_key="unused", default_model="m", client=client)
    ctx = ctx_with()
    agent = agent_input(ctx).agent
    turn = executor.run_agent(
        agent, "hello", tool_callables={READ_TOOL: ctx.read_tool}, preview=False
    )
    assert turn.final_output == "done" and turn.tokens == 30
    assert ctx.read_calls == [{"case_id": "c-9"}]
    assert client.options == []  # the governed path's retry setting is not applied to it


def test_the_executors_constructor_is_compatible() -> None:
    executor = AnthropicChatWorkflowExecutor(api_key="unused", default_model="m")
    assert executor._step_clock is None
    assert executor._max_tokens == 4096
