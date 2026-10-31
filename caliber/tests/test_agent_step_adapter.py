"""The AgentStepAdapter boundary and the scripted fake adapter (B08.1b, #720).

The boundary (``run_agent_step``) treats an adapter as untrusted: the request is checked before the adapter runs, and what
the adapter returns or raises is replaced by a typed, bounded result, never an exception and never text from the failure; a
result is read once, from one plain snapshot, and only when it is exactly the contract's own types. The scripted
``FakeWorkflowExecutor`` is deterministic and offline. The four provider executors refuse governed execution until their own
B08 tasks integrate them. ``tests/agent_step_conformance.py`` is the harness those tasks plug into; here it is exercised
through the fake, and each way an adapter could cheat it is shown to be caught.
"""

from __future__ import annotations

import asyncio
import dataclasses
import gc
import importlib
import json
import logging
import warnings
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from caliber.contracts import ContractValidationError, UnsupportedSchemaVersion
from caliber.contracts.agent_step import (
    AgentStepRequest,
    AgentStepResult,
    ProposedAction,
    StepLimits,
    StepUsage,
    parse_step_result,
)
from caliber.effects.records import ProposalError
from caliber.workflows.agent_step import (
    ADAPTER_ERROR,
    ADAPTER_NOT_INTEGRATED,
    ADAPTER_TIMEOUT,
    ADAPTER_UNSUPPORTED,
    MALFORMED_RESULT,
    NOT_CALLED_CODES,
    PROVIDER_ERROR,
    SCRIPT_EXHAUSTED,
    TOOL_ERROR,
    TOOL_NOT_READ_CLASS,
    AgentStepAdapter,
    AgentStepInput,
    ScriptedStep,
    StepHandoffTarget,
    StepScript,
    answer,
    failure,
    filter_read_tools,
    refusal,
    run_agent_step,
)
from caliber.workflows.ir import IRAgent, IRToolBinding, NodeType
from caliber.workflows.runtime import AgentStepAdapter as RuntimeAgentStepAdapter
from caliber.workflows.runtime import FakeWorkflowExecutor
from tests.agent_step_conformance import (
    PLATFORM_CODES,
    PROTECTED_TOOL,
    READ_TOOL,
    SCENARIOS,
    SECRET_MARKER,
    AdapterDriver,
    Scenario,
    StepContext,
    every_executor,
    make_context,
    run_scenario,
)

# The step fixtures carry a fixed deadline: the boundary reads its clock, not the date the suite runs on (B08.3).
pytestmark = pytest.mark.usefixtures("fixed_step_clock")


class Spy:
    """An adapter that returns whatever it is given (or raises it) and counts its calls."""

    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.calls: list[tuple[AgentStepRequest, AgentStepInput]] = []

    def run_step(self, request: AgentStepRequest, step_input: AgentStepInput) -> Any:
        self.calls.append((request, step_input))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        if callable(self.outcome):
            return self.outcome(request)
        return self.outcome


def run(adapter: object, ctx: StepContext) -> AgentStepResult:
    return run_agent_step(adapter, ctx.request, ctx.step_input)


def assert_typed_failure(result: AgentStepResult, ctx: StepContext, code: str, stop: str) -> None:
    """A bounded failure: the asked step's identity, a fixed code, no proposal, zero usage from an unknown source."""
    assert parse_step_result(result.record(), ctx.request) == result
    assert (result.stop_reason, result.error_code) == (stop, code)
    assert result.proposed_actions == ()
    assert result.usage == StepUsage(0, 0, 0, 0, 0)
    assert result.usage_source == "unknown"


# --------------------------------------------------------------------------------- the scripted fake driver


class FakeDriver:
    """The conformance driver for ``FakeWorkflowExecutor``: each scenario is a scripted outcome."""

    name = "fake"
    unsupported: frozenset[Scenario] = frozenset()

    def adapter_for(self, scenario: Scenario, ctx: StepContext) -> object:
        usage = ctx.staged_usage
        entries: dict[Scenario, Any] = {
            Scenario.COMPLETES: ScriptedStep("completed", usage=usage, usage_source="provider"),
            Scenario.READ_TOOL_CALL: ScriptedStep(
                "completed",
                usage=usage,
                usage_source="provider",
                read_tool_calls=((READ_TOOL, ctx.staged_read_args),),
            ),
            Scenario.PROPOSES_PROTECTED_TOOL: ScriptedStep(
                "proposed_action", (ctx.protected_action(),), usage, "provider"
            ),
            Scenario.MODEL_LIES_ABOUT_EFFECT_CLASS: ScriptedStep(
                "proposed_action", (ctx.protected_action("read"),), usage, "provider"
            ),
            Scenario.BUDGET_EXHAUSTED: ScriptedStep(
                "max_tokens", usage=usage, usage_source="provider"
            ),
            Scenario.PROVIDER_ERROR: RuntimeError(f"upstream said {SECRET_MARKER}"),
            Scenario.PROVIDER_TIMEOUT: TimeoutError(SECRET_MARKER),
            Scenario.UNREADABLE_OUTPUT: lambda request: {"note": SECRET_MARKER},
            Scenario.CLAIMS_COMMIT: lambda request: ctx.valid_record(
                state="committed", receipt_id=SECRET_MARKER
            ),
            Scenario.ANOTHER_STEPS_RESULT: lambda request: ctx.valid_record(
                occurrence_id="occ-of-another-step"
            ),
        }

        class Counting(FakeWorkflowExecutor):
            """The script is the fake's stubbed provider: consuming an entry is the provider call."""

            def run_step(
                self, request: AgentStepRequest, step_input: AgentStepInput
            ) -> AgentStepResult:
                ctx.provider_called()
                return super().run_step(request, step_input)

        return Counting(step_script=[entries[scenario]])


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_the_fake_conforms_to_the_shared_harness(scenario: Scenario) -> None:
    driver: AdapterDriver = FakeDriver()
    result, ctx = run_scenario(driver, scenario)
    assert result.step_request_id == ctx.request.step_request_id


