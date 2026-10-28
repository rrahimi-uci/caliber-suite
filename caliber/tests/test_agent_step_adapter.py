"""The AgentStepAdapter boundary and the scripted fake adapter (B08.1b, #720).

The boundary (``run_agent_step``) treats an adapter as untrusted: the request is checked before the adapter runs, and what
the adapter returns or raises is rebuilt or replaced by a typed, bounded result, never an exception and never text from the
failure. The scripted ``FakeWorkflowExecutor`` is deterministic and offline. The four provider executors refuse governed
execution until their own B08 tasks integrate them. ``tests/agent_step_conformance.py`` is the harness those tasks plug into;
here it is exercised through the fake.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from caliber.contracts import ContractValidationError, UnsupportedSchemaVersion
from caliber.contracts.agent_step import (
    AgentStepRequest,
    AgentStepResult,
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
    SCRIPT_EXHAUSTED,
    AgentStepAdapter,
    AgentStepInput,
    ScriptedStep,
    StepScript,
    answer,
    failure,
    refusal,
    run_agent_step,
)
from caliber.workflows.runtime import AgentStepAdapter as RuntimeAgentStepAdapter
from caliber.workflows.runtime import FakeWorkflowExecutor
from tests.agent_step_conformance import (
    SCENARIOS,
    SECRET_MARKER,
    AdapterDriver,
    Scenario,
    StepContext,
    every_executor,
    make_context,
    run_scenario,
)


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


def completed(ctx: StepContext, **changes: Any) -> AgentStepResult:
    return dataclasses.replace(answer(ctx.request, "completed"), **changes)


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
        usage = StepUsage(100, 20, 120, 1, 0)
        entries: dict[Scenario, Any] = {
            Scenario.COMPLETES: ScriptedStep("completed", usage=usage),
            Scenario.PROPOSES_PROTECTED_TOOL: ScriptedStep(
                "proposed_action", (ctx.protected_action(),), usage
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
        return FakeWorkflowExecutor(step_script=[entries[scenario]])


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_the_fake_conforms_to_the_shared_harness(scenario: Scenario) -> None:
    driver: AdapterDriver = FakeDriver()
    result, ctx = run_scenario(driver, scenario)
    assert result.step_request_id == ctx.request.step_request_id


def test_the_harness_catches_an_adapter_that_executes_a_protected_tool() -> None:
    """The conformance check is not hollow: an adapter that runs the protected tool is reported."""

    class Executing(FakeDriver):
        def adapter_for(self, scenario: Scenario, ctx: StepContext) -> object:
            def run_it(request: AgentStepRequest) -> AgentStepResult:
                ctx.protected_tool(document_type="pay_statement")
                return answer(request, "completed")

            return Spy(run_it)

    with pytest.raises(AssertionError, match="executed a protected tool"):
        run_scenario(Executing(), Scenario.COMPLETES)


def test_the_harness_catches_an_adapter_that_leaks_provider_text() -> None:
    class Leaking(FakeDriver):
        def adapter_for(self, scenario: Scenario, ctx: StepContext) -> object:
            return Spy(lambda request: answer(request, "error", error_code=SECRET_MARKER))

    with pytest.raises(AssertionError, match="carries provider text"):
        run_scenario(Leaking(), Scenario.PROVIDER_ERROR)


def test_the_harness_skips_a_scenario_the_transport_cannot_stage() -> None:
    class Limited(FakeDriver):
        unsupported = frozenset({Scenario.ANOTHER_STEPS_RESULT})

    with pytest.raises(pytest.skip.Exception):
        run_scenario(Limited(), Scenario.ANOTHER_STEPS_RESULT)


# ------------------------------------------------------------------------------------------ the boundary


def test_a_conformant_adapter_is_called_once_with_the_validated_request() -> None:
    ctx = make_context()
    spy = Spy(lambda request: answer(request, "completed", usage_source="provider"))
    result = run(spy, ctx)
    assert result.stop_reason == "completed"
    ((seen_request, seen_input),) = spy.calls
    assert seen_request == ctx.request
    assert seen_input is ctx.step_input


def test_the_protocol_is_the_one_runtime_exports() -> None:
    assert RuntimeAgentStepAdapter is AgentStepAdapter
    assert isinstance(FakeWorkflowExecutor(), AgentStepAdapter)
    assert not isinstance(object(), AgentStepAdapter)


@pytest.mark.parametrize("not_an_adapter", [object(), None, "run_step", {"run_step": 1}])
def test_an_object_without_run_step_is_refused_and_not_called(not_an_adapter: object) -> None:
    ctx = make_context()
    result = run(not_an_adapter, ctx)
    assert_typed_failure(result, ctx, ADAPTER_UNSUPPORTED, "refused")


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


def test_a_subclass_cannot_return_a_different_record_than_it_validates() -> None:
    """The boundary returns what it rebuilt from the record, not the adapter's own object."""
    ctx = make_context()

    class Sneaky(AgentStepResult):
        pass

    sneaky = Sneaky(
        **{
            f.name: getattr(answer(ctx.request, "completed"), f.name)
            for f in dataclasses.fields(AgentStepResult)
        }
    )
    result = run(Spy(sneaky), ctx)
    assert type(result) is AgentStepResult
    assert result == answer(ctx.request, "completed")


