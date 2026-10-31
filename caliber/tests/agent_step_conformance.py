"""A conformance harness for ``AgentStepAdapter`` implementations (B08.1b, #720).

Every provider adapter (B08.anthropic #727, B08.openai-agents #728, B08.openai-chat #729, B08.openai-responses #730) plugs into
the same harness, and the conformance suite (B08.5, #769) parametrizes over the five executors with it. An adapter's own test
file supplies a **driver**: given a provider *scenario* ("the model proposes a protected tool", "the provider times out",
"the provider returns something that claims a commit") the driver returns the adapter built on a transport stubbed to that
behaviour, **whose stub calls** :meth:`StepContext.provider_called` **each time the provider would be called**. The harness runs
it through the one platform entry, :func:`caliber.workflows.agent_step.run_agent_step`, and asserts what holds for every
adapter whatever its provider:

* the driver's transport was reached (a driver that returns a canned result without a provider path fails);
* the result answers the asked step and is the contract's own (``parse_step_result(result.record(), request)``);
* a protected tool is never run. The harness plants a canary behind the protected tool in **three** places an adapter could
  reach it: the agent's tool binding (``module_path``/``callable_name``), :attr:`StepContext.tool_registry` (the unfiltered
  name-to-callable map a legacy executor resolves tools from) and nowhere in ``step_input.read_tools``. An adapter that
  ignores ``read_tools`` and runs what the model named trips it;
* a model that proposes a protected tool yields exactly one proposed action built from **that** tool call (the staged,
  per-scenario arguments), which stays an unbound claim (``platform_bound`` false); a model that lies about its
  ``effect_class`` for the protected tool is a ``ClaimMismatch`` at ``bound_to``;
* a model that calls a read tool has it run through ``read_tools`` (``ctx.read_calls``), and a ``max_*`` stop keeps the
  provider's usage;
* ``completes`` reports exactly the usage the stubbed provider gave, with ``usage_source`` ``provider``;
* a provider error, a timeout, unreadable output, output that claims a commit and a result for another step are each a bounded
  ``error`` result with no proposal, whose code is the adapter's own (the platform's ``adapter_not_integrated``,
  ``adapter_unsupported``, ``script_exhausted`` and ``adapter_error`` never pass): ``adapter_timeout`` for a timeout and
  ``malformed_result`` for output that cannot be read. The adapter itself, called directly, returns that typed result: an
  adapter never raises;
* the provider's payload, planted as :data:`SECRET_MARKER`, is in neither the result nor any log record (the loggers are
  lowered to DEBUG for the run);
* the platform, not the adapter, enforces the request's limits (B08.3, #752): :func:`fixed_clock`, :func:`limits_changes` and
  :func:`stop_before_work` let B08.5 (#769) show, for each adapter and each of turns, tool calls, tokens and the deadline, that
  a step with no budget never reaches its provider and that one which passes a limit stops with the typed reason. Every
  :func:`run_scenario` runs on :attr:`StepContext.clock` (a fixed one, so no deadline depends on the date the suite runs on).

A driver implements :class:`AdapterDriver`; ``unsupported`` names scenarios its transport cannot express, but never
``completes``, ``proposes_protected_tool`` or ``claims_commit``. Nothing here calls a provider or reads a clock. Importing it
needs no provider SDK.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from functools import cached_property
from typing import Any, Protocol

import pytest

from caliber.contracts.agent_step import (
    AgentStepRequest,
    AgentStepResult,
    ClaimMismatch,
    ProposedAction,
    StepUsage,
    parse_step_request,
    parse_step_result,
)
from caliber.effects.inventory import effect_class
from caliber.workflows.agent_step import (
    ADAPTER_ERROR,
    ADAPTER_NOT_INTEGRATED,
    ADAPTER_TIMEOUT,
    ADAPTER_UNSUPPORTED,
    MALFORMED_RESULT,
    SCRIPT_EXHAUSTED,
    AgentStepInput,
    StepClock,
    run_agent_step,
)
from caliber.workflows.ir import IRAgent, IRToolBinding, NodeType

#: Planted in every failure a driver stages (exception text, a provider payload); it must never appear in a result or a log.
SECRET_MARKER = "provider-payload-marker-7f3a"
#: The tool a scenario's model proposes, and the platform's class for it (the inventory fails closed for a name it does not list).
PROTECTED_TOOL = "request_missing_document"
PROTECTED_CLASS = effect_class(PROTECTED_TOOL)
#: The read-class tool, the only one in ``step_input.read_tools``.
READ_TOOL = "lookup_case"
#: The platform's own codes: an adapter that merely falls through to one of them has not exercised its provider path.
PLATFORM_CODES = frozenset(
    {ADAPTER_NOT_INTEGRATED, ADAPTER_UNSUPPORTED, SCRIPT_EXHAUSTED, ADAPTER_ERROR}
)
_BUDGET_STOPS = ("max_turns", "max_tool_calls", "max_tokens")

#: The instant every conformance step starts at: ten minutes before the fixture deadline of ``2026-10-26T09:10:00Z``.
FIXED_NOW = datetime(2026, 10, 26, 9, 0, 0, tzinfo=timezone.utc)


class FakeStepClock:
    """A platform clock that only moves when a test moves it: wall time and a monotonic reading, advanced together."""

    def __init__(self, now: datetime = FIXED_NOW) -> None:
        self.wall = now
        self.mono = 0.0

    def advance(self, seconds: float) -> None:
        """Time passes: both readings move."""
        self.wall += timedelta(seconds=seconds)
        self.mono += seconds

    def step_clock(self) -> StepClock:
        return StepClock(now=lambda: self.wall, monotonic=lambda: self.mono)


def fixed_clock() -> StepClock:
    """A clock that never moves, at :data:`FIXED_NOW`."""
    return FakeStepClock().step_clock()


def limits_changes(**changes: Any) -> dict[str, Any]:
    """``make_request(**limits_changes(max_turns=0))``: the default limits with ``changes`` applied (``limits`` as a record)."""
    limits: dict[str, Any] = {
        "max_turns": 8,
        "max_tool_calls": 4,
        "max_tokens": 20000,
        "deadline": "2026-10-26T09:10:00Z",
    }
    limits.update(changes)
    return {"limits": limits}


class Scenario(StrEnum):
    """What the stubbed provider does."""

    COMPLETES = "completes"
    READ_TOOL_CALL = "read_tool_call"
    PROPOSES_PROTECTED_TOOL = "proposes_protected_tool"
    MODEL_LIES_ABOUT_EFFECT_CLASS = "model_lies_about_effect_class"
    BUDGET_EXHAUSTED = "budget_exhausted"
    PROVIDER_ERROR = "provider_error"
    PROVIDER_TIMEOUT = "provider_timeout"
    UNREADABLE_OUTPUT = "unreadable_output"
    CLAIMS_COMMIT = "claims_commit"
    ANOTHER_STEPS_RESULT = "another_steps_result"


#: Scenarios a driver may not declare unsupported: they are the core of governed execution.
MANDATORY = frozenset(
    {Scenario.COMPLETES, Scenario.PROPOSES_PROTECTED_TOOL, Scenario.CLAIMS_COMMIT}
)
_ACTIVE: list[StepContext] = []


def canary_protected_tool(**arguments: Any) -> str:
    """What the agent's protected tool binding resolves to. Running it is a conformance failure."""
    _ACTIVE[-1].protected_calls.append(arguments)
    return "mutated"


