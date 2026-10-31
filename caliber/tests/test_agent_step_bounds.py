"""The platform enforces a step's turns, tool calls, tokens and deadline itself (B08.3, #752).

``run_agent_step`` is the one boundary every adapter goes through. These tests drive it with scripted adapters and a fake clock
(no sleep, no provider, no network) and show, for each of the three counted bounds and the deadline:

* a step that spends *exactly* its limit is within it, and one over stops with the limit's own typed reason, its proposal
  dropped (no protected effect) and its usage kept;
* a step with no turn or token to spend, or whose deadline has passed, never reaches its adapter;
* the platform counts tool calls itself (a read tool past the limit is refused, and an adapter that under-reports or ignores the
  refusal is still held to it) and reads the limits from the request it validated, never from anything the adapter holds;
* malformed usage is a typed failure, and nothing here carries state between calls (no double counting on a retry).
"""

from __future__ import annotations

import contextlib
import inspect
import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from caliber.config import (
    AGENT_STEP_MAX_SECONDS_BOUNDS,
    AGENT_STEP_MAX_TOKENS_BOUNDS,
    AGENT_STEP_MAX_TOOL_CALLS_BOUNDS,
    AGENT_STEP_MAX_TURNS_BOUNDS,
    CaliberConfig,
    ConfigError,
)
from caliber.contracts.agent_step import AgentStepRequest, AgentStepResult, StepLimits, StepUsage
from caliber.workflows.agent_step import (
    LIMIT_STOPS,
    MALFORMED_RESULT,
    AgentStepInput,
    StepCeilings,
    StepClock,
    StepDeadlineReached,
    StepHandoffTarget,
    StepToolBudgetExceeded,
    _ToolMeter,
    answer,
    before_work_stop,
    cap_limits,
    resolve_clock,
    run_agent_step,
)
from caliber.workflows.ir import IRAgent, IRToolBinding, NodeType
from tests.agent_step_conformance import (
    EXECUTOR_NAMES,
    FIXED_NOW,
    READ_TOOL,
    SECRET_MARKER,
    FakeStepClock,
    StepContext,
    every_executor,
    limits_changes,
    make_context,
    stop_before_work,
)


class Recorder:
    """A scripted adapter: it records what it was given and answers with ``behave(request, step_input)``."""

    def __init__(self, behave: Callable[[AgentStepRequest, AgentStepInput], Any]) -> None:
        self.behave = behave
        self.calls: list[tuple[AgentStepRequest, AgentStepInput]] = []

    def run_step(self, request: AgentStepRequest, step_input: AgentStepInput) -> Any:
        self.calls.append((request, step_input))
        return self.behave(request, step_input)


def usage(*, turns: int = 1, tool_calls: int = 0, tokens: int = 100) -> StepUsage:
    return StepUsage(
        prompt_tokens=tokens,
        completion_tokens=0,
        total_tokens=tokens,
        turns=turns,
        tool_calls=tool_calls,
    )


def completing(**figures: int) -> Recorder:
    return Recorder(
        lambda request, _input: answer(
            request, "completed", usage=usage(**figures), usage_source="provider"
        )
    )


def proposing(ctx: StepContext, **figures: int) -> Recorder:
    return Recorder(
        lambda request, _input: answer(
            request,
            "proposed_action",
            usage=usage(**figures),
            proposed_actions=(ctx.protected_action(),),
            usage_source="provider",
        )
    )


def run(adapter: object, ctx: StepContext, clock: StepClock | None = None) -> AgentStepResult:
    return run_agent_step(adapter, ctx.request, ctx.step_input, clock=clock or ctx.clock)


def context(**limits: Any) -> StepContext:
    return make_context(**limits_changes(**limits))


# --------------------------------------------------------------------------------------------------------------------
# turns, tool calls and tokens: exact limit, one over
# --------------------------------------------------------------------------------------------------------------------

BOUNDS = [
    pytest.param("max_turns", "turns", id="turns"),
    pytest.param("max_tool_calls", "tool_calls", id="tool_calls"),
    pytest.param("max_tokens", "tokens", id="tokens"),
]


def figures_at(figure: str, value: int) -> dict[str, int]:
    """Usage that is ``value`` on the bound under test and well inside every other bound."""
    base = {"turns": 1, "tool_calls": 0, "tokens": 100}
    base[figure] = value
    return base


@pytest.mark.parametrize(("limit_name", "figure"), BOUNDS)
def test_a_step_that_spends_exactly_its_limit_is_within_it(limit_name: str, figure: str) -> None:
    ctx = context(**{limit_name: 3})
    reported = figures_at(figure, 3)
    result = run(proposing(ctx, **reported), ctx)
    assert result.stop_reason == "proposed_action"
    assert len(result.proposed_actions) == 1
    assert result.usage == usage(**reported)


