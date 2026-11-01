"""``OpenAIChatWorkflowExecutor`` as an ``AgentStepAdapter`` (B08.openai-chat, #729).

The provider is never called: the executor is built on a stub client that stands in for the SDK at the transport
(``client.chat.completions.create``), so what runs is the real adapter, the real message and tool code and the real
``run_agent_step`` boundary. The stub counts every call it receives (``ctx.provider_called()``), records the request it was
sent and answers from a script, with a fixed clock so no deadline depends on the date the suite runs on.

The first half plugs the executor into the shared conformance harness (``tests/agent_step_conformance.py``); the second half
is what only a chat-completions adapter has to prove: limits, usage, the shape of a proposal, SDK timeouts, the one retry the
``run_agent`` path also makes, and that nothing the provider said reaches a result or a log line.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from caliber.contracts.agent_step import AgentStepRequest, AgentStepResult, parse_step_result
from caliber.testing.adapter_conformance import (
    MALFORMED_USAGE,
    AdapterUnderTest,
    Fault,
    Garbage,
    ProviderProbe,
    ProviderUsage,
    Script,
    ScriptItem,
    fault_exception,
)
from caliber.workflows.agent_step import (
    AgentStepInput,
    run_agent_step,
)
from caliber.workflows.runtime import OpenAIChatWorkflowExecutor
from tests.agent_step_conformance import (
    PROTECTED_CLASS,
    PROTECTED_TOOL,
    READ_TOOL,
    SCENARIOS,
    SECRET_MARKER,
    AdapterDriver,
    Scenario,
    StepContext,
    make_context,
    run_scenario,
)

NOW = datetime(2026, 10, 26, 9, 0, 0, tzinfo=timezone.utc)
DEADLINE = "2026-10-26T09:10:00Z"  # ten minutes after NOW: the harness' default


class Clock:
    """A fake clock: an aware ``datetime`` that only moves when a test moves it."""

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now
        self.reads = 0

    def __call__(self) -> datetime:
        self.reads += 1
        return self.now


# ------------------------------------------------------------------------------------------ the transport stub


def tool_call(name: str, arguments: Any, call_id: str = "call_1") -> SimpleNamespace:
    raw = arguments if isinstance(arguments, str) or arguments is None else json.dumps(arguments)
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=raw))


def completion(
    *,
    content: str | None = None,
    tool_calls: list[Any] | None = None,
    usage: tuple[int, int, int] | None = (10, 5, 15),
    finish_reason: str | None = "stop",
    refusal: str | None = None,
    cached: int | None = None,
) -> SimpleNamespace:
    message = SimpleNamespace(content=content, tool_calls=tool_calls, refusal=refusal)
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)]
    )
    if usage is not None:
        details = None if cached is None else SimpleNamespace(cached_tokens=cached)
        response.usage = SimpleNamespace(
            prompt_tokens=usage[0],
            completion_tokens=usage[1],
            total_tokens=usage[2],
            prompt_tokens_details=details,
        )
    return response


class StubClient:
    """``client.chat.completions.create`` answering from a script; ``with_options`` is the SDK's per-call override."""

    def __init__(self, script: list[Any], ctx: StepContext | None = None) -> None:
        self.script = list(script)
        self.ctx = ctx
        self.requests: list[dict[str, Any]] = []
        self.options: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def with_options(self, **options: Any) -> StubClient:
        self.options.append(options)
        return self

    def _create(self, **kwargs: Any) -> Any:
        if self.ctx is not None:
            self.ctx.provider_called(
                tools=[t["function"]["name"] for t in kwargs.get("tools") or []],
                sent=json.dumps(kwargs.get("messages"), default=str),
            )
        self.requests.append(copy.deepcopy(kwargs))
        entry = self.script.pop(0)
        if isinstance(entry, BaseException):
            raise entry
        return entry(kwargs) if callable(entry) else entry


def executor_for(
    client: Any, clock: Callable[[], datetime] | None = None, **options: Any
) -> OpenAIChatWorkflowExecutor:
    return OpenAIChatWorkflowExecutor(
        api_key="unused",
        default_model="gpt-4o",
        client=client,
        step_clock=Clock() if clock is None else clock,
        **options,
    )


def envelope(ctx: StepContext, **changes: Any) -> dict[str, Any]:
    """The arguments of a model's call to the protected tool: the action it proposes, with its claims."""
    proposal = {
        "connector": "document-request",
        "connector_version": "1.0.0",
        "effect_class": PROTECTED_CLASS,
        "resource": "cases/CASE-1/document-requests",
        "args": ctx.staged_args,
    }
    proposal.update(changes)
    return proposal


def limits(**changes: Any) -> dict[str, Any]:
    base = {"max_turns": 8, "max_tool_calls": 4, "max_tokens": 20000, "deadline": DEADLINE}
    base.update(changes)
    return base


def step(
    script: list[Any],
    scenario: Scenario = Scenario.COMPLETES,
    *,
    clock: Callable[[], datetime] | None = None,
    step_limits: dict[str, Any] | None = None,
    **options: Any,
) -> tuple[AgentStepResult, StepContext, StubClient]:
    """Run one step through the real boundary against a scripted stub."""
    ctx = make_context(scenario, limits=step_limits or limits())
    client = StubClient(script, ctx)
    result = run_agent_step(executor_for(client, clock, **options), ctx.request, ctx.step_input)
    assert parse_step_result(result.record(), ctx.request) == result
    assert ctx.protected_calls == []
    return result, ctx, client


