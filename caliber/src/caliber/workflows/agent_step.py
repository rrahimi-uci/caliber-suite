"""The agent-step adapter boundary: one bounded step in, one validated result out, never an exception (B08.1b, #720).

:mod:`caliber.contracts.agent_step` (B08.1a) says what a step is asked and what it answers. This module is the seam between that
contract and a model provider:

* :class:`AgentStepAdapter` is the protocol an executor implements to take part in *governed* execution:
  ``run_step(request, step_input) -> AgentStepResult``. An adapter **proposes**; it never executes a protected tool (it is
  only handed the callables of tools the platform has already classified ``read``, in :attr:`AgentStepInput.read_tools`),
  never says an effect happened (the result has no field for it) and never raises for a provider failure: it answers with
  a typed ``refused`` or ``error`` result.
* :func:`run_agent_step` is the one entry the platform calls. It validates the request **before** the adapter is touched, and
  then treats everything the adapter returns, or raises, as untrusted: a result is rebuilt from its own record through
  :func:`~caliber.contracts.agent_step.parse_step_result` (schema, proposed-action intents, and that it answers *this* step:
  same ``step_request_id`` and the same run, node, occurrence and attempt), and an exception, a timeout or a malformed
  result becomes a bounded ``error`` result whose ``error_code`` is a fixed token. **No message, exception text or provider
  payload ever reaches a result or a log line**; only the exception's class name is logged.
* :class:`StepScript` is the deterministic, offline scripted adapter :class:`~caliber.workflows.runtime.FakeWorkflowExecutor`
  uses (``step_script=``): it answers a request from a list of scripted outcomes, with no clock, no network and no tool call.

**Refusals and failures are results, and they keep the reservation.** A ``refused`` or ``error`` result carries zero usage with
``usage_source`` ``unknown`` unless the adapter knows better: that is "the provider may have been called and nothing is
known", not "nothing was used". B08.4 keeps a conservative charge for every such result; nothing here releases a token
reservation.

**Not here.** Limits (turns, tool calls, tokens, deadline) are enforced by the platform before work and against the returned
usage in B08.3, the token reservation is B08.4, and mapping a model's tool call to a :class:`ProposedAction` bound to the
protected-effect inventory is B08.2. The four provider adapters (B08.openai-chat, -responses, -agents, anthropic) replace their
:data:`ADAPTER_NOT_INTEGRATED` refusal in ``caliber.workflows.runtime`` one by one; until then they refuse governed execution.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final, Protocol, runtime_checkable

from caliber.contracts.agent_step import (
    AgentStepRequest,
    AgentStepResult,
    ProposedAction,
    StepUsage,
    parse_step_request,
    parse_step_result,
)
from caliber.contracts.registry import ContractValidationError, UnsupportedSchemaVersion
from caliber.effects.records import ProposalError
from caliber.workflows.ir import IRAgent
from caliber.workflows.session_memory import HistoryMessage

logger = logging.getLogger("caliber.workflows.agent_step")

#: The executor has no governed step yet (a provider adapter that B08 has not integrated): refused, nothing was called.
ADAPTER_NOT_INTEGRATED: Final = "adapter_not_integrated"
#: The object handed to :func:`run_agent_step` does not implement :class:`AgentStepAdapter`: refused, nothing was called.
ADAPTER_UNSUPPORTED: Final = "adapter_unsupported"
#: The adapter raised. The provider may have been called, so the usage is unknown.
ADAPTER_ERROR: Final = "adapter_error"
#: The adapter (or the provider under it) timed out: the call's outcome is unknown, so is its usage.
ADAPTER_TIMEOUT: Final = "adapter_timeout"
#: The adapter returned, or built, something that is not a valid result for this step: a schema violation (an unknown field, a
#: claimed commit, a bad effect class), a result for another step, or a proposal whose intent cannot be canonicalised.
MALFORMED_RESULT: Final = "malformed_result"
#: A scripted adapter was asked for more steps than it was given (a test bug, never a silent pass).
SCRIPT_EXHAUSTED: Final = "script_exhausted"

#: The completion tokens the scripted adapter's default result reports (the prompt is one token a word of input).
_DEFAULT_REPLY_TOKENS: Final = 8

StepHandoffAgents = Mapping[str, tuple[IRAgent, Mapping[str, Callable[..., Any]]]]


@dataclass(frozen=True)
class AgentStepInput:
    """What the platform hands an adapter besides the request: the agent, its input and the tools it may *execute*.

    ``read_tools`` holds the callables of the tools the platform has classified ``read`` in the protected-effect inventory
    (never the model's own claim): the only tools an adapter may run in its loop. A tool the model calls that is *not* in it
    is not run; the adapter proposes it (B08.2) and stops. ``handoff_agents`` is what the agent may hand off to, as
    :data:`~caliber.workflows.runtime.ResolvedHandoffAgents`.
    """

    agent: IRAgent
    input_text: str
    history: tuple[HistoryMessage, ...] = ()
    read_tools: Mapping[str, Callable[..., Any]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    handoff_agents: StepHandoffAgents | None = None


@runtime_checkable
class AgentStepAdapter(Protocol):
    """An executor that can run one governed agent step.

    Implementations return a typed :class:`~caliber.contracts.agent_step.AgentStepResult` that repeats the request's
    ``step_request_id`` and ``(run, node, occurrence, attempt)`` tuple. They never execute a tool outside
    :attr:`AgentStepInput.read_tools`, never make an effect happen and never raise for a provider failure: a provider error,
    a timeout or output they cannot read is a ``refused`` or ``error`` result (use :func:`refusal` and :func:`failure`). The
    platform calls :func:`run_agent_step`, which does not trust any of that and checks it.
    """

    def run_step(
        self, request: AgentStepRequest, step_input: AgentStepInput
    ) -> AgentStepResult: ...


def answer(
    request: AgentStepRequest,
    stop_reason: str,
    *,
    usage: StepUsage | None = None,
    proposed_actions: Iterable[ProposedAction] = (),
    usage_source: str | None = None,
    error_code: str | None = None,
) -> AgentStepResult:
    """A result for ``request``: its identity is copied from the request, so it cannot answer another step."""
    return AgentStepResult(
        step_request_id=request.step_request_id,
        workflow_run_id=request.workflow_run_id,
        node_id=request.node_id,
        occurrence_id=request.occurrence_id,
        attempt=request.attempt,
        stop_reason=stop_reason,
        usage=usage if usage is not None else StepUsage(0, 0, 0, 0, 0),
        proposed_actions=tuple(proposed_actions),
        usage_source=usage_source,
        error_code=error_code,
    )


def refusal(request: AgentStepRequest, error_code: str) -> AgentStepResult:
    """``refused``: the step was not run. Zero usage, source ``unknown``: the reservation is kept, not released (B08.4)."""
    return answer(request, "refused", usage_source="unknown", error_code=error_code)


def failure(
    request: AgentStepRequest, error_code: str, *, usage: StepUsage | None = None
) -> AgentStepResult:
    """``error``: the step failed. Usage is what the adapter knows, else zero from source ``unknown`` (never invented)."""
    return answer(request, "error", usage=usage, usage_source="unknown", error_code=error_code)


def _failure_code(exc: Exception) -> str:
    if isinstance(exc, TimeoutError):
        return ADAPTER_TIMEOUT
    if isinstance(exc, ContractValidationError | UnsupportedSchemaVersion | ProposalError):
        return MALFORMED_RESULT
    return ADAPTER_ERROR


def run_agent_step(
    adapter: object, request: AgentStepRequest, step_input: AgentStepInput
) -> AgentStepResult:
    """Run one governed step through ``adapter`` and return a validated result; never an exception from the adapter.

    ``request`` is validated first (``ContractValidationError`` and the adapter is not called when it is not a valid
    ``agent_step_request`` v1: nothing has run). An ``adapter`` without ``run_step`` is refused (:data:`ADAPTER_UNSUPPORTED`).
    Otherwise the adapter is called once, with the validated request, and whatever it returns is rebuilt from its own record
    so that the caller gets only a schema-valid result that answers this very step, whose proposed actions are the model's
    unbound claims (:attr:`ProposedAction.platform_bound` is false). A raise, a timeout, a non-result or a result that fails
    that check is an ``error`` result with a fixed ``error_code`` (:data:`ADAPTER_ERROR`, :data:`ADAPTER_TIMEOUT`,
    :data:`MALFORMED_RESULT`) and no text from the failure. Interrupts (``KeyboardInterrupt``, ``SystemExit``) propagate.
    """
    checked = parse_step_request(request.record())
    if not isinstance(adapter, AgentStepAdapter):
        return refusal(checked, ADAPTER_UNSUPPORTED)
    try:
        returned = adapter.run_step(checked, step_input)
    except Exception as exc:
        logger.warning(
            "agent step %s: adapter %s raised %s",
            checked.step_request_id,
            type(adapter).__name__,
            type(exc).__name__,
        )
        return failure(checked, _failure_code(exc))
    try:
        if not isinstance(returned, AgentStepResult):
            raise ContractValidationError(
                "agent_step_result", ["result: is not an AgentStepResult"]
            )
        return parse_step_result(returned.record(), checked)
    except Exception:
        # The adapter's object is untrusted (it may be a subclass or hold a non-action): any failure to read it is the
        # same typed result, and its text, which could quote the payload, goes nowhere.
        logger.warning(
            "agent step %s: adapter %s returned an invalid result",
            checked.step_request_id,
            type(adapter).__name__,
        )
        return failure(checked, MALFORMED_RESULT)


@dataclass(frozen=True)
class ScriptedStep:
    """One scripted outcome: the answer a scripted adapter gives to the next request, with the request's identity."""

    stop_reason: str = "completed"
    proposed_actions: tuple[ProposedAction, ...] = ()
    usage: StepUsage = field(default_factory=lambda: StepUsage(0, 0, 0, 0, 0))
    usage_source: str | None = None
    error_code: str | None = None


#: A scripted outcome. A :class:`ScriptedStep` is answered with the request's identity. An exception is raised (a provider
#: error or timeout). A callable gets the request and returns a raw ``agent_step_result`` record, which is read with
#: :func:`~caliber.contracts.agent_step.parse_step_result` like real provider output (so a bad record, a claimed commit or
#: a result for another step is refused the way an adapter's own parse refuses it).
ScriptEntry = ScriptedStep | Exception | Callable[[AgentStepRequest], Mapping[str, Any]]


class StepScript:
    """Deterministic scripted answers for :class:`AgentStepAdapter`: no clock, no network, no tool is ever called.

    Without entries it answers every request with the same ``completed`` result (usage estimated from the input's word
    count). With entries it answers in order, and when they run out it refuses with :data:`SCRIPT_EXHAUSTED`, so a test that
    asks for more steps than it scripted fails loudly instead of passing on a default.
    """

    def __init__(self, entries: Iterable[ScriptEntry] | None = None) -> None:
        self._entries: deque[ScriptEntry] | None = None if entries is None else deque(entries)

    @property
    def remaining(self) -> int | None:
        """Scripted outcomes not yet used; ``None`` when there is no script (every request gets the default)."""
        return None if self._entries is None else len(self._entries)

    def answer(self, request: AgentStepRequest, step_input: AgentStepInput) -> AgentStepResult:
        if self._entries is None:
            prompt = len(step_input.input_text.split())
            return answer(
                request,
                "completed",
                usage=StepUsage(
                    prompt_tokens=prompt,
                    completion_tokens=_DEFAULT_REPLY_TOKENS,
                    total_tokens=prompt + _DEFAULT_REPLY_TOKENS,
                    turns=1,
                    tool_calls=0,
                ),
                usage_source="estimated",
            )
        if not self._entries:
            return refusal(request, SCRIPT_EXHAUSTED)
        entry = self._entries.popleft()
        if isinstance(entry, Exception):
            raise entry
        if isinstance(entry, ScriptedStep):
            return answer(
                request,
                entry.stop_reason,
                usage=entry.usage,
                proposed_actions=entry.proposed_actions,
                usage_source=entry.usage_source,
                error_code=entry.error_code,
            )
        return parse_step_result(entry(request), request)


__all__ = [
    "ADAPTER_ERROR",
    "ADAPTER_NOT_INTEGRATED",
    "ADAPTER_TIMEOUT",
    "ADAPTER_UNSUPPORTED",
    "MALFORMED_RESULT",
    "SCRIPT_EXHAUSTED",
    "AgentStepAdapter",
    "AgentStepInput",
    "ScriptEntry",
    "ScriptedStep",
    "StepHandoffAgents",
    "StepScript",
    "answer",
    "failure",
    "refusal",
    "run_agent_step",
]