def canary_read_tool(**arguments: Any) -> str:
    """What the agent's read tool binding resolves to."""
    _ACTIVE[-1].read_calls.append(arguments)
    return "data"


def _binding(name: str, callable_name: str, side_effect_level: str) -> IRToolBinding:
    return IRToolBinding(
        local_name=name,
        registry_ref=name,
        version_constraint="*",
        requires_approval=side_effect_level != "read",
        side_effect_level=side_effect_level,
        allow_in_preview=False,
        module_path=__name__,
        callable_name=callable_name,
    )


@dataclass
class StepContext:
    """One step's request, input, staged provider data and the canaries a driver wires into its stubbed transport."""

    request: AgentStepRequest
    scenario: Scenario = Scenario.COMPLETES
    protected_calls: list[dict[str, Any]] = field(default_factory=list)
    read_calls: list[dict[str, Any]] = field(default_factory=list)
    provider_calls: int = 0
    #: What the staged provider reports for the scenario, when a driver's adapter counts turns and tool calls itself and the
    #: default :attr:`staged_usage` cannot be produced by its loop (three turns and no tool call, for a budget stop). A driver
    #: sets it before its transport answers; the tokens it states are the ones the stub must report. Additive (B08.anthropic).
    usage_override: StepUsage | None = None
    #: The platform's clock for the step (B08.3); fixed, so a deadline never depends on the date the suite runs on.
    clock: StepClock = field(default_factory=fixed_clock)

    def provider_called(self) -> None:
        """A driver's stubbed transport calls this each time the provider would be called."""
        self.provider_calls += 1

    @property
    def staged_args(self) -> dict[str, Any]:
        """The arguments of the protected tool call the staged model makes: distinct per scenario, so a result must derive from it."""
        return {"document_type": f"pay_statement-{self.scenario.value}"}

    @property
    def staged_read_args(self) -> dict[str, Any]:
        return {"case_id": f"case-{self.scenario.value}"}

    @property
    def staged_usage(self) -> StepUsage:
        """The usage the stubbed provider reports for the scenario; a conformant adapter reports exactly it."""
        if self.usage_override is not None:
            return self.usage_override
        if self.scenario is Scenario.BUDGET_EXHAUSTED:
            # One turn that spent the budget: three turns with no tool call is not something a provider transport can stage
            # under the default limits (a second turn exists only after a tool call, and ``tool_calls`` would not be 0).
            return StepUsage(900, 100, 1000, 1, 0)
        if self.scenario is Scenario.READ_TOOL_CALL:
            return StepUsage(150, 30, 180, 2, 1)
        return StepUsage(100, 20, 120, 1, 0)

    @cached_property
    def tool_registry(self) -> dict[str, Callable[..., Any]]:
        """The unfiltered name-to-callable map of every tool of the agent, protected canary included."""
        return {READ_TOOL: self.read_tool, PROTECTED_TOOL: self.protected_tool}

    @cached_property
    def step_input(self) -> AgentStepInput:
        """What the platform hands the adapter: the agent (both tool bindings) and only the read-class tool."""
        agent = IRAgent(
            node_id=self.request.node_id,
            node_type=NodeType.AGENT,
            name="Request document",
            tools=[
                _binding(READ_TOOL, "canary_read_tool", "read"),
                _binding(PROTECTED_TOOL, "canary_protected_tool", "external_action"),
            ],
        )
        return AgentStepInput(
            agent=agent,
            input_text="Ask the applicant for the missing pay statement",
            read_tools={READ_TOOL: self.read_tool},
        )

    def protected_tool(self, **arguments: Any) -> str:
        """The implementation behind the protected tool. If an adapter ever runs it, the step is not conformant."""
        self.protected_calls.append(arguments)
        return "mutated"

    def read_tool(self, **arguments: Any) -> str:
        """A read-class tool the adapter may run in-loop; it is in :attr:`AgentStepInput.read_tools`."""
        self.read_calls.append(arguments)
        return "data"

    def protected_action(self, claimed_effect_class: str | None = None) -> ProposedAction:
        """The action a staged model proposes for :data:`PROTECTED_TOOL`, from :attr:`staged_args`.

        The claim is :data:`PROTECTED_CLASS` unless ``claimed_effect_class`` says the model lies.
        """
        return ProposedAction(
            connector="document-request",
            connector_version="1.0.0",
            effect_class=claimed_effect_class or PROTECTED_CLASS,
            resource="cases/CASE-1/document-requests",
            args=self.staged_args,
        )

    def valid_record(self, **changes: Any) -> dict[str, Any]:
        """A schema-valid ``completed`` result record for this step, for a driver that stages raw provider output."""
        record: dict[str, Any] = {
            "schema_version": "v1",
            "step_request_id": self.request.step_request_id,
            "workflow_run_id": self.request.workflow_run_id,
            "node_id": self.request.node_id,
            "occurrence_id": self.request.occurrence_id,
            "attempt": self.request.attempt,
            "stop_reason": "completed",
            "proposed_actions": [],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "turns": 1,
                "tool_calls": 0,
            },
        }
        record.update(changes)
        return record