def read_call(ctx: StepContext, call_id: str = "call_1") -> SimpleNamespace:
    return tool_call(READ_TOOL, ctx.staged_read_args, call_id)


def usage_of(result: AgentStepResult) -> tuple[int, int, int, int, int]:
    u = result.usage
    return u.prompt_tokens, u.completion_tokens, u.total_tokens, u.turns, u.tool_calls


# ------------------------------------------------------------------------ the shared conformance harness


class OpenAIChatDriver:
    """The harness driver: each scenario is what the stubbed chat-completions transport answers."""

    name = "openai_chat"
    # A chat-completions response carries no identity of the step it answers, so another step's result cannot be staged.
    unsupported = frozenset({Scenario.ANOTHER_STEPS_RESULT})

    def adapter_for(self, scenario: Scenario, ctx: StepContext) -> object:
        usage = ctx.staged_usage
        proposal = envelope(ctx)
        staged: dict[Scenario, list[Any]] = {
            Scenario.COMPLETES: [
                completion(
                    content="done",
                    usage=(usage.prompt_tokens, usage.completion_tokens, usage.total_tokens),
                )
            ],
            Scenario.READ_TOOL_CALL: [
                completion(tool_calls=[read_call(ctx)], usage=(70, 10, 80)),
                completion(content="the case is open", usage=(80, 20, 100)),
            ],
            Scenario.PROPOSES_PROTECTED_TOOL: [
                completion(tool_calls=[tool_call(PROTECTED_TOOL, proposal)], usage=(100, 20, 120))
            ],
            Scenario.MODEL_LIES_ABOUT_EFFECT_CLASS: [
                completion(
                    tool_calls=[tool_call(PROTECTED_TOOL, envelope(ctx, effect_class="read"))],
                    usage=(100, 20, 120),
                )
            ],
            Scenario.BUDGET_EXHAUSTED: [
                completion(content="cut off", usage=(900, 100, 1000), finish_reason="length")
            ],
            Scenario.PROVIDER_ERROR: [RuntimeError(f"upstream said {SECRET_MARKER}")],
            Scenario.PROVIDER_TIMEOUT: [httpx.ReadTimeout(SECRET_MARKER)],
            Scenario.UNREADABLE_OUTPUT: [{"note": SECRET_MARKER}],
            Scenario.CLAIMS_COMMIT: [
                completion(
                    tool_calls=[
                        tool_call(
                            PROTECTED_TOOL,
                            {**proposal, "state": "committed", "receipt_id": SECRET_MARKER},
                        )
                    ]
                )
            ],
        }
        return executor_for(StubClient(staged[scenario], ctx))


def _chat_entry(item: ScriptItem) -> Any:
    """What the stubbed chat-completions transport does for one step of a conformance script."""
    if isinstance(item, Fault):
        return httpx.ReadTimeout(item.text) if item.kind == "timeout" else fault_exception(item)
    if isinstance(item, Garbage):
        return {"note": item.text}
    if item.usage is None:
        usage: Any = None
    elif item.usage is MALFORMED_USAGE:
        usage = ("many", "some", "lots")
    else:
        assert isinstance(item.usage, ProviderUsage)
        usage = (item.usage.prompt, item.usage.completion, item.usage.total_tokens)
    calls = [tool_call(c.name, c.arguments, f"call_{n}") for n, c in enumerate(item.calls, 1)]
    return completion(
        content=item.text,
        tool_calls=calls or None,
        usage=usage,
        finish_reason="length" if item.cut_off else ("tool_calls" if calls else "stop"),
    )


def chat_subject() -> AdapterUnderTest:
    """The conformance suite's subject for ``OpenAIChatWorkflowExecutor``: the real executor over a canned transport."""

    def build(script: Script, probe: ProviderProbe) -> object:
        client = StubClient([_chat_entry(item) for item in script], probe)  # type: ignore[arg-type]
        return executor_for(client, probe.clock.now)

    return AdapterUnderTest(name="openai_chat", build=build)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_the_openai_chat_executor_conforms_to_the_shared_harness(scenario: Scenario) -> None:
    driver: AdapterDriver = OpenAIChatDriver()
    result, ctx = run_scenario(driver, scenario)
    assert result.step_request_id == ctx.request.step_request_id


def test_the_real_sdk_timeout_conforms_too() -> None:
    openai = pytest.importorskip("openai")

    class SdkTimeoutDriver(OpenAIChatDriver):
        def adapter_for(self, scenario: Scenario, ctx: StepContext) -> object:
            timeout = openai.APITimeoutError(request=httpx.Request("POST", "https://x.invalid/v1"))
            return executor_for(StubClient([timeout], ctx))

    run_scenario(SdkTimeoutDriver(), Scenario.PROVIDER_TIMEOUT)


# ------------------------------------------------------------------------------- what the model is offered