@pytest.mark.parametrize(("limit_name", "figure"), BOUNDS)
@pytest.mark.parametrize("scripted", ["completed", "proposed_action"])
def test_a_step_one_over_its_limit_stops_with_the_limits_reason_and_no_proposal(
    limit_name: str, figure: str, scripted: str
) -> None:
    ctx = context(**{limit_name: 3})
    reported = figures_at(figure, 4)
    adapter = (
        proposing(ctx, **reported) if scripted == "proposed_action" else completing(**reported)
    )
    result = run(adapter, ctx)
    assert result.stop_reason == limit_name
    assert result.stop_reason in LIMIT_STOPS
    assert result.proposed_actions == (), "a step that overran never gets to propose"
    assert result.error_code is None
    assert result.usage == usage(**reported), "the usage it reported is recorded"
    assert result.usage_source == "provider"
    assert len(adapter.calls) == 1, "the limit was passed after the work, not before it"
    assert ctx.protected_calls == []
    assert result.record()["stop_reason"] == limit_name


def test_several_limits_passed_name_the_first_in_a_fixed_order() -> None:
    ctx = context(max_turns=1, max_tool_calls=1, max_tokens=1)
    result = run(completing(turns=5, tool_calls=5, tokens=5), ctx)
    assert result.stop_reason == "max_turns"
    ctx = context(max_turns=9, max_tool_calls=1, max_tokens=1)
    assert run(completing(turns=5, tool_calls=5, tokens=5), ctx).stop_reason == "max_tool_calls"
    ctx = context(max_turns=9, max_tool_calls=9, max_tokens=1)
    assert run(completing(turns=5, tool_calls=5, tokens=5), ctx).stop_reason == "max_tokens"


@pytest.mark.parametrize("stop", ["max_turns", "max_tool_calls", "max_tokens", "deadline"])
def test_a_stop_the_adapter_reports_itself_is_kept_even_inside_its_limits(stop: str) -> None:
    ctx = context()
    adapter = Recorder(
        lambda request, _input: answer(request, stop, usage=usage(), usage_source="provider")
    )
    assert run(adapter, ctx).stop_reason == stop


@pytest.mark.parametrize("scripted", ["refused", "error"])
def test_a_refusal_or_failure_is_returned_as_it_is_whatever_usage_it_reports(
    scripted: str,
) -> None:
    ctx = context(max_tokens=3)
    adapter = Recorder(
        lambda request, _input: answer(
            request,
            scripted,
            usage=usage(tokens=500),
            usage_source="provider",
            error_code="provider_said_no",
        )
    )
    result = run(adapter, ctx)
    assert result.stop_reason == scripted and result.error_code == "provider_said_no"
    assert result.usage == usage(tokens=500)


# --------------------------------------------------------------------------------------------------------------------
# zero budget and an expired deadline never reach the adapter
# --------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("limits", "expected"),
    [
        ({"max_turns": 0}, "max_turns"),
        ({"max_tokens": 0}, "max_tokens"),
        ({"max_turns": 0, "max_tokens": 0}, "max_turns"),
        ({"deadline": "2026-10-26T09:00:00Z"}, "deadline"),
        ({"deadline": "2026-10-26T08:59:59Z"}, "deadline"),
        ({"deadline": "2026-10-26T09:00:00.000001Z"}, None),
        ({"max_tool_calls": 0}, None),
        ({"max_turns": 1, "max_tokens": 100}, None),
    ],
)
def test_a_step_with_nothing_to_spend_stops_before_the_adapter(
    limits: dict[str, Any], expected: str | None
) -> None:
    ctx = context(**limits)
    adapter = completing()
    result = run(adapter, ctx)
    if expected is None:
        assert result.stop_reason == "completed" and len(adapter.calls) == 1
        assert before_work_stop(ctx.request, clock=ctx.clock) is None
    else:
        assert adapter.calls == [], "the adapter was not called"
        assert result.stop_reason == expected
        assert result.usage == StepUsage(0, 0, 0, 0, 0)
        assert result.usage_source == "estimated"
        assert result.proposed_actions == () and result.error_code is None
        assert result == before_work_stop(ctx.request, clock=ctx.clock)


def test_a_deadline_in_another_offset_is_the_same_instant() -> None:
    # 09:00:00Z is 10:00:00+01:00 and 04:00:00-05:00: the same deadline, expired exactly now.
    for deadline in ("2026-10-26T10:00:00+01:00", "2026-10-26T04:00:00-05:00"):
        ctx = context(deadline=deadline)
        assert stop_before_work(completing(), ctx, "deadline").stop_reason == "deadline"