# ---- an adapter that fails: bounded, quiet, typed


@pytest.mark.parametrize(
    ("raised", "code"),
    [
        (RuntimeError(f"401 for key {SECRET_MARKER}"), ADAPTER_ERROR),
        (ConnectionError(SECRET_MARKER), ADAPTER_ERROR),
        (KeyError(SECRET_MARKER), ADAPTER_ERROR),
        (TimeoutError(SECRET_MARKER), ADAPTER_TIMEOUT),
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


def test_an_interrupt_is_not_swallowed() -> None:
    ctx = make_context()
    for interrupt in (KeyboardInterrupt(), SystemExit(1)):
        with pytest.raises(type(interrupt)):
            run(Spy(interrupt), ctx)


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


def test_the_fake_never_calls_a_tool_it_was_handed() -> None:
    """Neither the read callables nor anything else in the step input is invoked: a step proposes, it does not act."""
    ctx = make_context()
    fake = FakeWorkflowExecutor(step_script=[ScriptedStep("completed")])
    run(fake, ctx)
    run(FakeWorkflowExecutor(), ctx)
    assert ctx.read_calls == [] and ctx.protected_calls == []


def test_a_scripted_exception_is_raised_once_by_the_adapter_and_the_next_entry_still_runs() -> None:
    ctx = make_context()
    fake = FakeWorkflowExecutor(step_script=[TimeoutError("t"), ScriptedStep("completed")])
    with pytest.raises(TimeoutError):
        fake.run_step(ctx.request, ctx.step_input)
    assert fake.run_step(ctx.request, ctx.step_input).stop_reason == "completed"


def test_a_scripted_record_is_read_like_provider_output() -> None:
    ctx = make_context()
    good = lambda request: ctx.valid_record()
    fake = FakeWorkflowExecutor(step_script=[good])
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
def test_scripted_provider_records_that_are_invalid_are_malformed(
    record: Callable[[StepContext], dict[str, Any]],
) -> None:
    ctx = make_context()
    fake = FakeWorkflowExecutor(step_script=[lambda request: record(ctx)])
    assert_typed_failure(run(fake, ctx), ctx, MALFORMED_RESULT, "error")


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


def test_a_step_script_can_be_used_without_the_executor() -> None:
    ctx = make_context()
    script = StepScript([ScriptedStep("completed")])
    assert script.answer(ctx.request, ctx.step_input).stop_reason == "completed"


def test_run_agent_is_unchanged_by_the_step_script() -> None:
    """Existing (ungoverned) agent turns keep working and keep their own bookkeeping, scripted or not."""
    ctx = make_context()
    agent = ctx.step_input.agent
    fake = FakeWorkflowExecutor(step_script=[ScriptedStep("completed")])
    turn = fake.run_agent(agent, "hello world", tool_callables={}, preview=True)
    assert turn.final_output == "[Request document] processed: hello world"
    assert turn.tokens == 10
    assert fake.calls == [agent.node_id] and fake.step_requests == []
    run(fake, ctx)
    assert fake.calls == [agent.node_id]
    assert fake.step_script.remaining == 0


# -------------------------------------------------------------- the four provider executors refuse until integrated


@pytest.mark.parametrize("name", ["openai_chat", "openai_responses", "openai_agents", "anthropic"])
def test_a_provider_executor_refuses_governed_execution_until_it_is_integrated(name: str) -> None:
    ctx = make_context()
    executor = every_executor()[name]
    assert isinstance(executor, AgentStepAdapter)
    result = run(executor, ctx)
    assert_typed_failure(result, ctx, ADAPTER_NOT_INTEGRATED, "refused")