# ---- the harness is not hollow: each way an adapter can cheat is caught


def adapter_driver(build: Callable[[Scenario, StepContext], object]) -> AdapterDriver:
    """A driver whose adapter is whatever ``build`` makes."""

    class Custom(FakeDriver):
        def adapter_for(self, scenario: Scenario, ctx: StepContext) -> object:
            return build(scenario, ctx)

    return Custom()


def reaching_provider(
    ctx: StepContext, respond: Callable[[AgentStepRequest], AgentStepResult]
) -> Spy:
    """An adapter whose stubbed provider is reached (its counter moves) before ``respond`` answers."""

    def reach(request: AgentStepRequest) -> AgentStepResult:
        ctx.provider_called()
        return respond(request)

    return Spy(reach)


def completing(ctx: StepContext, request: AgentStepRequest) -> AgentStepResult:
    return answer(request, "completed", usage=ctx.staged_usage, usage_source="provider")


def test_the_harness_catches_a_driver_that_never_reaches_its_provider() -> None:
    """A canned result with no provider path (the stub's counter never moves) is not a passing adapter."""
    driver = adapter_driver(lambda scenario, ctx: Spy(lambda request: completing(ctx, request)))
    with pytest.raises(AssertionError, match="never reached"):
        run_scenario(driver, Scenario.COMPLETES)


def test_the_harness_catches_an_adapter_that_runs_the_protected_tool_from_the_registry() -> None:
    """A lazy adapter that runs whatever the model named from the unfiltered registry trips the canary."""

    def build(scenario: Scenario, ctx: StepContext) -> object:
        def respond(request: AgentStepRequest) -> AgentStepResult:
            ctx.tool_registry[PROTECTED_TOOL](**ctx.staged_args)
            return completing(ctx, request)

        return reaching_provider(ctx, respond)

    with pytest.raises(AssertionError, match="executed a protected tool"):
        run_scenario(adapter_driver(build), Scenario.COMPLETES)


def test_the_harness_catches_an_adapter_that_resolves_the_protected_tool_from_its_binding() -> None:
    """The agent's own tool binding points at the canary: an adapter that imports what the binding names is caught."""

    def build(scenario: Scenario, ctx: StepContext) -> object:
        def respond(request: AgentStepRequest) -> AgentStepResult:
            binding = next(b for b in ctx.step_input.agent.tools if b.local_name == PROTECTED_TOOL)
            tool = getattr(importlib.import_module(binding.module_path), binding.callable_name)
            tool(**ctx.staged_args)
            return completing(ctx, request)

        return reaching_provider(ctx, respond)

    with pytest.raises(AssertionError, match="executed a protected tool"):
        run_scenario(adapter_driver(build), Scenario.COMPLETES)


@pytest.mark.parametrize(
    "dodged",
    [Scenario.COMPLETES, Scenario.PROPOSES_PROTECTED_TOOL, Scenario.CLAIMS_COMMIT],
)
def test_the_harness_does_not_let_a_driver_declare_a_core_scenario_unsupported(
    dodged: Scenario,
) -> None:
    class Dodging(FakeDriver):
        unsupported = frozenset({dodged})

    with pytest.raises(AssertionError, match="may not declare"):
        run_scenario(Dodging(), Scenario.READ_TOOL_CALL)


def test_the_harness_catches_an_adapter_that_leaks_provider_text() -> None:
    def build(scenario: Scenario, ctx: StepContext) -> object:
        return reaching_provider(
            ctx, lambda request: answer(request, "error", error_code=SECRET_MARKER)
        )

    with pytest.raises(AssertionError, match="carries provider text"):
        run_scenario(adapter_driver(build), Scenario.PROVIDER_ERROR)


def test_the_harness_sees_a_debug_log_that_leaks_provider_text() -> None:
    """Loggers are lowered to DEBUG for the run, so a leak at debug level is caught; the levels are then restored."""
    root, caliber = logging.getLogger(), logging.getLogger("caliber")
    before = (root.level, caliber.level)

    def build(scenario: Scenario, ctx: StepContext) -> object:
        def leak(request: AgentStepRequest) -> AgentStepResult:
            # Keep the deliberately leaked fixture local so CodeQL does not treat the
            # conformance harness's secret marker as a production secret.
            provider_text = "provider-" + "payload-marker-" + "7f3a"
            logging.getLogger("caliber.some.adapter").debug("provider said %s", provider_text)
            return failure(request, PROVIDER_ERROR)

        return reaching_provider(ctx, leak)

    with pytest.raises(AssertionError, match="a log record carries provider text"):
        run_scenario(adapter_driver(build), Scenario.PROVIDER_ERROR)
    assert (root.level, caliber.level) == before


def test_the_harness_skips_a_scenario_the_transport_cannot_stage() -> None:
    class Limited(FakeDriver):
        unsupported = frozenset({Scenario.ANOTHER_STEPS_RESULT})

    with pytest.raises(pytest.skip.Exception):
        run_scenario(Limited(), Scenario.ANOTHER_STEPS_RESULT)


@pytest.mark.parametrize("platform_code", sorted(PLATFORM_CODES))
def test_the_harness_does_not_accept_a_platform_code_as_a_provider_failure(
    platform_code: str,
) -> None:
    """An adapter that only falls through to one of the platform's own codes has not exercised its provider path."""

    def build(scenario: Scenario, ctx: StepContext) -> object:
        return reaching_provider(ctx, lambda request: failure(request, platform_code))

    with pytest.raises(AssertionError):
        run_scenario(adapter_driver(build), Scenario.PROVIDER_ERROR)


@pytest.mark.parametrize(
    "scenario",
    [
        Scenario.PROVIDER_TIMEOUT,
        Scenario.UNREADABLE_OUTPUT,
        Scenario.CLAIMS_COMMIT,
        Scenario.ANOTHER_STEPS_RESULT,
    ],
)
def test_the_harness_expects_the_specific_code_for_timeouts_and_unreadable_output(
    scenario: Scenario,
) -> None:
    def build(scenario: Scenario, ctx: StepContext) -> object:
        return reaching_provider(ctx, lambda request: failure(request, PROVIDER_ERROR))

    with pytest.raises(AssertionError):
        run_scenario(adapter_driver(build), scenario)