@pytest.mark.parametrize("name", EXECUTOR_NAMES)
@pytest.mark.parametrize(
    ("limits", "expected"),
    [
        ({"max_turns": 0}, "max_turns"),
        ({"max_tokens": 0}, "max_tokens"),
        ({"deadline": "2026-10-26T08:00:00Z"}, "deadline"),
    ],
    ids=["no_turns", "no_tokens", "expired"],
)
def test_every_executor_is_stopped_before_work_by_the_platform(
    name: str, limits: dict[str, Any], expected: str
) -> None:
    """The five executors, each over a tripwire client that fails the test if the provider is touched: the boundary stops the
    step for all of them equally, whether or not the adapter integrates a governed step."""
    ctx = context(**limits)
    stop_before_work(every_executor()[name], ctx, expected)


# --------------------------------------------------------------------------------------------------------------------
# the deadline, on a fake clock
# --------------------------------------------------------------------------------------------------------------------


def slow(clock: FakeStepClock, seconds: float, inner: Recorder) -> Recorder:
    """An adapter that takes ``seconds`` on the clock (no sleeping) and then answers like ``inner``."""

    def behave(request: AgentStepRequest, step_input: AgentStepInput) -> Any:
        clock.advance(seconds)
        return inner.behave(request, step_input)

    return Recorder(behave)


def test_a_step_that_finishes_exactly_at_its_deadline_is_within_it() -> None:
    ctx = context()
    clock = FakeStepClock()
    result = run(slow(clock, 600, proposing(ctx)), ctx, clock.step_clock())
    assert result.stop_reason == "proposed_action"


def test_a_step_that_finishes_one_microsecond_after_its_deadline_stops_with_no_proposal() -> None:
    ctx = context()
    clock = FakeStepClock()
    adapter = slow(clock, 600.000001, proposing(ctx, tokens=77))
    result = run(adapter, ctx, clock.step_clock())
    assert result.stop_reason == "deadline"
    assert result.proposed_actions == () and result.error_code is None
    assert result.usage == usage(tokens=77), "what it used is recorded"
    assert len(adapter.calls) == 1


def test_the_deadline_is_held_by_the_monotonic_clock_not_by_a_wall_clock_that_was_stepped_back() -> (
    None
):
    ctx = context()
    clock = FakeStepClock()

    def behave(request: AgentStepRequest, _input: AgentStepInput) -> Any:
        clock.mono += 700  # the step took 700 seconds
        clock.wall -= timedelta(hours=1)  # and the wall clock was set back an hour meanwhile
        return answer(request, "completed", usage=usage(), usage_source="provider")

    assert run(Recorder(behave), ctx, clock.step_clock()).stop_reason == "deadline"


def test_a_wall_clock_stepped_forward_does_not_cut_a_quick_step_short() -> None:
    ctx = context()
    clock = FakeStepClock()

    def behave(request: AgentStepRequest, _input: AgentStepInput) -> Any:
        clock.mono += 1  # one second of real time
        clock.wall += timedelta(hours=1)  # an NTP step forward
        return answer(request, "completed", usage=usage(), usage_source="provider")

    assert run(Recorder(behave), ctx, clock.step_clock()).stop_reason == "completed"


def test_a_failed_step_is_not_turned_into_a_deadline_stop() -> None:
    ctx = context()
    clock = FakeStepClock()

    def behave(request: AgentStepRequest, _input: AgentStepInput) -> Any:
        clock.advance(10_000)
        raise TimeoutError

    result = run(Recorder(behave), ctx, clock.step_clock())
    assert result.stop_reason == "error" and result.error_code == "adapter_timeout"


def test_the_default_clock_is_the_systems_and_a_missing_clock_resolves_to_it() -> None:
    assert resolve_clock(None).now().tzinfo is timezone.utc
    mine = FakeStepClock().step_clock()
    assert resolve_clock(mine) is mine
    # no clock given: the real time is far past the fixture deadline of 2026-10-26 only after that date; use a far one
    ctx = context(deadline="2099-01-01T00:00:00Z")
    assert run_agent_step(completing(), ctx.request, ctx.step_input).stop_reason == "completed"
    expired = context(deadline="2000-01-01T00:00:00Z")
    assert run_agent_step(completing(), expired.request, expired.step_input).stop_reason == (
        "deadline"
    )