class AdapterDriver(Protocol):
    """What an adapter's test supplies: the adapter, with its provider transport stubbed to the scenario."""

    name: str
    #: Scenarios the transport cannot express (a provider that cannot be made to answer another step's identity, say).
    unsupported: frozenset[Scenario]

    def adapter_for(self, scenario: Scenario, ctx: StepContext) -> object: ...


def make_request(**changes: Any) -> AgentStepRequest:
    """A valid request; ``changes`` override its record fields (identity, limits) and the result is validated."""
    record: dict[str, Any] = {
        "schema_version": "v1",
        "step_request_id": "sr-conformance-1",
        "workflow_run_id": "run-conformance-1",
        "node_id": "request_document",
        "occurrence_id": "occ-conformance-1",
        "attempt": 1,
        "run_manifest_digest": "a" * 64,
        "context_manifest_digest": "b" * 64,
        "grant_id": "grant-conformance-1",
        "limits": {
            "max_turns": 8,
            "max_tool_calls": 4,
            "max_tokens": 20000,
            "deadline": "2026-10-26T09:10:00Z",
        },
    }
    record.update(changes)
    return parse_step_request(record)


def make_context(scenario: Scenario = Scenario.COMPLETES, **request_changes: Any) -> StepContext:
    return StepContext(request=make_request(**request_changes), scenario=scenario)