def test_the_harness_requires_the_adapter_itself_to_return_a_typed_failure() -> None:
    """The boundary would catch a raise; the adapter must not rely on it. Only its direct (second) call raises here."""
    calls: list[int] = []

    def build(scenario: Scenario, ctx: StepContext) -> object:
        def respond(request: AgentStepRequest) -> AgentStepResult:
            calls.append(1)
            if len(calls) > 1:
                raise RuntimeError(SECRET_MARKER)
            return failure(request, PROVIDER_ERROR)

        return reaching_provider(ctx, respond)

    with pytest.raises(pytest.fail.Exception, match="instead of returning a typed result"):
        run_scenario(adapter_driver(build), Scenario.PROVIDER_ERROR)


def test_the_harness_does_not_accept_an_adapter_that_raises_for_a_provider_error() -> None:
    def build(scenario: Scenario, ctx: StepContext) -> object:
        def respond(request: AgentStepRequest) -> AgentStepResult:
            raise RuntimeError("provider down")

        return reaching_provider(ctx, respond)

    # the boundary turns the raise into adapter_error, which is the platform's code, not the adapter's
    with pytest.raises(AssertionError):
        run_scenario(adapter_driver(build), Scenario.PROVIDER_ERROR)


@pytest.mark.parametrize(
    ("usage", "source"),
    [
        (StepUsage(0, 0, 0, 0, 0), "provider"),
        (StepUsage(1, 1, 2, 1, 0), "provider"),
        (None, "estimated"),
    ],
    ids=["zero_usage", "recounted_usage", "estimated_source"],
)
def test_the_harness_checks_usage_fidelity_on_completion(
    usage: StepUsage | None, source: str
) -> None:
    def build(scenario: Scenario, ctx: StepContext) -> object:
        return reaching_provider(
            ctx,
            lambda request: answer(
                request, "completed", usage=usage or ctx.staged_usage, usage_source=source
            ),
        )

    with pytest.raises(AssertionError):
        run_scenario(adapter_driver(build), Scenario.COMPLETES)


def test_the_harness_requires_a_proposal_to_derive_from_the_models_tool_call() -> None:
    def build(scenario: Scenario, ctx: StepContext) -> object:
        generic = dataclasses.replace(ctx.protected_action(), args={"document_type": "template"})
        return reaching_provider(
            ctx,
            lambda request: answer(
                request,
                "proposed_action",
                usage=ctx.staged_usage,
                usage_source="provider",
                proposed_actions=[generic],
            ),
        )

    with pytest.raises(AssertionError):
        run_scenario(adapter_driver(build), Scenario.PROPOSES_PROTECTED_TOOL)


def test_the_harness_requires_the_models_effect_class_claim_to_be_carried_unchanged() -> None:
    """An adapter that quietly rewrites a lie to the platform's class hides it; the claim is kept for B08.2 to refuse."""

    def build(scenario: Scenario, ctx: StepContext) -> object:
        return reaching_provider(
            ctx,
            lambda request: answer(
                request,
                "proposed_action",
                usage=ctx.staged_usage,
                usage_source="provider",
                proposed_actions=[ctx.protected_action()],
            ),
        )

    with pytest.raises(AssertionError):
        run_scenario(adapter_driver(build), Scenario.MODEL_LIES_ABOUT_EFFECT_CLASS)


def test_the_harness_requires_a_read_tool_call_to_run_through_read_tools() -> None:
    def build(scenario: Scenario, ctx: StepContext) -> object:
        return reaching_provider(ctx, lambda request: completing(ctx, request))

    with pytest.raises(AssertionError):
        run_scenario(adapter_driver(build), Scenario.READ_TOOL_CALL)


@pytest.mark.parametrize("source", ["unknown", "provider"])
def test_the_harness_requires_an_exhausted_budget_to_keep_the_providers_usage(source: str) -> None:
    """Zero usage fails whatever it claims as its source: the staged provider spent tokens."""

    def build(scenario: Scenario, ctx: StepContext) -> object:
        return reaching_provider(
            ctx, lambda request: answer(request, "max_tokens", usage_source=source)
        )

    with pytest.raises(AssertionError):
        run_scenario(adapter_driver(build), Scenario.BUDGET_EXHAUSTED)


# ------------------------------------------------------------------------------------------ the step input


def tool_agent(*names: str) -> IRAgent:
    return IRAgent(
        node_id="request_document",
        node_type=NodeType.AGENT,
        name="a",
        tools=[IRToolBinding(n, n, "*", False, "read", False, "m", "f") for n in names],
    )


def test_the_step_input_copies_what_it_is_given_into_read_only_containers() -> None:
    """What the platform built cannot be changed through what an adapter holds, or the reverse."""
    agent = tool_agent("a", "b")
    tools: dict[str, Callable[..., Any]] = {"a": lambda: 1}
    history = [{"role": "user", "content": "hi"}]
    step_input = AgentStepInput(agent, "text", history=tuple(history), read_tools=tools)
    tools["b"] = lambda: 2
    history[0]["content"] = "changed"
    assert set(step_input.read_tools) == {"a"}
    assert step_input.history == ({"role": "user", "content": "hi"},)
    with pytest.raises(TypeError):
        step_input.read_tools["b"] = lambda: 2  # type: ignore[index]
    with pytest.raises(dataclasses.FrozenInstanceError):
        step_input.read_tools = {}  # type: ignore[misc]


def test_the_step_input_refuses_a_read_tool_the_agent_does_not_have() -> None:
    with pytest.raises(ValueError, match="not a tool of agent"):
        AgentStepInput(tool_agent("a"), "t", read_tools={"other": lambda: 1})