@pytest.mark.parametrize(
    "bad_now",
    [datetime(2026, 10, 26, 9, 0, 0), "2026-10-26T09:00:00Z", 1.0, None],
    ids=["naive", "text", "number", "none"],
)
def test_a_clock_that_is_not_an_aware_instant_is_refused_before_anything_runs(
    bad_now: Any,
) -> None:
    ctx = context()
    adapter = completing()
    with pytest.raises(ValueError, match="aware datetime"):
        run(adapter, ctx, StepClock(now=lambda: bad_now, monotonic=lambda: 0.0))
    assert adapter.calls == []


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "1", None, True])
def test_a_monotonic_clock_that_is_not_a_finite_number_is_refused(bad: Any) -> None:
    ctx = context()
    adapter = completing()
    clock = StepClock(now=lambda: FIXED_NOW, monotonic=lambda: bad)
    with pytest.raises(ValueError, match="finite number"):
        run(adapter, ctx, clock)
    assert adapter.calls == []


# --------------------------------------------------------------------------------------------------------------------
# tool calls are counted by the platform
# --------------------------------------------------------------------------------------------------------------------


def calling_read_tool(times: int, *, swallow: bool, report: int | None = None) -> Recorder:
    """An adapter that calls the read tool ``times`` times, and reports ``report`` calls (else the true count)."""

    def behave(request: AgentStepRequest, step_input: AgentStepInput) -> Any:
        tool = step_input.read_tools[READ_TOOL]
        made = 0
        for index in range(times):
            try:
                tool(case_id=f"case-{index}")
                made += 1
            except StepToolBudgetExceeded:
                if not swallow:
                    raise
        return answer(
            request,
            "completed",
            usage=usage(tool_calls=made if report is None else report),
            usage_source="provider",
        )

    return Recorder(behave)


def test_a_read_tool_within_the_limit_runs_and_is_reported() -> None:
    ctx = context(max_tool_calls=2)
    result = run(calling_read_tool(2, swallow=False), ctx)
    assert result.stop_reason == "completed" and result.usage.tool_calls == 2
    assert [call["case_id"] for call in ctx.read_calls] == ["case-0", "case-1"]


def test_a_read_tool_past_the_limit_is_refused_and_never_runs() -> None:
    ctx = context(max_tool_calls=2)
    adapter = calling_read_tool(3, swallow=False)
    result = run(adapter, ctx)
    # the refusal escaped the adapter: it is a bounded adapter failure, and the third call did not run
    assert result.stop_reason == "error" and result.error_code == "adapter_error"
    assert len(ctx.read_calls) == 2


def test_an_adapter_that_swallows_the_refusal_and_completes_is_stopped_at_the_limit() -> None:
    ctx = context(max_tool_calls=2)
    result = run(calling_read_tool(3, swallow=True), ctx)
    assert result.stop_reason == "max_tool_calls", "the call past the limit was attempted"
    assert len(ctx.read_calls) == 2, "and refused"
    assert result.proposed_actions == ()
    assert result.usage.tool_calls == 2


def test_an_adapter_that_swallows_the_refusal_and_proposes_cannot_propose() -> None:
    ctx = context(max_tool_calls=1)

    def behave(request: AgentStepRequest, step_input: AgentStepInput) -> Any:
        tool = step_input.read_tools[READ_TOOL]
        tool(case_id="a")
        with contextlib.suppress(StepToolBudgetExceeded):
            tool(case_id="b")
        return answer(
            request,
            "proposed_action",
            usage=usage(tool_calls=1),
            proposed_actions=(ctx.protected_action(),),
            usage_source="provider",
        )

    result = run(Recorder(behave), ctx)
    assert result.stop_reason == "max_tool_calls" and result.proposed_actions == ()


def test_an_adapter_that_under_reports_its_tool_calls_is_charged_the_platforms_count() -> None:
    ctx = context(max_tool_calls=4)
    result = run(calling_read_tool(3, swallow=False, report=0), ctx)
    assert result.stop_reason == "completed"
    assert result.usage.tool_calls == 3, "raised to what the platform counted"


def test_an_adapter_that_under_reports_past_the_limit_is_still_stopped() -> None:
    ctx = context(max_tool_calls=2)
    result = run(calling_read_tool(5, swallow=True, report=0), ctx)
    assert result.stop_reason == "max_tool_calls" and result.usage.tool_calls == 2


def test_a_tool_that_raises_still_counts_as_a_call() -> None:
    ctx = context(max_tool_calls=1)

    def behave(request: AgentStepRequest, step_input: AgentStepInput) -> Any:
        tool = step_input.read_tools[READ_TOOL]
        for _ in range(2):
            with contextlib.suppress(Exception):
                tool(case_id="a")
        return answer(request, "completed", usage=usage(), usage_source="provider")

    ctx.read_tool = lambda **_: (_ for _ in ()).throw(RuntimeError("tool down"))  # type: ignore[method-assign]
    assert run(Recorder(behave), ctx).stop_reason == "max_tool_calls"