def test_read_tools_are_offered_plainly_and_protected_tools_as_a_proposal() -> None:
    result, ctx, client = step([completion(content="ok")])
    assert result.stop_reason == "completed"
    (request,) = client.requests
    specs = {spec["function"]["name"]: spec["function"] for spec in request["tools"]}
    assert set(specs) == {READ_TOOL, PROTECTED_TOOL}
    assert "properties" in specs[READ_TOOL]["parameters"]
    assert (
        "proposal" not in specs[READ_TOOL]["description"]
        and "proposes" not in specs[READ_TOOL]["description"]
    )
    protected = specs[PROTECTED_TOOL]
    assert "proposes the action" in protected["description"]
    schema = protected["parameters"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "connector",
        "connector_version",
        "effect_class",
        "resource",
        "args",
    }
    assert "irreversible_write" in schema["properties"]["effect_class"]["enum"]
    assert request["tool_choice"] == "auto"
    assert request["messages"][0]["role"] == "system"
    assert request["messages"][-1] == {"role": "user", "content": ctx.step_input.input_text}


def test_history_and_a_tool_result_reach_the_next_request_in_order() -> None:
    ctx = make_context(limits=limits())
    history = ({"role": "user", "content": "earlier"}, {"role": "assistant", "content": "reply"})
    step_input = dataclasses.replace(ctx.step_input, history=history)
    client = StubClient(
        [completion(tool_calls=[read_call(ctx, "call_42")]), completion(content="done")], ctx
    )
    result = run_agent_step(executor_for(client), ctx.request, step_input)
    assert result.stop_reason == "completed"
    first, second = client.requests
    assert [m["role"] for m in first["messages"]] == ["system", "user", "assistant", "user"]
    assert first["messages"][1:3] == list(history)
    tail = second["messages"][-2:]
    assert tail[0]["role"] == "assistant" and tail[0]["tool_calls"][0]["id"] == "call_42"
    assert tail[1] == {"role": "tool", "tool_call_id": "call_42", "content": "data"}


def test_each_call_is_bounded_by_the_deadline_the_budget_and_no_sdk_retries() -> None:
    clock = Clock(NOW)
    deadline = (NOW + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ctx = make_context(limits=limits(deadline=deadline, max_tokens=1000))
    client = StubClient(
        [completion(tool_calls=[read_call(ctx)], usage=(60, 20, 80)), completion(content="ok")],
        ctx,
    )
    result = run_agent_step(executor_for(client, clock), ctx.request, ctx.step_input)
    assert result.stop_reason == "completed"
    # The SDK's own retries are off and its timeout cannot outlast the 30 seconds left (the configured bound is 120).
    assert client.options == [{"max_retries": 0, "timeout": 30.0}] * 2
    assert [r["max_completion_tokens"] for r in client.requests] == [1000, 920]


def test_a_client_without_per_call_options_is_used_as_it_is() -> None:
    ctx = make_context(limits=limits())

    class Plain:
        def __init__(self) -> None:
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=lambda **kw: completion(content="ok"))
            )

    result = run_agent_step(executor_for(Plain()), ctx.request, ctx.step_input)
    assert result.stop_reason == "completed" and result.usage_source == "provider"


def test_the_model_and_cache_settings_follow_the_executor() -> None:
    ctx = make_context(limits=limits())
    client = StubClient([completion(content="ok")], ctx)
    executor = executor_for(
        client,
        parallel_tool_calls=True,
        prompt_cache_enabled=True,
        prompt_cache_retention="24h",
    )
    run_agent_step(executor, ctx.request, ctx.step_input)
    (sent,) = client.requests
    assert sent["model"] == "gpt-4o" and sent["temperature"] == 0.2
    assert sent["parallel_tool_calls"] is True
    assert sent["prompt_cache_key"].startswith("caliber:chat:")
    assert sent["prompt_cache_retention"] == "24h"
    # The agent's own model wins over the executor's default.
    agent = dataclasses.replace(ctx.step_input.agent, model="gpt-4.1")
    client = StubClient([completion(content="ok")], ctx)
    run_agent_step(
        executor_for(client), ctx.request, dataclasses.replace(ctx.step_input, agent=agent)
    )
    (sent,) = client.requests
    assert (
        sent["model"] == "gpt-4.1"
        and "prompt_cache_key" not in sent
        and "parallel_tool_calls" not in sent
    )


class ToolsOrReasoningError(Exception):
    """The 400 a reasoning model answers function tools plus a reasoning effort with (what ``run_agent`` also handles)."""

    status_code = 400

    def __init__(self) -> None:
        super().__init__(
            "Error code: 400 - Function tools with reasoning_effort are not supported for this model"
        )


def reasoning_step(script: list[Any]) -> tuple[AgentStepResult, StepContext, StubClient]:
    ctx = make_context(limits=limits())
    agent = dataclasses.replace(ctx.step_input.agent, model="gpt-5.6-luna")
    client = StubClient(script, ctx)
    executor = executor_for(client, reasoning_effort="high")
    result = run_agent_step(executor, ctx.request, dataclasses.replace(ctx.step_input, agent=agent))
    return result, ctx, client


def test_a_rejected_tools_with_reasoning_request_is_retried_once_with_effort_none() -> None:
    ctx = make_context(limits=limits())
    result, _, client = reasoning_step(
        [ToolsOrReasoningError(), completion(tool_calls=[read_call(ctx)]), completion(content="ok")]
    )
    assert result.stop_reason == "completed"
    # The rejected request used nothing: it is not a turn, but it is a provider call, and the next turn remembers.
    assert [r["reasoning_effort"] for r in client.requests] == ["high", "none", "none"]
    assert "temperature" not in client.requests[0]
    assert result.usage.turns == 2