class _LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.text: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.text.append(self.format(record))


def _capture_logs() -> tuple[_LogCapture, Callable[[], None]]:
    """Capture every record at DEBUG: the root and ``caliber`` loggers are lowered for the run and restored by the callback."""
    capture = _LogCapture()
    loggers = [logging.getLogger(), logging.getLogger("caliber")]
    levels = [(logger, logger.level) for logger in loggers]
    for logger in loggers:
        logger.setLevel(logging.DEBUG)
    loggers[0].addHandler(capture)

    def restore() -> None:
        loggers[0].removeHandler(capture)
        for logger, level in levels:
            logger.setLevel(level)

    return capture, restore


def _run(driver: AdapterDriver, ctx: StepContext) -> AgentStepResult:
    _ACTIVE.append(ctx)
    try:
        return run_agent_step(
            driver.adapter_for(ctx.scenario, ctx), ctx.request, ctx.step_input, clock=ctx.clock
        )
    finally:
        _ACTIVE.pop()


def run_scenario(driver: AdapterDriver, scenario: Scenario) -> tuple[AgentStepResult, StepContext]:
    """Run one scenario through :func:`run_agent_step` and check what holds for every adapter. Returns the result."""
    forbidden = driver.unsupported & MANDATORY
    assert not forbidden, (
        f"{driver.name} may not declare {sorted(s.value for s in forbidden)} unsupported"
    )
    if scenario in driver.unsupported:
        pytest.skip(f"{driver.name}: its transport cannot stage {scenario.value}")
    ctx = make_context(scenario)
    capture, restore = _capture_logs()
    try:
        result = _run(driver, ctx)
    finally:
        restore()
    assert ctx.provider_calls >= 1, f"{driver.name}: the stubbed provider was never reached"
    # Whatever happened, the caller got the answer to this very step, and a protected tool was never run.
    assert parse_step_result(result.record(), ctx.request) == result
    assert ctx.protected_calls == [], f"{driver.name} executed a protected tool"
    assert SECRET_MARKER not in json.dumps(result.record()), "the result carries provider text"
    assert not any(SECRET_MARKER in line for line in capture.text), (
        "a log record carries provider text"
    )
    _CHECKS[scenario](result, ctx)
    if scenario in _FAILURES:
        _adapter_returns_typed_failure(driver, scenario, result)
    return result, ctx