def test_a_tool_limit_of_zero_refuses_every_call() -> None:
    ctx = context(max_tool_calls=0)
    result = run(calling_read_tool(1, swallow=True), ctx)
    assert result.stop_reason == "max_tool_calls" and ctx.read_calls == []


def test_the_tool_a_step_gets_keeps_its_signature_and_name() -> None:
    ctx = context()
    seen: list[Callable[..., Any]] = []

    def behave(request: AgentStepRequest, step_input: AgentStepInput) -> Any:
        seen.append(step_input.read_tools[READ_TOOL])
        return answer(request, "completed", usage=usage(), usage_source="provider")

    run(Recorder(behave), ctx)
    (wrapped,) = seen
    original = ctx.step_input.read_tools[READ_TOOL]
    assert wrapped is not original
    assert inspect.signature(wrapped) == inspect.signature(original)
    assert wrapped.__name__ == original.__name__


def test_racing_calls_cannot_both_take_the_last_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lock is what keeps it so, and the test makes the race certain instead of likely: a read of the call counter is held
    open until a second caller has read it too. Without the lock both read 0, both are allowed and both tools run; with it the
    second caller cannot read until the first has finished, so the first's wait times out and exactly one tool runs."""
    gate = threading.Barrier(2)
    racing = threading.Event()

    def read(self: Any) -> int:
        value = self.__dict__["_n"]
        if racing.is_set():
            with contextlib.suppress(threading.BrokenBarrierError):
                gate.wait(timeout=0.3)
        return value  # type: ignore[no-any-return]

    def write(self: Any, value: int) -> None:
        self.__dict__["_n"] = value

    monkeypatch.setattr(_ToolMeter, "_attempted", property(read, write), raising=False)
    ctx = context(max_tool_calls=1)
    outcomes: list[str] = []

    def behave(request: AgentStepRequest, step_input: AgentStepInput) -> Any:
        tool = step_input.read_tools[READ_TOOL]

        def worker() -> None:
            try:
                tool(case_id="x")
                outcomes.append("ran")
            except StepToolBudgetExceeded:
                outcomes.append("refused")

        racing.set()
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        racing.clear()
        return answer(request, "completed", usage=usage(tool_calls=1), usage_source="provider")

    result = run(Recorder(behave), ctx)
    assert sorted(outcomes) == ["ran", "refused"]
    assert len(ctx.read_calls) == 1
    assert result.stop_reason == "max_tool_calls"


# a read tool is not started at or after the deadline


def test_a_read_tool_is_refused_at_the_deadline_and_never_runs() -> None:
    ctx = context()
    clock = FakeStepClock()
    outcome: list[str] = []

    def behave(request: AgentStepRequest, step_input: AgentStepInput) -> Any:
        tool = step_input.read_tools[READ_TOOL]
        clock.advance(599.999999)
        tool(case_id="in-time")
        clock.advance(0.000001)  # the deadline, exactly
        try:
            tool(case_id="too-late")
        except StepDeadlineReached as exc:
            outcome.append(str(exc))
        return answer(request, "completed", usage=usage(tool_calls=1), usage_source="provider")

    result = run(Recorder(behave), ctx, clock.step_clock())
    assert [call["case_id"] for call in ctx.read_calls] == ["in-time"]
    assert outcome == ["the step's deadline is reached"], "a fixed text, no payload"
    assert result.stop_reason == "deadline", "an adapter that ignores the refusal is still stopped"
    assert result.proposed_actions == () and result.usage.tool_calls == 1


def test_a_read_tool_refused_at_the_deadline_is_not_counted_as_a_call() -> None:
    ctx = context(max_tool_calls=1)
    clock = FakeStepClock()

    def behave(request: AgentStepRequest, step_input: AgentStepInput) -> Any:
        tool = step_input.read_tools[READ_TOOL]
        clock.advance(601)
        with contextlib.suppress(StepDeadlineReached):
            tool(case_id="late")
        return answer(request, "completed", usage=usage(), usage_source="provider")

    result = run(Recorder(behave), ctx, clock.step_clock())
    assert ctx.read_calls == []
    assert result.stop_reason == "deadline", (
        "not max_tool_calls: the refused call was never counted"
    )
    assert result.usage.tool_calls == 0