@pytest.mark.parametrize("bad", [{"a": "not callable"}, ["a"], None])
def test_the_step_input_refuses_read_tools_that_are_not_callables(bad: Any) -> None:
    with pytest.raises(TypeError):
        AgentStepInput(tool_agent("a"), "t", read_tools=bad)


def test_handoff_targets_carry_only_their_own_read_class_callables() -> None:
    parent, target = tool_agent("a"), tool_agent("b", "write")
    target = dataclasses.replace(target, node_id="child")
    read_b = lambda: "data"
    step_input = AgentStepInput(
        parent,
        "t",
        handoff_agents={"child": StepHandoffTarget(target, {"b": read_b})},
    )
    assert step_input.handoff_agents is not None
    handed = step_input.handoff_agents["child"]
    assert dict(handed.read_tools) == {"b": read_b}
    with pytest.raises(TypeError):
        step_input.handoff_agents["x"] = handed  # type: ignore[index]
    with pytest.raises(TypeError):
        handed.read_tools["write"] = read_b  # type: ignore[index]


def test_a_handoff_target_cannot_carry_a_tool_the_target_lacks_or_the_runtimes_unfiltered_map() -> (
    None
):
    target = dataclasses.replace(tool_agent("b"), node_id="child")
    with pytest.raises(ValueError, match="not a tool of agent"):
        StepHandoffTarget(target, {"write": lambda: 1})
    # the runtime's own shape, a (agent, every-callable) tuple, is refused: the type is the guard
    with pytest.raises(TypeError, match="StepHandoffTarget"):
        AgentStepInput(
            tool_agent("a"),
            "t",
            handoff_agents={"child": (target, {"b": lambda: 1})},  # type: ignore[dict-item]
        )
    with pytest.raises(ValueError, match="names another agent"):
        AgentStepInput(
            tool_agent("a"),
            "t",
            handoff_agents={"someone_else": StepHandoffTarget(target)},
        )


def test_filter_read_tools_fails_closed() -> None:
    def tool() -> None:
        return None

    tools = {"read_it": tool, "write_it": tool, "boom": tool, "truthy": tool}

    def is_read(name: str) -> Any:
        if name == "boom":
            raise RuntimeError("classifier down")
        return {"read_it": True, "truthy": "yes"}.get(name, False)

    assert set(filter_read_tools(tools, is_read)) == {"read_it"}


def test_the_step_input_must_be_for_the_requests_node() -> None:
    ctx = make_context()
    other = AgentStepInput(dataclasses.replace(ctx.step_input.agent, node_id="another_node"), "t")
    spy = Spy(lambda request: completing(ctx, request))
    with pytest.raises(ContractValidationError, match="not the request's node"):
        run_agent_step(spy, ctx.request, other)
    assert spy.calls == []


# ------------------------------------------------------------------------------------------ the boundary


def test_a_conformant_adapter_is_called_once_with_the_validated_request() -> None:
    ctx = make_context()
    spy = Spy(lambda request: answer(request, "completed", usage_source="provider"))
    result = run(spy, ctx)
    assert result.stop_reason == "completed"
    ((seen_request, seen_input),) = spy.calls
    assert seen_request == ctx.request
    # The adapter is handed the same step, with its read tools going through the platform's own counter (B08.3).
    assert seen_input.agent is ctx.step_input.agent
    assert seen_input.input_text == ctx.step_input.input_text
    assert seen_input.history == ctx.step_input.history
    assert set(seen_input.read_tools) == set(ctx.step_input.read_tools)
    assert seen_input.read_tools[READ_TOOL] is not ctx.step_input.read_tools[READ_TOOL]


def test_the_protocol_is_the_one_runtime_exports() -> None:
    assert RuntimeAgentStepAdapter is AgentStepAdapter
    assert isinstance(FakeWorkflowExecutor(), AgentStepAdapter)
    assert not isinstance(object(), AgentStepAdapter)


@pytest.mark.parametrize("not_an_adapter", [object(), None, "run_step", {"run_step": 1}])
def test_an_object_without_run_step_is_refused_and_not_called(not_an_adapter: object) -> None:
    ctx = make_context()
    result = run(not_an_adapter, ctx)
    assert_typed_failure(result, ctx, ADAPTER_UNSUPPORTED, "refused")


def test_an_async_run_step_is_refused_and_its_coroutine_is_closed_not_left_unawaited() -> None:
    ctx = make_context()
    created: list[Any] = []

    class AsyncAdapter:
        async def run_step(self, request: AgentStepRequest, step_input: AgentStepInput) -> Any:
            raise AssertionError("an async adapter's body must never run")

    class Recording(AsyncAdapter):
        def run_step(self, request: AgentStepRequest, step_input: AgentStepInput) -> Any:  # type: ignore[override]
            coroutine = AsyncAdapter.run_step(self, request, step_input)
            created.append(coroutine)
            return coroutine

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        result = run(Recording(), ctx)
        gc.collect()
    assert_typed_failure(result, ctx, ADAPTER_UNSUPPORTED, "refused")
    assert result.error_code in NOT_CALLED_CODES
    (coroutine,) = created
    assert coroutine.cr_frame is None  # closed: it can no longer be awaited


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"attempt": 0}, id="attempt_zero"),
        pytest.param({"attempt": 1.5}, id="attempt_not_an_integer"),
        pytest.param({"step_request_id": ""}, id="empty_step_request_id"),
        pytest.param({"node_id": "   "}, id="blank_node_id"),
        pytest.param({"occurrence_id": "occ\x00"}, id="control_character_in_occurrence"),
        pytest.param({"run_manifest_digest": "A" * 64}, id="uppercase_manifest_digest"),
        pytest.param({"context_manifest_digest": "short"}, id="short_context_digest"),
        pytest.param({"grant_id": ""}, id="empty_grant"),
        pytest.param(
            {"limits": StepLimits(-1, 1, 1, "2026-10-26T09:10:00Z")}, id="negative_turn_limit"
        ),
        pytest.param({"limits": StepLimits(1, 1, 1, "tomorrow")}, id="deadline_not_rfc3339"),
    ],
)
def test_an_invalid_request_is_refused_before_the_adapter_runs(changes: dict[str, Any]) -> None:
    """The request is built by the platform but is a dataclass that can be built wrong: nothing has run when it raises."""
    ctx = make_context()
    bad = dataclasses.replace(ctx.request, **changes)
    spy = Spy(lambda request: answer(request, "completed"))
    with pytest.raises(ContractValidationError):
        run_agent_step(spy, bad, ctx.step_input)
    assert spy.calls == []