def stop_before_work(adapter: object, ctx: StepContext, expected_reason: str) -> AgentStepResult:
    """Run ``adapter`` through the boundary for a step that may not start: it is never called, whatever it is.

    Asserts the typed stop (``expected_reason``), the zero ``estimated`` usage and that no proposal, provider call or tool ran;
    ``ctx`` carries the request (see :func:`limits_changes`). The adapter's own checks are not what is being tested: for a
    real executor the provider client is a tripwire, for a driver ``ctx.provider_calls`` stays 0.
    """
    result = run_agent_step(adapter, ctx.request, ctx.step_input, clock=ctx.clock)
    assert result.stop_reason == expected_reason, result.stop_reason
    assert result.proposed_actions == () and result.error_code is None
    assert result.usage == StepUsage(0, 0, 0, 0, 0)
    assert result.usage_source == "estimated"
    assert ctx.provider_calls == 0 and ctx.read_calls == [] and ctx.protected_calls == []
    parse_step_result(result.record(), ctx.request)
    return result


def _adapter_returns_typed_failure(
    driver: AdapterDriver, scenario: Scenario, via_boundary: AgentStepResult
) -> None:
    """The adapter itself, called directly, returns the typed failure: it never relies on the boundary to catch it."""
    ctx = make_context(scenario)
    _ACTIVE.append(ctx)
    try:
        adapter = driver.adapter_for(scenario, ctx)
        try:
            direct = adapter.run_step(ctx.request, ctx.step_input)  # type: ignore[attr-defined]
        except Exception as exc:
            pytest.fail(
                f"{driver.name}: run_step raised {type(exc).__name__} instead of returning a typed result"
            )
    finally:
        _ACTIVE.pop()
    assert ctx.provider_calls >= 1 and ctx.protected_calls == []
    assert type(direct) is AgentStepResult
    assert direct == via_boundary, (
        f"{driver.name}: the adapter's own result differs from the validated one"
    )


def _completes(result: AgentStepResult, ctx: StepContext) -> None:
    assert result.stop_reason == "completed"
    assert result.proposed_actions == ()
    assert result.error_code is None
    # The usage is the stubbed provider's, reported as such: not recounted, not zero, not estimated.
    assert result.usage == ctx.staged_usage
    assert result.usage.total_tokens > 0
    assert result.usage_source in (None, "provider")


def _read_tool_call(result: AgentStepResult, ctx: StepContext) -> None:
    assert result.stop_reason == "completed"
    # The tool the model named ran, with the model's arguments, through read_tools: the only tool it could run.
    assert ctx.read_calls == [ctx.staged_read_args]
    assert result.usage.tool_calls >= 1
    assert result.usage == ctx.staged_usage


def _proposes(result: AgentStepResult, ctx: StepContext) -> None:
    assert result.stop_reason == "proposed_action"
    (action,) = result.proposed_actions
    # The action is built from the staged tool call, not from a template: the per-scenario arguments are in it.
    assert dict(action.args) == ctx.staged_args
    assert action == ctx.protected_action()
    # What the model says is a claim until the platform binds it: nothing the adapter returned is already trusted.
    assert action.platform_bound is False
    assert action.bound_to(PROTECTED_CLASS).platform_bound is True
    other = "read" if PROTECTED_CLASS != "read" else "irreversible_write"
    with pytest.raises(ClaimMismatch):
        action.bound_to(other)
    assert not {"state", "receipt", "receipt_id", "approval"} & result.record().keys()
    assert result.usage == ctx.staged_usage


def _lies(result: AgentStepResult, ctx: StepContext) -> None:
    assert result.stop_reason == "proposed_action"
    (action,) = result.proposed_actions
    assert dict(action.args) == ctx.staged_args
    # The model called a protected tool a read. The platform's class governs: the claim cannot be bound, and it was not run.
    assert action.platform_bound is False
    assert PROTECTED_CLASS != "read"
    assert action.effect_class != PROTECTED_CLASS
    with pytest.raises(ClaimMismatch):
        action.bound_to(PROTECTED_CLASS)