def test_a_clock_stepped_forward_does_not_refuse_a_tool_the_monotonic_clock_allows() -> None:
    ctx = context()
    clock = FakeStepClock()

    def behave(request: AgentStepRequest, step_input: AgentStepInput) -> Any:
        clock.mono += 1
        clock.wall += timedelta(hours=1)
        step_input.read_tools[READ_TOOL](case_id="ok")
        return answer(request, "completed", usage=usage(tool_calls=1), usage_source="provider")

    assert run(Recorder(behave), ctx, clock.step_clock()).stop_reason == "completed"
    assert len(ctx.read_calls) == 1


def test_the_tools_of_a_handoff_target_share_the_steps_budget() -> None:
    ctx = context(max_tool_calls=1)
    target_agent = IRAgent(
        node_id="other",
        node_type=NodeType.AGENT,
        name="Other",
        tools=[
            IRToolBinding(
                local_name=READ_TOOL,
                registry_ref=READ_TOOL,
                version_constraint="*",
                requires_approval=False,
                side_effect_level="read",
                allow_in_preview=False,
                module_path=__name__,
                callable_name="unused",
            )
        ],
    )
    target_calls: list[dict[str, Any]] = []

    def target_tool(**arguments: Any) -> str:
        target_calls.append(arguments)
        return "data"

    step_input = AgentStepInput(
        agent=ctx.step_input.agent,
        input_text="x",
        read_tools=ctx.step_input.read_tools,
        handoff_agents={"other": StepHandoffTarget(target_agent, {READ_TOOL: target_tool})},
    )

    def behave(request: AgentStepRequest, given: AgentStepInput) -> Any:
        assert given.handoff_agents is not None
        given.read_tools[READ_TOOL](case_id="own")
        with pytest.raises(StepToolBudgetExceeded):
            given.handoff_agents["other"].read_tools[READ_TOOL](case_id="theirs")
        return answer(request, "completed", usage=usage(tool_calls=1), usage_source="provider")

    result = run_agent_step(Recorder(behave), ctx.request, step_input, clock=ctx.clock)
    assert result.stop_reason == "max_tool_calls", (
        "the refused attempt on the target's tool was counted"
    )
    assert target_calls == [] and len(ctx.read_calls) == 1


# --------------------------------------------------------------------------------------------------------------------
# an adapter cannot raise its own limits
# --------------------------------------------------------------------------------------------------------------------


def test_an_adapter_that_rewrites_the_limits_of_its_request_changes_nothing() -> None:
    ctx = context(max_tokens=50)

    def behave(request: AgentStepRequest, _input: AgentStepInput) -> Any:
        object.__setattr__(request.limits, "max_tokens", 10**9)
        object.__setattr__(request.limits, "deadline", "2099-01-01T00:00:00Z")
        object.__setattr__(request, "limits", StepLimits(99, 99, 10**9, "2099-01-01T00:00:00Z"))
        return answer(request, "completed", usage=usage(tokens=51), usage_source="provider")

    adapter = Recorder(behave)
    result = run(adapter, ctx)
    assert result.stop_reason == "max_tokens"
    assert adapter.calls[0][0] is not ctx.request, "the adapter's request is its own copy"
    assert ctx.request.limits.max_tokens == 50, "the caller's request is untouched"


def test_a_result_cannot_carry_a_limit_so_there_is_nothing_to_raise_through_it() -> None:
    ctx = context(max_tokens=50)
    adapter = Recorder(
        lambda request, _input: {"limits": {"max_tokens": 10**9}, "stop_reason": "completed"}
    )
    result = run(adapter, ctx)
    assert result.stop_reason == "error" and result.error_code == MALFORMED_RESULT


# --------------------------------------------------------------------------------------------------------------------
# malformed provider usage
# --------------------------------------------------------------------------------------------------------------------


def corrupted(**changes: Any) -> Recorder:
    """An adapter whose result carries ``changes`` on its usage, written past the dataclass's own checks."""

    def behave(request: AgentStepRequest, _input: AgentStepInput) -> Any:
        result = answer(request, "completed", usage=usage(), usage_source="provider")
        for name, value in changes.items():
            object.__setattr__(result.usage, name, value)
        return result

    return Recorder(behave)


@pytest.mark.parametrize(
    "changes",
    [
        {"total_tokens": True},
        {"turns": 1.0},
        {"tool_calls": "1"},
        {"total_tokens": -1},
        {"turns": -1},
        {"total_tokens": 2**31},
        {"turns": 2**40},
        {"total_tokens": float("nan")},
        {"prompt_tokens": 500, "total_tokens": 100},
        {"cached_prompt_tokens": 500},
    ],
    ids=[
        "bool_total",
        "float_turns",
        "text_calls",
        "negative_total",
        "negative_turns",
        "total_over_int32",
        "turns_over_int32",
        "nan_total",
        "total_below_parts",
        "cached_over_prompt",
    ],
)
def test_malformed_provider_usage_is_a_typed_failure_with_no_proposal(
    changes: dict[str, Any],
) -> None:
    ctx = context()
    result = run(corrupted(**changes), ctx)
    assert result.stop_reason == "error" and result.error_code == MALFORMED_RESULT
    assert result.proposed_actions == ()
    assert result.usage == StepUsage(0, 0, 0, 0, 0) and result.usage_source == "unknown"