# ---- an adapter's answer is untrusted


@pytest.mark.parametrize(
    "changes",
    [
        {"step_request_id": "sr-of-another-step"},
        {"workflow_run_id": "run-of-another-step"},
        {"node_id": "another_node"},
        {"occurrence_id": "occ-of-another-step"},
        {"attempt": 2},
    ],
    ids=["step_request_id", "run", "node", "occurrence", "attempt"],
)
def test_a_result_for_another_step_is_never_returned(changes: dict[str, Any]) -> None:
    """Identity is bound both ways: any one field of (id, run, node, occurrence, attempt) off and the answer is dropped."""
    ctx = make_context()
    crossed = Spy(dataclasses.replace(answer(ctx.request, "completed"), **changes))
    result = run(crossed, ctx)
    assert_typed_failure(result, ctx, MALFORMED_RESULT, "error")
    assert (result.step_request_id, result.occurrence_id, result.attempt) == (
        ctx.request.step_request_id,
        ctx.request.occurrence_id,
        ctx.request.attempt,
    )


@pytest.mark.parametrize("returned", [None, {"stop_reason": "completed"}, "completed", 7, object()])
def test_a_return_that_is_not_a_result_is_malformed(returned: object) -> None:
    ctx = make_context()
    assert_typed_failure(run(Spy(returned), ctx), ctx, MALFORMED_RESULT, "error")


def test_a_result_that_is_schema_invalid_is_malformed() -> None:
    ctx = make_context()
    # an error_code is only allowed with error or refused
    bad = dataclasses.replace(answer(ctx.request, "completed"), error_code="why")
    assert_typed_failure(run(Spy(bad), ctx), ctx, MALFORMED_RESULT, "error")


def test_a_proposal_must_come_with_its_stop_reason_and_the_reverse() -> None:
    ctx = make_context()
    action = ctx.protected_action()
    no_action = answer(ctx.request, "proposed_action")
    hidden_action = answer(ctx.request, "completed", proposed_actions=[action])
    for bad in (no_action, hidden_action):
        assert_typed_failure(run(Spy(bad), ctx), ctx, MALFORMED_RESULT, "error")


def test_two_proposed_actions_are_refused_in_v1() -> None:
    ctx = make_context()
    other = dataclasses.replace(ctx.protected_action(), resource="cases/CASE-2/document-requests")
    two = answer(ctx.request, "proposed_action", proposed_actions=[ctx.protected_action(), other])
    assert_typed_failure(run(Spy(two), ctx), ctx, MALFORMED_RESULT, "error")


def test_inconsistent_usage_is_malformed_not_trusted() -> None:
    """A total below its parts understates the charge: a result built around the check is refused at the boundary."""
    ctx = make_context()
    usage = StepUsage(10, 5, 15, 1, 0)
    object.__setattr__(usage, "total_tokens", 1)
    understated = answer(ctx.request, "completed", usage=usage)
    assert_typed_failure(run(Spy(understated), ctx), ctx, MALFORMED_RESULT, "error")


def test_the_returned_action_is_the_models_claim_even_if_the_adapter_marked_it_bound() -> None:
    """``platform_bound`` is the platform's mark: an adapter cannot pass an action off as already bound."""
    ctx = make_context()
    action = ctx.protected_action()
    object.__setattr__(action, "platform_bound", True)
    result = run(Spy(answer(ctx.request, "proposed_action", proposed_actions=[action])), ctx)
    assert result.stop_reason == "proposed_action"
    assert result.proposed_actions[0].platform_bound is False
    assert result.proposed_actions[0] == action


class Hostile(dict):  # type: ignore[type-arg]
    """A record that answers differently once it has been iterated: valid for the first pass, a commit for the next."""

    def __init__(self, record: dict[str, Any]) -> None:
        super().__init__(record)
        self.armed = False
        self.reads_after_arming = 0

    def items(self) -> Any:
        self.armed = True
        return super().items()

    def __getitem__(self, key: str) -> Any:
        if self.armed:
            self.reads_after_arming += 1
            if key == "stop_reason":
                return "committed"
            if key == "error_code":
                return SECRET_MARKER
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        if self.armed:
            self.reads_after_arming += 1
            if key == "error_code":
                return SECRET_MARKER
        return super().get(key, default)


def test_the_result_is_read_once_from_a_plain_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """A record whose reads change after the validation pass cannot get a different answer through the parse pass."""
    ctx = make_context()
    hostile: list[Hostile] = []
    real_record = AgentStepResult.record

    def stateful_record(self: AgentStepResult) -> dict[str, Any]:
        record = Hostile(real_record(self))
        hostile.append(record)
        return record

    monkeypatch.setattr(AgentStepResult, "record", stateful_record)
    result = run(Spy(answer(ctx.request, "completed", usage_source="provider")), ctx)
    monkeypatch.undo()
    assert result.stop_reason == "completed"
    assert result.error_code is None
    assert SECRET_MARKER not in json.dumps(result.record())
    (record,) = hostile
    assert record.armed and record.reads_after_arming == 0