def _budget_exhausted(result: AgentStepResult, ctx: StepContext) -> None:
    assert result.stop_reason in _BUDGET_STOPS
    assert result.proposed_actions == ()
    # The provider's usage is kept: what was spent is charged, not forgotten.
    assert result.usage == ctx.staged_usage
    assert result.usage_source in (None, "provider")


def _fails(expected: str | None) -> Callable[[AgentStepResult, StepContext], None]:
    """A bounded ``error`` with no proposal; ``expected`` is the exact code, or ``None`` for any code the adapter itself chose."""

    def check(result: AgentStepResult, ctx: StepContext) -> None:
        del ctx
        assert result.stop_reason == "error", result.stop_reason
        assert result.proposed_actions == ()
        assert result.error_code is not None
        if expected is None:
            assert result.error_code not in PLATFORM_CODES | {MALFORMED_RESULT}, result.error_code
        else:
            assert result.error_code == expected, result.error_code
        # Figures that look genuine must be genuine: a failure with no usage says "unknown" or "estimated".
        if result.usage.total_tokens == 0:
            assert result.usage_source in ("unknown", "estimated")

    return check


_CHECKS: dict[Scenario, Callable[[AgentStepResult, StepContext], None]] = {
    Scenario.COMPLETES: _completes,
    Scenario.READ_TOOL_CALL: _read_tool_call,
    Scenario.PROPOSES_PROTECTED_TOOL: _proposes,
    Scenario.MODEL_LIES_ABOUT_EFFECT_CLASS: _lies,
    Scenario.BUDGET_EXHAUSTED: _budget_exhausted,
    Scenario.PROVIDER_ERROR: _fails(None),
    Scenario.PROVIDER_TIMEOUT: _fails(ADAPTER_TIMEOUT),
    Scenario.UNREADABLE_OUTPUT: _fails(MALFORMED_RESULT),
    Scenario.CLAIMS_COMMIT: _fails(MALFORMED_RESULT),
    Scenario.ANOTHER_STEPS_RESULT: _fails(MALFORMED_RESULT),
}
_FAILURES = frozenset(
    {
        Scenario.PROVIDER_ERROR,
        Scenario.PROVIDER_TIMEOUT,
        Scenario.UNREADABLE_OUTPUT,
        Scenario.CLAIMS_COMMIT,
        Scenario.ANOTHER_STEPS_RESULT,
    }
)
SCENARIOS = tuple(Scenario)
EXECUTOR_NAMES = ("fake", "openai_chat", "openai_responses", "openai_agents", "anthropic")


def every_executor() -> dict[str, object]:
    """The five ``WorkflowExecutor`` implementations, each built so that constructing it touches no provider.

    The real ones get a tripwire client that fails the test if anything calls it, so a governed step that is not integrated
    can be shown to make no provider call. B08.5 (#769) replaces the tripwire with its canned transport per adapter.
    """
    from caliber.workflows.runtime import (
        AnthropicChatWorkflowExecutor,
        FakeWorkflowExecutor,
        OpenAIAgentsWorkflowExecutor,
        OpenAIChatWorkflowExecutor,
        OpenAIResponsesWorkflowExecutor,
    )

    class _Tripwire:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"the provider client was used ({name})")

    tripwire = _Tripwire()
    return {
        "fake": FakeWorkflowExecutor(),
        "openai_chat": OpenAIChatWorkflowExecutor(
            api_key="unused", default_model="m", client=tripwire
        ),
        "openai_responses": OpenAIResponsesWorkflowExecutor(
            api_key="unused", default_model="m", client=tripwire
        ),
        "openai_agents": OpenAIAgentsWorkflowExecutor(api_key="unused", default_model="m"),
        "anthropic": AnthropicChatWorkflowExecutor(
            api_key="unused", default_model="m", client=tripwire
        ),
    }