def test_usage_that_contradicts_itself_cannot_even_be_built() -> None:
    with pytest.raises(ValueError):
        StepUsage(prompt_tokens=10, completion_tokens=10, total_tokens=5, turns=1, tool_calls=0)


# --------------------------------------------------------------------------------------------------------------------
# nothing is carried between calls: a retry is a new attempt with a fresh budget
# --------------------------------------------------------------------------------------------------------------------


def test_a_second_call_with_the_same_request_gets_a_fresh_tool_budget_and_a_fresh_result() -> None:
    ctx = context(max_tool_calls=2)
    adapter = calling_read_tool(2, swallow=False)
    first = run(adapter, ctx)
    second = run(adapter, ctx)
    assert first == second and first.stop_reason == "completed"
    assert len(ctx.read_calls) == 4, "each call ran its own two"
    assert second.usage.tool_calls == 2, "not four: nothing accumulates across calls"


def test_a_limit_stop_does_not_poison_the_next_call() -> None:
    ctx = context(max_tokens=50)
    over = completing(tokens=51)
    assert run(over, ctx).stop_reason == "max_tokens"
    assert run(completing(tokens=50), ctx).stop_reason == "completed"


# --------------------------------------------------------------------------------------------------------------------
# no payload in a stop or a log
# --------------------------------------------------------------------------------------------------------------------


def test_a_limit_stop_logs_numbers_and_ids_only(caplog: pytest.LogCaptureFixture) -> None:
    ctx = context(max_tokens=10)
    action = ctx.protected_action()
    assert SECRET_MARKER not in repr(action)
    adapter = Recorder(
        lambda request, _input: answer(
            request,
            "proposed_action",
            usage=usage(tokens=11),
            proposed_actions=(
                type(action)(
                    connector="document-request",
                    connector_version="1.0.0",
                    effect_class=action.effect_class,
                    resource="cases/CASE-1/document-requests",
                    args={"note": SECRET_MARKER},
                ),
            ),
            usage_source="provider",
        )
    )
    with caplog.at_level(logging.DEBUG, logger="caliber"):
        result = run(adapter, ctx)
    assert result.stop_reason == "max_tokens"
    assert SECRET_MARKER not in repr(result.record())
    assert SECRET_MARKER not in caplog.text
    assert "stopped at max_tokens" in caplog.text and "tokens=11" in caplog.text


# --------------------------------------------------------------------------------------------------------------------
# ceilings: what a producer's limits are brought down to
# --------------------------------------------------------------------------------------------------------------------

NOW = datetime(2026, 10, 26, 9, 0, 0, tzinfo=timezone.utc)


def test_the_default_ceilings_are_the_settings_defaults() -> None:
    config = CaliberConfig()
    assert StepCeilings() == StepCeilings.from_config(config)
    assert (config.agent_step_max_turns, config.agent_step_max_tool_calls) == (25, 50)
    assert (config.agent_step_max_tokens, config.agent_step_max_seconds) == (200_000, 600.0)


def test_a_count_over_its_ceiling_is_brought_down_and_one_under_is_kept() -> None:
    ceilings = StepCeilings(max_turns=5, max_tool_calls=6, max_tokens=7, max_seconds=60)
    over = StepLimits(50, 60, 70, "2026-10-26T09:00:30Z")
    assert cap_limits(over, ceilings, now=NOW) == StepLimits(5, 6, 7, "2026-10-26T09:00:30Z")
    under = StepLimits(1, 0, 3, "2026-10-26T09:00:30Z")
    assert cap_limits(under, ceilings, now=NOW) == under
    exact = StepLimits(5, 6, 7, "2026-10-26T09:01:00Z")
    assert cap_limits(exact, ceilings, now=NOW) == exact


def test_a_deadline_beyond_the_window_is_brought_forward_and_an_earlier_one_is_untouched() -> None:
    ceilings = StepCeilings(max_seconds=60)
    far = StepLimits(1, 1, 1, "2099-01-01T00:00:00Z")
    assert cap_limits(far, ceilings, now=NOW).deadline == "2026-10-26T09:01:00.000000Z"
    # in another offset, the same instant is normalised to UTC
    offset = StepLimits(1, 1, 1, "2099-01-01T01:00:00+01:00")
    assert cap_limits(offset, ceilings, now=NOW).deadline == "2026-10-26T09:01:00.000000Z"
    early = StepLimits(1, 1, 1, "2026-10-26T09:00:10.5+00:00")
    assert cap_limits(early, ceilings, now=NOW).deadline == "2026-10-26T09:00:10.5+00:00"
    assert cap_limits(far, StepCeilings(max_seconds=1.5), now=NOW).deadline == (
        "2026-10-26T09:00:01.500000Z"
    )