def test_the_retry_is_only_once_and_other_provider_errors_are_not_retried() -> None:
    result, ctx, client = reasoning_step([ToolsOrReasoningError(), ToolsOrReasoningError()])
    assert (result.stop_reason, result.error_code) == ("error", "provider_error")
    assert len(client.requests) == 2 and ctx.provider_calls == 2

    class OtherError(Exception):
        status_code = 400

    result, ctx, client = reasoning_step([OtherError("model not found")])
    assert (result.stop_reason, result.error_code) == ("error", "provider_error")
    assert len(client.requests) == 1


# ------------------------------------------------------------------------------------------- the stops


def test_a_final_answer_is_completed_with_the_providers_usage() -> None:
    result, _, _ = step([completion(content="all done", usage=(11, 7, 18), cached=4)])
    assert (result.stop_reason, result.error_code, result.proposed_actions) == (
        "completed",
        None,
        (),
    )
    assert usage_of(result) == (11, 7, 18, 1, 0)
    assert result.usage.cached_prompt_tokens == 4
    assert result.usage_source == "provider"


def test_usage_is_summed_over_turns_and_a_total_below_the_parts_is_raised_to_them() -> None:
    ctx = make_context(limits=limits())
    result, _, _ = step(
        [
            completion(tool_calls=[read_call(ctx)], usage=(10, 5, 3)),
            completion(content="ok", usage=(20, 5, 25), cached=999),
        ]
    )
    # 3 < 10 + 5 is raised to 15; cached tokens are part of the prompt and never exceed it.
    assert usage_of(result) == (30, 10, 40, 2, 1)
    assert (
        result.usage.cached_prompt_tokens == 20
    )  # turn two's 999 is clamped to its own 20-token prompt


def test_usage_may_be_a_mapping_and_partial() -> None:
    response = completion(content="ok", usage=None)
    response.usage = {"prompt_tokens": 9, "completion_tokens": 3}
    result, _, _ = step([response])
    assert usage_of(result) == (9, 3, 12, 1, 0) and result.usage_source == "provider"
    assert result.usage.cached_prompt_tokens is None  # not reported is not zero


@pytest.mark.parametrize("spelling", ["input_tokens_details", "cached_input_tokens"])
def test_the_other_spellings_of_cached_tokens_are_read(spelling: str) -> None:
    response = completion(content="ok", usage=(10, 5, 15))
    del response.usage.prompt_tokens_details
    value = SimpleNamespace(cached_tokens=6) if spelling == "input_tokens_details" else 6
    setattr(response.usage, spelling, value)
    result, _, _ = step([response])
    assert result.usage.cached_prompt_tokens == 6


def test_a_response_without_usage_is_estimated_and_says_so() -> None:
    result, ctx, _ = step([completion(content="two words", usage=None)])
    words = len(ctx.step_input.input_text.split())
    assert result.usage_source == "estimated"
    assert result.usage.prompt_tokens > words and result.usage.completion_tokens == 2
    assert result.usage.total_tokens == result.usage.prompt_tokens + 2 and result.usage.turns == 1
    # An empty answer still costs a token (the same floor run_agent has).
    result, _, _ = step([completion(content=None, usage=None)])
    assert result.usage.completion_tokens == 1


@pytest.mark.parametrize(
    "bad",
    [-1, "12", 1.5, True, [], 2147483648],
    ids=["negative", "text", "float", "bool", "list", "above-the-contract-maximum"],
)
def test_unreadable_usage_is_malformed_not_guessed(bad: Any) -> None:
    response = completion(content="ok")
    response.usage.total_tokens = bad
    result, _, _ = step([response])
    assert (result.stop_reason, result.error_code) == ("error", "malformed_result")
    assert result.usage_source == "unknown" and result.usage.total_tokens == 0


def test_unreadable_cached_tokens_are_malformed() -> None:
    response = completion(content="ok", cached=1)
    response.usage.prompt_tokens_details.cached_tokens = "many"
    result, _, _ = step([response])
    assert result.error_code == "malformed_result"


def test_usage_past_the_contract_maximum_keeps_what_was_used_before_it() -> None:
    ctx = make_context(limits=limits())
    big = 2147483647
    result, _, _ = step(
        [
            completion(tool_calls=[read_call(ctx)], usage=(1, 1, 2)),
            completion(content="ok", usage=(big, 0, big)),
        ]
    )
    assert (result.stop_reason, result.error_code) == ("error", "malformed_result")
    assert usage_of(result) == (1, 1, 2, 1, 1) and result.usage_source == "unknown"


def test_a_response_the_provider_cut_off_is_max_tokens_and_its_tool_calls_are_not_acted_on() -> (
    None
):
    ctx = make_context(limits=limits())
    result, _, _ = step(
        [completion(tool_calls=[read_call(ctx)], usage=(50, 50, 100), finish_reason="length")]
    )
    assert result.stop_reason == "max_tokens" and result.proposed_actions == ()
    assert usage_of(result) == (50, 50, 100, 1, 0) and result.usage_source == "provider"
    assert ctx.read_calls == []


