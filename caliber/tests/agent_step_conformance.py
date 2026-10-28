"""A conformance harness for ``AgentStepAdapter`` implementations (B08.1b, #720).

Every provider adapter (B08.anthropic #727, B08.openai-agents #728, B08.openai-chat #729, B08.openai-responses #730) plugs into
the same harness, and the conformance suite (B08.5, #769) parametrizes over the five executors with it. An adapter's own test
file supplies a **driver**: given a provider *scenario* ("the model proposes a protected tool", "the provider times out",
"the provider returns something that claims a commit") the driver returns the adapter built on a transport stubbed to that
behaviour. The harness runs it through the one platform entry, :func:`caliber.workflows.agent_step.run_agent_step`, and asserts
what holds for **every** adapter whatever its provider:

* it answers the step that was asked (``parse_step_result(result.record(), request)`` holds: same id, same
  run/node/occurrence/attempt, schema-valid), and nothing it does raises to the caller;
* a model that proposes a protected tool yields exactly one proposed action, which stays an **unbound claim**
  (``platform_bound`` false; ``bound_to`` the inventory's class works and a different class is a ``ClaimMismatch``), with the
  protected tool never executed (the canary :attr:`StepContext.protected_tool` has no call);
* a provider error, a timeout, unreadable output, output that claims an effect was committed and a result for another step are
  each a bounded ``error``/``refused`` result with no proposal and no text from the failure (the provider's payload, planted
  as :data:`SECRET_MARKER`, is in neither the result nor any log record).

A driver implements :class:`AdapterDriver`; ``unsupported`` names scenarios its transport cannot express. Nothing here calls a
provider or reads a clock. Importing it needs no provider SDK.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cached_property
from typing import Any, Protocol

import pytest

from caliber.contracts.agent_step import (
    AgentStepRequest,
    AgentStepResult,
    ClaimMismatch,
    ProposedAction,
    parse_step_request,
    parse_step_result,
)
from caliber.effects.inventory import effect_class
from caliber.workflows.agent_step import AgentStepInput, run_agent_step
from caliber.workflows.ir import IRAgent, NodeType

#: Planted in every failure a driver stages (exception text, a provider payload); it must never appear in a result or a log.
SECRET_MARKER = "provider-payload-marker-7f3a"
#: The tool a scenario's model proposes, and the platform's class for it (the inventory fails closed for a name it does not list).
PROTECTED_TOOL = "request_missing_document"
PROTECTED_CLASS = effect_class(PROTECTED_TOOL)
_ERROR_STOPS = ("error", "refused")


class Scenario(StrEnum):
    """What the stubbed provider does."""

    COMPLETES = "completes"
    PROPOSES_PROTECTED_TOOL = "proposes_protected_tool"
    PROVIDER_ERROR = "provider_error"
    PROVIDER_TIMEOUT = "provider_timeout"
    UNREADABLE_OUTPUT = "unreadable_output"
    CLAIMS_COMMIT = "claims_commit"
    ANOTHER_STEPS_RESULT = "another_steps_result"


@dataclass
class StepContext:
    """One step's request, input and the canaries a driver wires into its stubbed transport."""

    request: AgentStepRequest
    protected_calls: list[dict[str, Any]] = field(default_factory=list)
    read_calls: list[dict[str, Any]] = field(default_factory=list)

    @cached_property
    def step_input(self) -> AgentStepInput:
        """What the platform hands the adapter: the agent, its input and only the read-class tool."""
        agent = IRAgent(
            node_id=self.request.node_id, node_type=NodeType.AGENT, name="Request document"
        )
        return AgentStepInput(
            agent=agent,
            input_text="Ask the applicant for the missing pay statement",
            read_tools={"lookup_case": self.read_tool},
        )

    def protected_tool(self, **arguments: Any) -> str:
        """The implementation behind the protected tool. If an adapter ever runs it, the step is not conformant."""
        self.protected_calls.append(arguments)
        return "mutated"

    def read_tool(self, **arguments: Any) -> str:
        """A read-class tool the adapter may run in-loop; it is in :attr:`AgentStepInput.read_tools`."""
        self.read_calls.append(arguments)
        return "data"

    def protected_action(self) -> ProposedAction:
        """The action a staged model proposes for :data:`PROTECTED_TOOL` (the honest claim; see :data:`PROTECTED_CLASS`)."""
        return ProposedAction(
            connector="document-request",
            connector_version="1.0.0",
            effect_class=PROTECTED_CLASS,
            resource="cases/CASE-1/document-requests",
            args={"document_type": "pay_statement"},
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


def make_context(**request_changes: Any) -> StepContext:
    return StepContext(request=make_request(**request_changes))


class _LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.text: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.text.append(self.format(record))


def _is_error(result: AgentStepResult) -> bool:
    return result.stop_reason in _ERROR_STOPS


def run_scenario(driver: AdapterDriver, scenario: Scenario) -> tuple[AgentStepResult, StepContext]:
    """Run one scenario through :func:`run_agent_step` and check what holds for every adapter. Returns the result."""
    if scenario in driver.unsupported:
        pytest.skip(f"{driver.name}: its transport cannot stage {scenario.value}")
    ctx = make_context()
    capture = _LogCapture()
    root = logging.getLogger()
    root.addHandler(capture)
    try:
        result = run_agent_step(driver.adapter_for(scenario, ctx), ctx.request, ctx.step_input)
    finally:
        root.removeHandler(capture)
    # Whatever happened, the caller got the answer to this very step, and a protected tool was never run.
    assert parse_step_result(result.record(), ctx.request) == result
    assert ctx.protected_calls == [], f"{driver.name} executed a protected tool"
    assert SECRET_MARKER not in json.dumps(result.record()), "the result carries provider text"
    assert not any(SECRET_MARKER in line for line in capture.text), (
        "a log record carries provider text"
    )
    check = _CHECKS[scenario]
    check(result, ctx)
    return result, ctx


def _completes(result: AgentStepResult, ctx: StepContext) -> None:
    del ctx
    assert result.stop_reason == "completed"
    assert result.proposed_actions == ()
    assert result.error_code is None
    assert result.usage.total_tokens >= result.usage.prompt_tokens + result.usage.completion_tokens


def _proposes(result: AgentStepResult, ctx: StepContext) -> None:
    assert result.stop_reason == "proposed_action"
    (action,) = result.proposed_actions
    expected = ctx.protected_action()
    assert action == expected
    # What the model says is a claim until the platform binds it: nothing the adapter returned is already trusted.
    assert action.platform_bound is False
    assert action.bound_to(PROTECTED_CLASS).platform_bound is True
    other = "read" if PROTECTED_CLASS != "read" else "irreversible_write"
    with pytest.raises(ClaimMismatch):
        action.bound_to(other)
    record = result.record()
    assert not {"state", "receipt", "receipt_id", "approval"} & record.keys()


def _fails_quietly(result: AgentStepResult, ctx: StepContext) -> None:
    del ctx
    assert _is_error(result), result.stop_reason
    assert result.proposed_actions == ()
    assert result.error_code is not None
    # Figures that look genuine must be genuine: a failure with no usage says "unknown" or "estimated", never a provider's zeros.
    # (The reservation is kept either way, B08.4; a failure never releases it.)
    if result.usage.total_tokens == 0:
        assert result.usage_source in ("unknown", "estimated")


_CHECKS: dict[Scenario, Callable[[AgentStepResult, StepContext], None]] = {
    Scenario.COMPLETES: _completes,
    Scenario.PROPOSES_PROTECTED_TOOL: _proposes,
    Scenario.PROVIDER_ERROR: _fails_quietly,
    Scenario.PROVIDER_TIMEOUT: _fails_quietly,
    Scenario.UNREADABLE_OUTPUT: _fails_quietly,
    Scenario.CLAIMS_COMMIT: _fails_quietly,
    Scenario.ANOTHER_STEPS_RESULT: _fails_quietly,
}
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