def test_a_record_that_is_not_a_json_object_is_malformed(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make_context()
    monkeypatch.setattr(AgentStepResult, "record", lambda self: ["not", "an", "object"])
    result = run(Spy(answer(ctx.request, "completed")), ctx)
    monkeypatch.undo()
    assert_typed_failure(result, ctx, MALFORMED_RESULT, "error")


def test_a_result_subclass_is_never_asked_for_its_record() -> None:
    """A subclass can override ``record()``: it is refused before any of its code runs."""
    ctx = make_context()
    asked: list[int] = []

    @dataclasses.dataclass(frozen=True)
    class Sneaky(AgentStepResult):
        def record(self) -> dict[str, Any]:
            asked.append(1)
            return {**super().record(), "stop_reason": "committed"}

    base = answer(ctx.request, "completed")
    sneaky = Sneaky(**{f.name: getattr(base, f.name) for f in dataclasses.fields(AgentStepResult)})
    assert_typed_failure(run(Spy(sneaky), ctx), ctx, MALFORMED_RESULT, "error")
    assert asked == []


def test_a_proposed_action_subclass_is_never_asked_for_its_record() -> None:
    ctx = make_context()
    asked: list[int] = []

    class Sneaky(ProposedAction):
        def record(self) -> dict[str, Any]:
            asked.append(1)
            return {**super().record(), "effect_class": "read"}

    real = ctx.protected_action()
    sneaky = Sneaky(
        connector=real.connector,
        connector_version=real.connector_version,
        effect_class=real.effect_class,
        resource=real.resource,
        args=dict(real.args),
    )
    result = answer(ctx.request, "proposed_action", proposed_actions=[sneaky])
    assert_typed_failure(run(Spy(result), ctx), ctx, MALFORMED_RESULT, "error")
    assert asked == []


def test_a_usage_subclass_is_never_trusted() -> None:
    ctx = make_context()

    class Sneaky(StepUsage):
        pass

    result = answer(ctx.request, "completed", usage=Sneaky(1, 1, 2, 1, 0))
    assert_typed_failure(run(Spy(result), ctx), ctx, MALFORMED_RESULT, "error")


class LyingText(str):
    """A ``str`` that compares equal to anything it is asked about, as a hostile field could."""

    __slots__ = ()

    def __eq__(self, other: object) -> bool:
        return True

    def __hash__(self) -> int:
        return 0


@pytest.mark.parametrize(
    "field",
    ["stop_reason", "step_request_id", "error_code"],
)
def test_a_field_that_is_not_exactly_a_plain_value_is_malformed(field: str) -> None:
    ctx = make_context()
    base = answer(
        ctx.request,
        "error" if field == "error_code" else "completed",
        error_code="x" if field == "error_code" else None,
    )
    tampered = dataclasses.replace(base, **{field: LyingText(getattr(base, field))})
    assert_typed_failure(run(Spy(tampered), ctx), ctx, MALFORMED_RESULT, "error")


@pytest.mark.parametrize(
    "tamper",
    [
        lambda r: dataclasses.replace(r, attempt=True),
        lambda r: dataclasses.replace(r, proposed_actions=[]),
        lambda r: dataclasses.replace(r, usage_source=b"unknown"),
        lambda r: dataclasses.replace(r, usage=StepUsage(True, 0, 1, 0, 0)),
    ],
    ids=["bool_attempt", "list_of_actions", "bytes_usage_source", "bool_usage_figure"],
)
def test_other_values_that_are_not_exactly_the_contracts_types_are_malformed(
    tamper: Callable[[AgentStepResult], AgentStepResult],
) -> None:
    ctx = make_context()
    assert_typed_failure(
        run(Spy(tamper(answer(ctx.request, "completed"))), ctx), ctx, MALFORMED_RESULT, "error"
    )


def test_a_dropped_result_logs_its_usage_numbers_and_nothing_else(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The figures an adapter reported on a result that is dropped are in the log (for B08.4's reconciliation), as numbers."""
    ctx = make_context()
    crossed = dataclasses.replace(
        answer(ctx.request, "completed", usage=StepUsage(10, 5, 15, 2, 1)),
        occurrence_id="occ-of-another-step",
    )
    with caplog.at_level(logging.DEBUG):
        run(Spy(crossed), ctx)
    assert "reported usage: prompt=10 completion=5 total=15 turns=2 tool_calls=1" in caplog.text
    assert "occ-of-another-step" not in caplog.text


def test_a_dropped_non_result_logs_no_usage(caplog: pytest.LogCaptureFixture) -> None:
    ctx = make_context()
    with caplog.at_level(logging.DEBUG):
        run(Spy({"usage": {"total_tokens": 999}}), ctx)
    assert "reported usage" not in caplog.text
    assert "returned an invalid result" in caplog.text


# ---- an adapter that fails: bounded, quiet, typed


@pytest.mark.parametrize(
    ("raised", "code"),
    [
        (RuntimeError(f"401 for key {SECRET_MARKER}"), ADAPTER_ERROR),
        (ConnectionError(SECRET_MARKER), ADAPTER_ERROR),
        (KeyError(SECRET_MARKER), ADAPTER_ERROR),
        (TimeoutError(SECRET_MARKER), ADAPTER_TIMEOUT),
        (asyncio.TimeoutError(SECRET_MARKER), ADAPTER_TIMEOUT),
        (ContractValidationError("agent_step_result", [SECRET_MARKER]), MALFORMED_RESULT),
        (UnsupportedSchemaVersion("agent_step_result", "v9", ("v1",)), MALFORMED_RESULT),
        (ProposalError("bad", SECRET_MARKER), MALFORMED_RESULT),
    ],
    ids=lambda value: value if isinstance(value, str) else type(value).__name__,
)
def test_an_adapter_that_raises_becomes_a_bounded_error_result(
    raised: Exception, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = make_context()
    with caplog.at_level(logging.DEBUG):
        result = run(Spy(raised), ctx)
    assert_typed_failure(result, ctx, code, "error")
    assert SECRET_MARKER not in json.dumps(result.record())
    assert SECRET_MARKER not in caplog.text
    # the exception's class is what is logged, with the step it belongs to
    assert type(raised).__name__ in caplog.text
    assert ctx.request.step_request_id in caplog.text


def test_a_base_exception_is_not_swallowed() -> None:
    """Interrupts and cancellation propagate: the provider call may have happened, and the caller keeps the reservation."""
    ctx = make_context()
    for interrupt in (
        KeyboardInterrupt(),
        SystemExit(1),
        asyncio.CancelledError(),
        GeneratorExit(),
    ):
        with pytest.raises(type(interrupt)):
            run(Spy(interrupt), ctx)


def test_only_a_step_whose_adapter_never_ran_may_release_its_reservation() -> None:
    ctx = make_context()
    assert {ADAPTER_UNSUPPORTED, ADAPTER_NOT_INTEGRATED} == NOT_CALLED_CODES
    kept = [
        run(Spy(RuntimeError("x")), ctx),
        run(Spy(TimeoutError("x")), ctx),
        run(Spy(None), ctx),
        run(FakeWorkflowExecutor(step_script=[RuntimeError("p")]), ctx),
    ]
    assert all(result.error_code not in NOT_CALLED_CODES for result in kept)
    released = [run(object(), ctx), run(every_executor()["openai_agents"], ctx)]
    assert released[0].error_code == ADAPTER_UNSUPPORTED
    assert released[1].stop_reason == "deadline"
    assert released[1].error_code is None


def test_a_failure_never_carries_a_proposal_or_invented_usage() -> None:
    ctx = make_context()
    result = run(Spy(RuntimeError("x")), ctx)
    assert result.usage == StepUsage(0, 0, 0, 0, 0)
    assert result.usage_source == "unknown"
    assert result.proposed_actions == ()


def test_adapter_helpers_copy_identity_from_the_request() -> None:
    ctx = make_context()
    request = ctx.request
    for built in (answer(request, "completed"), refusal(request, "x"), failure(request, "y")):
        assert (
            built.step_request_id,
            built.workflow_run_id,
            built.node_id,
            built.occurrence_id,
            built.attempt,
        ) == (
            request.step_request_id,
            request.workflow_run_id,
            request.node_id,
            request.occurrence_id,
            request.attempt,
        )
    assert refusal(request, "x").stop_reason == "refused"
    assert failure(request, "y").stop_reason == "error"
    kept = failure(request, "y", usage=StepUsage(3, 1, 4, 1, 0))
    assert kept.usage.total_tokens == 4 and kept.usage_source == "unknown"


# -------------------------------------------------------------------------- the scripted fake adapter


def test_without_a_script_every_step_completes_with_deterministic_estimated_usage() -> None:
    ctx = make_context()
    fake = FakeWorkflowExecutor()
    first, second = run(fake, ctx), run(fake, ctx)
    assert first == second
    words = len(ctx.step_input.input_text.split())
    assert (first.stop_reason, first.proposed_actions, first.usage_source) == (
        "completed",
        (),
        "estimated",
    )
    assert first.usage == StepUsage(words, 8, words + 8, 1, 0)
    assert fake.step_script.remaining is None
    assert fake.step_requests == [ctx.request, ctx.request]


def test_a_script_is_answered_in_order_and_then_refuses_loudly() -> None:
    ctx = make_context()
    script = [
        ScriptedStep("completed", usage=StepUsage(1, 1, 2, 1, 0), usage_source="provider"),
        ScriptedStep("max_tokens", usage=StepUsage(9, 9, 18, 3, 1)),
        ScriptedStep("refused", error_code="policy", usage_source="unknown"),
    ]
    fake = FakeWorkflowExecutor(step_script=script)
    assert fake.step_script.remaining == 3
    results = [run(fake, ctx) for _ in range(3)]
    assert [r.stop_reason for r in results] == ["completed", "max_tokens", "refused"]
    assert [r.usage.total_tokens for r in results] == [2, 18, 0]
    assert fake.step_script.remaining == 0
    exhausted = run(fake, ctx)
    assert_typed_failure(exhausted, ctx, SCRIPT_EXHAUSTED, "refused")


def test_an_empty_script_is_exhausted_not_the_default() -> None:
    ctx = make_context()
    assert_typed_failure(
        run(FakeWorkflowExecutor(step_script=[]), ctx), ctx, SCRIPT_EXHAUSTED, "refused"
    )


def test_a_scripted_proposal_is_returned_unexecuted_and_unbound() -> None:
    ctx = make_context()
    fake = FakeWorkflowExecutor(
        step_script=[ScriptedStep("proposed_action", (ctx.protected_action(),))]
    )
    result = run(fake, ctx)
    (action,) = result.proposed_actions
    assert action == ctx.protected_action()
    assert not action.platform_bound
    assert ctx.protected_calls == [] and ctx.read_calls == []


def test_the_fake_runs_only_the_read_tools_a_scripted_step_names_and_never_a_protected_one() -> (
    None
):
    ctx = make_context()
    fake = FakeWorkflowExecutor(step_script=[ScriptedStep("completed"), ScriptedStep("completed")])
    run(fake, ctx)
    run(FakeWorkflowExecutor(), ctx)
    assert ctx.read_calls == [] and ctx.protected_calls == []
    named = ScriptedStep("completed", read_tool_calls=((READ_TOOL, {"case_id": "c-1"}),))
    assert run(FakeWorkflowExecutor(step_script=[named]), ctx).stop_reason == "completed"
    assert ctx.read_calls == [{"case_id": "c-1"}]


def test_a_scripted_call_to_a_tool_outside_read_tools_is_refused_and_not_run() -> None:
    ctx = make_context()
    named = ScriptedStep("completed", read_tool_calls=((PROTECTED_TOOL, dict(ctx.staged_args)),))
    result = run(FakeWorkflowExecutor(step_script=[named]), ctx)
    assert_typed_failure(result, ctx, TOOL_NOT_READ_CLASS, "refused")
    assert ctx.protected_calls == []


def test_a_read_tool_that_raises_is_a_typed_failure_not_an_exception() -> None:
    ctx = make_context()

    def broken(**arguments: Any) -> None:
        raise RuntimeError(SECRET_MARKER)

    step_input = AgentStepInput(ctx.step_input.agent, "t", read_tools={READ_TOOL: broken})
    fake = FakeWorkflowExecutor(
        step_script=[ScriptedStep("completed", read_tool_calls=((READ_TOOL, {}),))]
    )
    result = fake.run_step(ctx.request, step_input)
    assert_typed_failure(result, ctx, TOOL_ERROR, "error")


@pytest.mark.parametrize(
    ("staged", "code"),
    [
        (TimeoutError(SECRET_MARKER), ADAPTER_TIMEOUT),
        (asyncio.TimeoutError(), ADAPTER_TIMEOUT),
        (RuntimeError(SECRET_MARKER), PROVIDER_ERROR),
        (KeyError("k"), PROVIDER_ERROR),
    ],
    ids=["timeout", "asyncio_timeout", "runtime", "key"],
)
def test_a_scripted_provider_exception_is_returned_as_a_typed_failure_by_the_adapter(
    staged: Exception, code: str
) -> None:
    """A conformant adapter never raises: the fake's staged provider error comes back as a result, and the next entry runs."""
    ctx = make_context()
    fake = FakeWorkflowExecutor(step_script=[staged, ScriptedStep("completed")])
    result = fake.run_step(ctx.request, ctx.step_input)
    assert_typed_failure(result, ctx, code, "error")
    assert fake.run_step(ctx.request, ctx.step_input).stop_reason == "completed"


def test_a_scripted_record_is_read_like_provider_output() -> None:
    ctx = make_context()
    fake = FakeWorkflowExecutor(step_script=[lambda request: ctx.valid_record()])
    assert run(fake, ctx).stop_reason == "completed"


@pytest.mark.parametrize(
    "record",
    [
        lambda ctx: ctx.valid_record(state="committed"),
        lambda ctx: ctx.valid_record(stop_reason="committed"),
        lambda ctx: ctx.valid_record(schema_version="v2"),
        lambda ctx: ctx.valid_record(attempt=2),
        lambda ctx: ctx.valid_record(proposed_actions=[{"effect_class": "write"}]),
        lambda ctx: {},
    ],
    ids=["extra_state", "stop_committed", "version_v2", "other_attempt", "bad_action", "empty"],
)
def test_scripted_provider_records_that_are_invalid_are_malformed_and_returned_by_the_adapter(
    record: Callable[[StepContext], dict[str, Any]],
) -> None:
    ctx = make_context()
    fake = FakeWorkflowExecutor(step_script=[lambda request: record(ctx)])
    assert_typed_failure(fake.run_step(ctx.request, ctx.step_input), ctx, MALFORMED_RESULT, "error")


def test_a_scripted_record_with_too_deep_arguments_is_refused() -> None:
    ctx = make_context()
    deep: dict[str, Any] = {}
    node = deep
    for _ in range(200):
        node["a"] = {}
        node = node["a"]
    action = {**ctx.valid_record(stop_reason="proposed_action")}
    action["proposed_actions"] = [
        {
            "effect_class": "irreversible_write",
            "target": {"connector": "c", "resource": "r"},
            "connector_version": "1",
            "args": deep,
            "expected_resource_version": None,
        }
    ]
    fake = FakeWorkflowExecutor(step_script=[lambda request: action])
    assert_typed_failure(run(fake, ctx), ctx, MALFORMED_RESULT, "error")


def test_a_shared_script_hands_each_concurrent_step_a_different_entry() -> None:
    ctx = make_context()
    count = 40
    entries = [ScriptedStep("completed", usage=StepUsage(i, 0, i, 1, 0)) for i in range(count)]
    fake = FakeWorkflowExecutor(step_script=entries)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: run(fake, ctx), range(count)))
    assert sorted(r.usage.total_tokens for r in results) == list(range(count))
    assert fake.step_script.remaining == 0


def test_two_callers_on_one_entry_get_it_once_and_a_refusal_never_an_exception() -> None:
    ctx = make_context()
    for _ in range(25):
        script = StepScript([ScriptedStep("completed")])
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda _, script=script: script.answer(ctx.request, ctx.step_input), range(2)
                )
            )
        assert sorted(r.stop_reason for r in results) == ["completed", "refused"]