def test_a_model_refusal_and_a_content_filter_are_refused_with_the_usage_kept() -> None:
    result, _, _ = step([completion(content=None, refusal="I cannot help", usage=(5, 5, 10))])
    assert (result.stop_reason, result.error_code) == ("refused", "model_refused")
    assert usage_of(result) == (5, 5, 10, 1, 0) and result.usage_source == "provider"
    assert "cannot" not in json.dumps(result.record())
    result, _, _ = step([completion(content="x", finish_reason="content_filter")])
    assert (result.stop_reason, result.error_code) == ("refused", "content_filtered")
    # A blank refusal is no refusal.
    result, _, _ = step([completion(content="fine", refusal="  ")])
    assert result.stop_reason == "completed"


def test_a_refusal_next_to_tool_calls_does_not_hide_them() -> None:
    ctx = make_context(limits=limits())
    result, run_ctx, _ = step(
        [completion(refusal="no", tool_calls=[read_call(ctx)]), completion(content="ok")]
    )
    assert result.stop_reason == "completed" and run_ctx.read_calls == [ctx.staged_read_args]


# -------------------------------------------------------------------------------------------- limits


def test_max_turns_stops_after_the_last_allowed_turn_and_keeps_its_usage() -> None:
    ctx = make_context(limits=limits(max_turns=1))
    client = StubClient([completion(tool_calls=[read_call(ctx)], usage=(70, 10, 80))], ctx)
    result = run_agent_step(executor_for(client), ctx.request, ctx.step_input)
    assert result.stop_reason == "max_turns" and result.proposed_actions == ()
    assert usage_of(result) == (70, 10, 80, 1, 1) and result.usage_source == "provider"
    assert len(client.requests) == 1 and ctx.read_calls == [ctx.staged_read_args]


def test_the_exact_turn_limit_is_allowed() -> None:
    ctx = make_context(limits=limits(max_turns=2))
    result, _, client = step(
        [completion(tool_calls=[read_call(ctx)]), completion(content="ok")],
        step_limits=limits(max_turns=2),
    )
    assert result.stop_reason == "completed" and len(client.requests) == 2


@pytest.mark.parametrize(
    ("bound", "reason"),
    [
        (limits(max_turns=0), "max_turns"),
        (limits(max_tokens=0), "max_tokens"),
        (limits(deadline="2026-10-26T09:00:00Z"), "deadline"),
        (limits(deadline="2000-01-01T00:00:00Z"), "deadline"),
    ],
    ids=["zero-turns", "zero-tokens", "deadline-reached-exactly", "deadline-long-past"],
)
def test_a_step_that_may_not_start_never_reaches_the_provider(
    bound: dict[str, Any], reason: str
) -> None:
    result, ctx, client = step([RuntimeError("never called")], step_limits=bound)
    assert result.stop_reason == reason and result.proposed_actions == ()
    assert ctx.provider_calls == 0 and client.requests == []
    assert usage_of(result) == (0, 0, 0, 0, 0) and result.usage_source == "estimated"


def test_a_step_that_may_not_start_does_not_even_touch_the_client() -> None:
    class Tripwire:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"the client was used ({name})")

    ctx = make_context(limits=limits(max_turns=0))
    result = run_agent_step(executor_for(Tripwire()), ctx.request, ctx.step_input)
    assert result.stop_reason == "max_turns"


def test_the_token_limit_is_checked_after_each_response_and_before_each_call() -> None:
    ctx = make_context(limits=limits())
    script = [completion(tool_calls=[read_call(ctx)], usage=(70, 10, 80))]
    # Spent exactly the limit: not over it, so the model's tool call runs; but nothing is left for another call.
    result, run_ctx, client = step(list(script), step_limits=limits(max_tokens=80))
    assert result.stop_reason == "max_tokens"
    assert usage_of(result) == (70, 10, 80, 1, 1) and len(client.requests) == 1
    assert run_ctx.read_calls == [ctx.staged_read_args]
    # One over the limit: the step stops before it runs what the model asked for.
    result, run_ctx, client = step(list(script), step_limits=limits(max_tokens=79))
    assert result.stop_reason == "max_tokens"
    assert usage_of(result) == (70, 10, 80, 1, 0) and run_ctx.read_calls == []


def test_a_step_over_its_token_limit_proposes_nothing() -> None:
    ctx = make_context(limits=limits(max_tokens=100))
    result, _, _ = step(
        [completion(tool_calls=[tool_call(PROTECTED_TOOL, envelope(ctx))], usage=(90, 20, 110))],
        step_limits=limits(max_tokens=100),
    )
    assert result.stop_reason == "max_tokens" and result.proposed_actions == ()
    result, _, _ = step(
        [completion(content="ok", usage=(90, 20, 110))], step_limits=limits(max_tokens=100)
    )
    assert result.stop_reason == "max_tokens"


def test_the_tool_call_limit_is_all_or_nothing_per_response() -> None:
    ctx = make_context(limits=limits(max_tool_calls=1))
    two = [read_call(ctx, "a"), read_call(ctx, "b")]
    client = StubClient([completion(tool_calls=two, usage=(10, 5, 15))], ctx)
    result = run_agent_step(executor_for(client), ctx.request, ctx.step_input)
    assert result.stop_reason == "max_tool_calls" and usage_of(result) == (10, 5, 15, 1, 0)
    assert ctx.read_calls == []
    # Exactly the limit runs both.
    ctx = make_context(limits=limits(max_tool_calls=2))
    client = StubClient([completion(tool_calls=two), completion(content="ok")], ctx)
    result = run_agent_step(executor_for(client), ctx.request, ctx.step_input)
    assert result.stop_reason == "completed" and usage_of(result)[3:] == (2, 2)
    assert ctx.read_calls == [ctx.staged_read_args] * 2
    # Zero tool calls allowed: the first read tool call stops the step.
    ctx = make_context(limits=limits(max_tool_calls=0))
    client = StubClient([completion(tool_calls=[read_call(ctx)])], ctx)
    result = run_agent_step(executor_for(client), ctx.request, ctx.step_input)
    assert result.stop_reason == "max_tool_calls" and ctx.read_calls == []