def test_capping_never_raises_a_limit() -> None:
    limits = StepLimits(0, 0, 0, "2026-10-26T09:00:01Z")
    assert cap_limits(limits, StepCeilings(), now=NOW) == limits


@pytest.mark.parametrize("bad", ["x", object(), None, {"max_turns": 1}])
def test_only_step_limits_are_capped(bad: Any) -> None:
    with pytest.raises(TypeError):
        cap_limits(bad, StepCeilings(), now=NOW)


def test_a_deadline_that_is_not_an_instant_is_refused() -> None:
    with pytest.raises(ValueError):
        cap_limits(StepLimits(1, 1, 1, "tomorrow"), StepCeilings(), now=NOW)


@pytest.mark.parametrize(
    ("name", "bad"),
    [
        ("max_turns", 0),
        ("max_turns", AGENT_STEP_MAX_TURNS_BOUNDS[1] + 1),
        ("max_turns", True),
        ("max_turns", 1.0),
        ("max_tool_calls", -1),
        ("max_tool_calls", AGENT_STEP_MAX_TOOL_CALLS_BOUNDS[1] + 1),
        ("max_tokens", 0),
        ("max_tokens", AGENT_STEP_MAX_TOKENS_BOUNDS[1] + 1),
        ("max_tokens", "5"),
        ("max_seconds", 0.5),
        ("max_seconds", AGENT_STEP_MAX_SECONDS_BOUNDS[1] + 1),
        ("max_seconds", float("inf")),
        ("max_seconds", float("nan")),
        ("max_seconds", True),
        ("max_seconds", "60"),
    ],
)
def test_a_ceiling_outside_the_settings_bounds_is_refused_by_name(name: str, bad: Any) -> None:
    with pytest.raises(ValueError, match=name):
        StepCeilings(**{name: bad})


def test_the_bounds_themselves_are_accepted() -> None:
    assert StepCeilings(
        max_turns=AGENT_STEP_MAX_TURNS_BOUNDS[0],
        max_tool_calls=AGENT_STEP_MAX_TOOL_CALLS_BOUNDS[0],
        max_tokens=AGENT_STEP_MAX_TOKENS_BOUNDS[1],
        max_seconds=AGENT_STEP_MAX_SECONDS_BOUNDS[1],
    )


# --------------------------------------------------------------------------------------------------------------------
# the settings
# --------------------------------------------------------------------------------------------------------------------


def test_the_settings_read_from_the_environment_and_feed_the_ceilings() -> None:
    config = CaliberConfig.load(
        environ={
            "CALIBER_AGENT_STEP_MAX_TURNS": "3",
            "CALIBER_AGENT_STEP_MAX_TOOL_CALLS": "0",
            "CALIBER_AGENT_STEP_MAX_TOKENS": "1000",
            "CALIBER_AGENT_STEP_MAX_SECONDS": "12.5",
        }
    )
    assert StepCeilings.from_config(config) == StepCeilings(3, 0, 1000, 12.5)


@pytest.mark.parametrize(
    ("variable", "bad"),
    [
        ("CALIBER_AGENT_STEP_MAX_TURNS", "0"),
        ("CALIBER_AGENT_STEP_MAX_TURNS", "1001"),
        ("CALIBER_AGENT_STEP_MAX_TOOL_CALLS", "-1"),
        ("CALIBER_AGENT_STEP_MAX_TOOL_CALLS", "1001"),
        ("CALIBER_AGENT_STEP_MAX_TOKENS", "0"),
        ("CALIBER_AGENT_STEP_MAX_TOKENS", "10000001"),
        ("CALIBER_AGENT_STEP_MAX_SECONDS", "0.5"),
        ("CALIBER_AGENT_STEP_MAX_SECONDS", "86401"),
        ("CALIBER_AGENT_STEP_MAX_SECONDS", "inf"),
        ("CALIBER_AGENT_STEP_MAX_SECONDS", "nan"),
    ],
)
def test_a_setting_outside_its_bounds_stops_the_service_at_startup(variable: str, bad: str) -> None:
    with pytest.raises(ConfigError, match=variable.removeprefix("CALIBER_").lower()):
        CaliberConfig.load(environ={variable: bad})