def test_the_script_does_not_check_then_pop() -> None:
    """If another caller takes the last entry between a length check and the pop, the loser is refused, not an IndexError."""
    ctx = make_context()

    class Drained(deque):  # type: ignore[type-arg]
        def __len__(self) -> int:
            return 1

        def __bool__(self) -> bool:
            return True

    script = StepScript([])
    script._entries = Drained()
    result = script.answer(ctx.request, ctx.step_input)
    assert_typed_failure(result, ctx, SCRIPT_EXHAUSTED, "refused")


def test_a_step_script_can_be_used_without_the_executor() -> None:
    ctx = make_context()
    script = StepScript([ScriptedStep("completed")])
    assert script.answer(ctx.request, ctx.step_input).stop_reason == "completed"


def test_run_agent_is_unchanged_by_the_step_script() -> None:
    """Existing (ungoverned) agent turns keep working and keep their own bookkeeping, scripted or not."""
    ctx = make_context()
    agent = dataclasses.replace(ctx.step_input.agent, tools=[])
    fake = FakeWorkflowExecutor(step_script=[ScriptedStep("completed")])
    turn = fake.run_agent(agent, "hello world", tool_callables={}, preview=True)
    assert turn.final_output == "[Request document] processed: hello world"
    assert turn.tokens == 10
    assert fake.calls == [agent.node_id] and fake.step_requests == []
    run(fake, ctx)
    assert fake.calls == [agent.node_id]
    assert fake.step_script.remaining == 0


# -------------------------------------------------------------- governed provider executor regression


@pytest.mark.parametrize("name", ["openai_agents"])
def test_a_provider_executor_honors_the_governed_deadline(name: str) -> None:
    ctx = make_context()
    executor = every_executor()[name]
    assert isinstance(executor, AgentStepAdapter)
    result = run(executor, ctx)
    assert result.stop_reason == "deadline"
    assert result.error_code is None
    assert result.usage_source == "estimated"