def test_the_tool_call_limit_counts_across_turns() -> None:
    ctx = make_context(limits=limits(max_tool_calls=1))
    client = StubClient(
        [completion(tool_calls=[read_call(ctx)]), completion(tool_calls=[read_call(ctx, "b")])], ctx
    )
    result = run_agent_step(executor_for(client), ctx.request, ctx.step_input)
    assert result.stop_reason == "max_tool_calls" and usage_of(result)[3:] == (2, 1)
    assert len(ctx.read_calls) == 1


def test_a_deadline_that_passes_between_turns_stops_the_step_with_the_usage_so_far() -> None:
    clock = Clock(NOW)
    ctx = make_context(limits=limits())

    def tick(kwargs: dict[str, Any]) -> SimpleNamespace:
        clock.now = NOW + timedelta(minutes=11)  # the provider answered after the deadline
        return completion(tool_calls=[read_call(ctx)], usage=(70, 10, 80))

    client = StubClient([tick], ctx)
    result = run_agent_step(executor_for(client, clock), ctx.request, ctx.step_input)
    assert result.stop_reason == "deadline" and result.proposed_actions == ()
    assert usage_of(result) == (70, 10, 80, 1, 1) and result.usage_source == "provider"
    assert len(client.requests) == 1


def test_the_real_clock_is_used_without_an_injected_one() -> None:
    ctx = make_context(limits=limits(deadline="2999-01-01T00:00:00Z"))
    client = StubClient([completion(content="ok")], ctx)
    executor = OpenAIChatWorkflowExecutor(api_key="unused", default_model="gpt-4o", client=client)
    assert run_agent_step(executor, ctx.request, ctx.step_input).stop_reason == "completed"
    ctx = make_context(limits=limits(deadline="2000-01-01T00:00:00Z"))
    client = StubClient([completion(content="ok")], ctx)
    executor = OpenAIChatWorkflowExecutor(api_key="unused", default_model="gpt-4o", client=client)
    assert run_agent_step(executor, ctx.request, ctx.step_input).stop_reason == "deadline"
    assert client.requests == []


# -------------------------------------------------------------------------------------- proposals


def test_a_protected_tool_is_proposed_from_the_models_envelope_and_never_run() -> None:
    ctx = make_context(limits=limits())
    result, _, client = step(
        [
            completion(
                tool_calls=[
                    tool_call(PROTECTED_TOOL, envelope(ctx, expected_resource_version="v7"))
                ],
                usage=(40, 10, 50),
            )
        ]
    )
    (action,) = result.proposed_actions
    assert result.stop_reason == "proposed_action" and usage_of(result) == (40, 10, 50, 1, 0)
    assert (action.connector, action.connector_version, action.resource) == (
        "document-request",
        "1.0.0",
        "cases/CASE-1/document-requests",
    )
    assert action.expected_resource_version == "v7" and dict(action.args) == ctx.staged_args
    assert action.platform_bound is False
    assert ctx.protected_calls == [] and len(client.requests) == 1


def test_the_first_protected_call_wins_and_the_rest_of_the_response_does_not_run() -> None:
    ctx = make_context(limits=limits())
    first = envelope(ctx, resource="cases/CASE-1/first")
    second = envelope(ctx, resource="cases/CASE-2/second")
    result, _, _ = step(
        [
            completion(
                tool_calls=[
                    read_call(ctx, "r"),
                    tool_call(PROTECTED_TOOL, first, "p1"),
                    tool_call(PROTECTED_TOOL, second, "p2"),
                ]
            )
        ]
    )
    (action,) = result.proposed_actions
    assert action.resource == "cases/CASE-1/first"
    assert ctx.read_calls == [] and result.usage.tool_calls == 0


def test_a_tool_the_agent_does_not_have_is_neither_run_nor_proposed() -> None:
    # Even a perfectly formed envelope is refused when the tool is not one of the agent's: a name is not a proposal.
    shape = make_context(limits=limits())
    result, ctx, _ = step([completion(tool_calls=[tool_call("ghost_tool", envelope(shape))])])
    assert (result.stop_reason, result.error_code) == ("error", "malformed_result")
    assert ctx.read_calls == []
    assert result.proposed_actions == () and usage_of(result) == (10, 5, 15, 1, 0)
    assert result.usage_source == "unknown"


def bad_envelopes(ctx: StepContext) -> dict[str, Any]:
    good = envelope(ctx)
    return {
        "not-json": "{nope",
        "empty-arguments": "",
        "a-list": [good],
        "missing-resource": {k: v for k, v in good.items() if k != "resource"},
        "missing-args": {k: v for k, v in good.items() if k != "args"},
        "unknown-key": {**good, "state": "committed"},
        "a-receipt": {**good, "receipt_id": "r-1"},
        "unknown-effect-class": {**good, "effect_class": "delete_everything"},
        "args-not-an-object": {**good, "args": ["x"]},
        "blank-resource": {**good, "resource": "  "},
        "resource-with-control-character": {**good, "resource": "cases/\x00x"},
        "connector-not-text": {**good, "connector": 7},
        "args-not-plain-json": {**good, "args": {"n": float("nan")}},
        "arguments-null": None,
    }


@pytest.mark.parametrize("label", list(bad_envelopes(make_context()).keys()))
def test_an_envelope_that_cannot_be_read_strictly_is_malformed(label: str) -> None:
    ctx = make_context(limits=limits())
    arguments = bad_envelopes(ctx)[label]
    if label == "args-not-plain-json":
        arguments = json.dumps(arguments)  # json.dumps writes NaN, which the contract refuses
    result, _, _ = step([completion(tool_calls=[tool_call(PROTECTED_TOOL, arguments)])])
    assert (result.stop_reason, result.error_code) == ("error", "malformed_result")
    assert result.proposed_actions == () and usage_of(result) == (10, 5, 15, 1, 0)
    assert result.usage_source == "unknown"


def test_deeply_nested_arguments_are_malformed_not_a_crash() -> None:
    result, _, _ = step([completion(tool_calls=[tool_call(PROTECTED_TOOL, "[" * 200_000)])])
    assert (result.stop_reason, result.error_code) == ("error", "malformed_result")


# ------------------------------------------------------------------------------------- read tools


def test_a_read_tool_runs_through_read_tools_with_the_models_arguments() -> None:
    result, ctx, client = step(
        [
            completion(tool_calls=[tool_call(READ_TOOL, {"case_id": "C-9"}, "c9")]),
            completion(content="done"),
        ]
    )
    assert result.stop_reason == "completed"
    assert result.usage.tool_calls == 1 and ctx.read_calls == [{"case_id": "C-9"}]
    assert len(client.requests) == 2


def test_a_failing_read_tool_is_reported_to_the_model_and_its_text_stays_out_of_results_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctx = make_context(limits=limits())

    def failing(**arguments: Any) -> str:
        raise RuntimeError(f"db said {SECRET_MARKER}")

    step_input = dataclasses.replace(ctx.step_input, read_tools={READ_TOOL: failing})
    client = StubClient([completion(tool_calls=[read_call(ctx)]), completion(content="ok")], ctx)
    caplog.set_level(logging.DEBUG)
    result = run_agent_step(executor_for(client), ctx.request, step_input)
    assert result.stop_reason == "completed" and result.usage.tool_calls == 1
    assert SECRET_MARKER not in json.dumps(result.record())
    assert SECRET_MARKER not in caplog.text


def test_a_protected_tool_called_with_plain_arguments_is_not_run_to_make_sense_of_them() -> None:
    """The protected tool's canary is behind the agent's binding and the registry, not in ``read_tools``."""
    result, ctx, _ = step(
        [completion(tool_calls=[tool_call(PROTECTED_TOOL, {"document_type": "x"})])]
    )
    assert result.error_code == "malformed_result" and ctx.protected_calls == []


# --------------------------------------------------------------------------------------- failures


@pytest.mark.parametrize(
    "timeout",
    [
        TimeoutError("t"),
        asyncio.TimeoutError("t"),
        httpx.ReadTimeout("t"),
        httpx.ConnectTimeout("t"),
    ],
    ids=["builtin", "asyncio", "httpx-read", "httpx-connect"],
)
def test_provider_timeouts_are_adapter_timeout(timeout: Exception) -> None:
    result, _, _ = step([timeout])
    assert (result.stop_reason, result.error_code) == ("error", "adapter_timeout")
    assert result.usage_source == "unknown" and result.usage.total_tokens == 0


@pytest.mark.parametrize(
    "boom",
    [
        RuntimeError("x"),
        ValueError("x"),
        httpx.ConnectError("x"),
        KeyError("x"),
        AssertionError("x"),
    ],
    ids=["runtime", "value", "connect", "key", "assertion"],
)
def test_any_other_provider_exception_is_provider_error(boom: Exception) -> None:
    result, _, _ = step([boom])
    assert (result.stop_reason, result.error_code) == ("error", "provider_error")


def test_a_failure_after_a_turn_keeps_that_turns_usage_as_unknown_source() -> None:
    ctx = make_context(limits=limits())
    for fail, code in (
        (httpx.ReadTimeout("t"), "adapter_timeout"),
        (RuntimeError("x"), "provider_error"),
    ):
        result, _, _ = step([completion(tool_calls=[read_call(ctx)], usage=(70, 10, 80)), fail])
        assert (result.stop_reason, result.error_code) == ("error", code)
        assert usage_of(result) == (70, 10, 80, 1, 1) and result.usage_source == "unknown"


@pytest.mark.parametrize(
    "response",
    [
        None,
        {"choices": []},
        SimpleNamespace(choices=[]),
        SimpleNamespace(choices="x"),
        SimpleNamespace(choices=[SimpleNamespace()]),
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(), finish_reason=3)]),
        completion(tool_calls=[tool_call("", {})]),
        completion(
            tool_calls=[
                SimpleNamespace(id="x", function=SimpleNamespace(name=READ_TOOL, arguments=7))
            ]
        ),
        completion(tool_calls=[SimpleNamespace(id="x", function=None)]),
        completion(
            tool_calls=[SimpleNamespace(id=5, function=SimpleNamespace(name="a", arguments="{}"))]
        ),
        completion(tool_calls=[tool_call(5, {})]),
    ],
    ids=[
        "none",
        "dict",
        "no-choices",
        "choices-not-a-list",
        "no-message",
        "finish-reason-not-text",
        "empty-tool-name",
        "arguments-not-text",
        "no-function",
        "id-not-text",
        "name-not-text",
    ],
)
def test_a_response_that_is_not_the_sdks_shape_is_malformed(response: Any) -> None:
    result, _, _ = step([response])
    assert (result.stop_reason, result.error_code) == ("error", "malformed_result")
    assert result.proposed_actions == () and result.usage_source == "unknown"


def test_missing_tool_arguments_are_empty_arguments() -> None:
    """A read call with no ``arguments`` (``None``) is a call with none, as in ``run_agent``."""
    ctx = make_context(limits=limits())
    result, _, _ = step(
        [completion(tool_calls=[tool_call(READ_TOOL, None)]), completion(content="ok")]
    )
    assert result.stop_reason == "completed"


def test_a_failure_logs_only_the_class_name_and_the_step(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    result, ctx, _ = step([RuntimeError(f"upstream {SECRET_MARKER}")])
    assert result.error_code == "provider_error"
    text = caplog.text
    assert (
        SECRET_MARKER not in text and "RuntimeError" in text and ctx.request.step_request_id in text
    )


def test_a_naive_clock_is_a_typed_failure_not_an_exception() -> None:
    result, _, client = step(
        [completion(content="ok")], clock=lambda: datetime(2026, 10, 26, 9, 0, 0)
    )
    assert (result.stop_reason, result.error_code) == ("error", "provider_error")
    assert client.requests == []


def test_an_interrupt_is_not_swallowed() -> None:
    ctx = make_context(limits=limits())
    client = StubClient([KeyboardInterrupt()], ctx)
    with pytest.raises(KeyboardInterrupt):
        run_agent_step(executor_for(client), ctx.request, ctx.step_input)


def test_the_adapter_returns_a_typed_failure_itself_without_the_boundary() -> None:
    ctx = make_context(limits=limits())
    adapter = executor_for(StubClient([RuntimeError(SECRET_MARKER)], ctx))
    result = adapter.run_step(ctx.request, ctx.step_input)
    assert type(result) is AgentStepResult and result.error_code == "provider_error"


# ------------------------------------------------------------------------ the existing path is unchanged


def test_step_input_is_not_changed_by_a_step() -> None:
    ctx = make_context(limits=limits())
    before = (dict(ctx.step_input.read_tools), ctx.step_input.history, ctx.step_input.input_text)
    step([completion(content="ok")])
    run_agent_step(
        executor_for(StubClient([completion(content="ok")], ctx)), ctx.request, ctx.step_input
    )
    assert (
        dict(ctx.step_input.read_tools),
        ctx.step_input.history,
        ctx.step_input.input_text,
    ) == before


def test_a_request_for_another_node_is_still_refused_by_the_boundary_before_the_client() -> None:
    ctx = make_context(limits=limits())
    other = make_context(limits=limits(), node_id="another_node")
    client = StubClient([completion(content="ok")], ctx)
    with pytest.raises(Exception, match="node"):
        run_agent_step(executor_for(client), other.request, ctx.step_input)
    assert client.requests == []


def test_the_request_and_input_types_are_the_contracts() -> None:
    ctx = make_context()
    assert isinstance(ctx.request, AgentStepRequest) and isinstance(ctx.step_input, AgentStepInput)


# ---------------------------------------------------------------------- the remaining request shapes


def test_a_reasoning_model_with_an_unknown_effort_sends_none_and_an_agent_without_tools_sends_no_tools() -> (
    None
):
    ctx = make_context(limits=limits())
    agent = dataclasses.replace(ctx.step_input.agent, model="gpt-5.6-luna", tools=[])
    step_input = AgentStepInput(agent=agent, input_text="hello")
    client = StubClient([completion(content="ok")], ctx)
    executor = executor_for(client, reasoning_effort="not-an-effort", prompt_cache_enabled=True)
    result = run_agent_step(executor, ctx.request, step_input)
    assert result.stop_reason == "completed"
    (sent,) = client.requests
    assert "reasoning_effort" not in sent and "temperature" not in sent
    assert "tools" not in sent and "tool_choice" not in sent
    # No retention was configured, so the cache key is sent alone.
    assert (
        sent["prompt_cache_key"].startswith("caliber:chat:")
        and "prompt_cache_retention" not in sent
    )


def test_a_usage_object_with_no_figures_is_no_usage() -> None:
    response = completion(content="ok words here")
    response.usage = SimpleNamespace(prompt_tokens=None, completion_tokens=None, total_tokens=None)
    result, _, _ = step([response])
    assert result.usage_source == "estimated" and result.usage.completion_tokens == 3


def test_a_timeout_is_recognised_without_the_openai_sdk_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    from caliber.workflows import step_openai_chat

    real = importlib.import_module

    def without_openai(name: str, *args: Any) -> Any:
        if name == "openai":
            raise ImportError(name)
        return real(name, *args)

    monkeypatch.setattr(step_openai_chat.importlib, "import_module", without_openai)
    result, _, _ = step([httpx.ReadTimeout("t")])
    assert result.error_code == "adapter_timeout"
    result, _, _ = step([RuntimeError("t")])
    assert result.error_code == "provider_error"
