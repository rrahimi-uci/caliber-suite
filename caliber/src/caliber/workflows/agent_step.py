"""The agent-step adapter boundary: one bounded step in, one validated result out, never an exception (B08.1b, #720).

:mod:`caliber.contracts.agent_step` (B08.1a) says what a step is asked and what it answers. This module is the seam between that
contract and a model provider:

* :class:`AgentStepAdapter` is the protocol an executor implements to take part in *governed* execution:
  ``run_step(request, step_input) -> AgentStepResult``. An adapter **proposes**; it never executes a protected tool (it is
  only handed the callables of tools the platform has already classified ``read``, in :attr:`AgentStepInput.read_tools` and
  :attr:`StepHandoffTarget.read_tools`), never says an effect happened (the result has no field for it) and never raises for
  a provider failure: it answers with a typed ``refused`` or ``error`` result.
* :func:`run_agent_step` is the one entry the platform calls. It validates the request **before** the adapter is touched, and
  then treats everything the adapter returns, or raises, as untrusted. A result must be exactly an
  :class:`~caliber.contracts.agent_step.AgentStepResult` (no subclass: a subclass can override ``record()``) holding exactly
  the contract's types; the platform then takes **one plain snapshot** of its record (a JSON round trip) and reads only
  that snapshot through :func:`~caliber.contracts.agent_step.parse_step_result` (schema, proposed-action intents, and that it
  answers *this* step: same ``step_request_id`` and the same run, node, occurrence and attempt). No method of an
  adapter-supplied object is called after the snapshot. An exception, a timeout, a coroutine, or a malformed result becomes
  a bounded ``error`` or ``refused`` result whose ``error_code`` is a fixed token. **No exception text and no provider
  payload ever reaches a result or a log line**; the exception's class name is logged, and when a result is dropped as
  malformed the usage *numbers* it reported are logged (B08.4 settles a conservative charge from the request, not from them).
* :class:`StepScript` is the deterministic, offline scripted adapter :class:`~caliber.workflows.runtime.FakeWorkflowExecutor`
  uses (``step_script=``): it answers a request from a list of scripted outcomes, with no clock, no network and no tool call
  beyond the read tools a scripted step names.

**Refusals and failures are results, and they keep the reservation.** A ``refused`` or ``error`` result the boundary creates
carries zero usage with ``usage_source`` ``unknown``: that is "the provider may have been called and nothing is known", not
"nothing was used". B08.4 keeps a conservative charge for every such result. The one deliberate exception is
:data:`NOT_CALLED_CODES` (``adapter_unsupported``, ``adapter_not_integrated``): the adapter body never ran, so B08.4 may
release that reservation, and only on those codes. B08.4 must keep the reservation on any ``BaseException`` that escapes
(``asyncio.CancelledError``, ``GeneratorExit``, ``KeyboardInterrupt``, ``SystemExit`` propagate out of :func:`run_agent_step`
because the provider call may have happened).

**Timeouts.** The boundary maps ``TimeoutError`` (and ``asyncio.TimeoutError``) to ``adapter_timeout`` and nothing else: it
does not know a provider's exception types. A provider adapter (#727-#730) maps its own SDK timeouts (openai, anthropic,
httpx) to ``failure(request, ADAPTER_TIMEOUT)`` itself.

**Not here.** Limits (turns, tool calls, tokens, deadline) are enforced by the platform before work and against the returned
usage in B08.3, the token reservation is B08.4, and mapping a model's tool call to a :class:`ProposedAction` bound to the
protected-effect inventory is B08.2. The four provider adapters (B08.openai-chat, -responses, -agents, anthropic) replace their
:data:`ADAPTER_NOT_INTEGRATED` refusal in ``caliber.workflows.runtime`` one by one; until then they refuse governed execution.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final, Protocol, runtime_checkable

from caliber.contracts.agent_step import (
    REQUEST_KIND,
    RESULT_KIND,
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
#: The object handed to :func:`run_agent_step` does not implement :class:`AgentStepAdapter` (or its ``run_step`` is a coroutine
#: function, which was never awaited): refused, nothing was called.
ADAPTER_UNSUPPORTED: Final = "adapter_unsupported"
#: The adapter raised. The provider may have been called, so the usage is unknown.
ADAPTER_ERROR: Final = "adapter_error"
#: The adapter (or the provider under it) timed out: the call's outcome is unknown, so is its usage.
ADAPTER_TIMEOUT: Final = "adapter_timeout"
#: The adapter returned, or built, something that is not a valid result for this step: a schema violation (an unknown field, a
#: claimed commit, a bad effect class), a result for another step, or a proposal whose intent cannot be canonicalised.
MALFORMED_RESULT: Final = "malformed_result"
#: The provider failed and the adapter reported it (the code an adapter uses for a provider error it caught itself).
PROVIDER_ERROR: Final = "provider_error"
#: A scripted step named a tool that is not a read tool of the step: it was not run.
TOOL_NOT_READ_CLASS: Final = "tool_not_read_class"
#: A read tool a scripted step ran raised.
TOOL_ERROR: Final = "tool_error"
#: A scripted adapter was asked for more steps than it was given (a test bug, never a silent pass).
SCRIPT_EXHAUSTED: Final = "script_exhausted"
#: Codes of a step whose adapter body never ran: B08.4 may release such a reservation deliberately; any other result keeps it.
NOT_CALLED_CODES: Final = frozenset({ADAPTER_UNSUPPORTED, ADAPTER_NOT_INTEGRATED})

#: The completion tokens the scripted adapter's default result reports (the prompt is one token a word of input).
_DEFAULT_REPLY_TOKENS: Final = 8
_TIMEOUTS: Final = (TimeoutError, asyncio.TimeoutError)


def _check_read_tools(
    owner: IRAgent, tools: Mapping[str, Callable[..., Any]]
) -> Mapping[str, Callable[..., Any]]:
    """A read-only copy of ``tools``; every name must be a tool bound to ``owner`` and every value callable."""
    if not isinstance(tools, Mapping):
        raise TypeError("read_tools must be a mapping of tool name to callable")
    bound = {binding.local_name for binding in owner.tools}
    copy: dict[str, Callable[..., Any]] = {}
    for name, tool in tools.items():
        if not isinstance(name, str) or name not in bound:
            raise ValueError(f"read tool {name!r} is not a tool of agent {owner.node_id!r}")
        if not callable(tool):
            raise TypeError(f"read tool {name!r} is not callable")
        copy[name] = tool
    return MappingProxyType(copy)


def filter_read_tools(
    tool_callables: Mapping[str, Callable[..., Any]], is_read: Callable[[str], bool]
) -> dict[str, Callable[..., Any]]:
    """The callables ``is_read`` admits: the one classification a platform applies to a step's tools and to its handoff targets'.

    ``is_read`` is the protected-effect inventory's answer (``effect_class(name) == "read"``), never the model's. It fails
    closed: a tool for which it raises, or returns anything but ``True``, is left out.
    """
    admitted: dict[str, Callable[..., Any]] = {}
    for name, tool in tool_callables.items():
        try:
            allowed = is_read(name) is True
        except Exception:
            allowed = False
        if allowed:
            admitted[name] = tool
    return admitted


@dataclass(frozen=True)
class StepHandoffTarget:
    """An agent the step's agent may hand off to, with only the read-class callables of *its* tools.

    The runtime's own handoff maps carry every tool callable of the target; those are not accepted here (the type is the
    guard): the platform builds ``read_tools`` with :func:`filter_read_tools`, the classification it used for the step.
    """

    agent: IRAgent
    read_tools: Mapping[str, Callable[..., Any]] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "read_tools", _check_read_tools(self.agent, self.read_tools))


StepHandoffAgents = Mapping[str, StepHandoffTarget]


@dataclass(frozen=True)
class AgentStepInput:
    """What the platform hands an adapter besides the request: the agent, its input and the tools it may *execute*.

    ``read_tools`` holds the callables of the tools the platform has classified ``read`` in the protected-effect inventory
    (never the model's own claim, see :func:`filter_read_tools`): the only tools an adapter may run in its loop, and each name
    must be a tool bound to ``agent``. A tool the model calls that is *not* in it is not run; the adapter proposes it (B08.2)
    and stops. ``handoff_agents`` is what the agent may hand off to, each with its own read-class callables only
    (:class:`StepHandoffTarget`). Construction copies every container into a read-only one, so what the platform built cannot
    be changed through what an adapter holds, and ``history`` is copied message by message.
    """

    agent: IRAgent
    input_text: str
    history: tuple[HistoryMessage, ...] = ()
    read_tools: Mapping[str, Callable[..., Any]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    handoff_agents: StepHandoffAgents | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "history", tuple(dict(message) for message in self.history))
        object.__setattr__(self, "read_tools", _check_read_tools(self.agent, self.read_tools))
        if self.handoff_agents is not None:
            targets: dict[str, StepHandoffTarget] = {}
            for node_id, target in self.handoff_agents.items():
                if not isinstance(target, StepHandoffTarget):
                    raise TypeError("handoff_agents must map a node id to a StepHandoffTarget")
                if target.agent.node_id != node_id:
                    raise ValueError(f"handoff target {node_id!r} names another agent")
                targets[node_id] = target
            object.__setattr__(self, "handoff_agents", MappingProxyType(targets))


@runtime_checkable
class AgentStepAdapter(Protocol):
    """An executor that can run one governed agent step.

    Implementations return a typed :class:`~caliber.contracts.agent_step.AgentStepResult` that repeats the request's
    ``step_request_id`` and ``(run, node, occurrence, attempt)`` tuple. They never execute a tool outside
    :attr:`AgentStepInput.read_tools`, never make an effect happen and never raise for a provider failure: a provider error,
    a timeout or output they cannot read is a ``refused`` or ``error`` result (use :func:`refusal` and :func:`failure`;
    map the provider's own timeout exceptions to :data:`ADAPTER_TIMEOUT`). ``run_step`` is a plain function, not a coroutine
    function. The platform calls :func:`run_agent_step`, which does not trust any of that and checks it.
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
    if isinstance(exc, _TIMEOUTS):
        return ADAPTER_TIMEOUT
    if isinstance(exc, ContractValidationError | UnsupportedSchemaVersion | ProposalError):
        return MALFORMED_RESULT
    return ADAPTER_ERROR


def _malformed(reason: str) -> ContractValidationError:
    return ContractValidationError(RESULT_KIND, [reason])


def _exactly(value: object, kind: type | tuple[type, ...], where: str) -> None:
    kinds = kind if isinstance(kind, tuple) else (kind,)
    if type(value) not in kinds:
        raise _malformed(f"{where}: has an unexpected type")


def _snapshot(returned: object) -> dict[str, Any]:
    """One plain snapshot of a result's record, taken from a result that is exactly the contract's types.

    A subclass of :class:`AgentStepResult`, :class:`StepUsage` or :class:`ProposedAction`, or a field that is a subclass of
    ``str`` or ``int``, can run its own code when it is read or validated, and could answer differently each time it is
    asked. None is accepted: with exact types ``record()`` is the contract's own and returns plain data, which is then
    copied through JSON (no ``default`` hook, no NaN) so that what is validated and what is read are the same bytes.
    """
    if type(returned) is not AgentStepResult:
        raise _malformed("result: is not exactly an AgentStepResult")
    for name in ("step_request_id", "workflow_run_id", "node_id", "occurrence_id", "stop_reason"):
        _exactly(getattr(returned, name), str, name)
    _exactly(returned.attempt, int, "attempt")
    _exactly(returned.usage_source, (str, type(None)), "usage_source")
    _exactly(returned.error_code, (str, type(None)), "error_code")
    usage = returned.usage
    _exactly(usage, StepUsage, "usage")
    for name in ("prompt_tokens", "completion_tokens", "total_tokens", "turns", "tool_calls"):
        _exactly(getattr(usage, name), int, f"usage/{name}")
    _exactly(usage.cached_prompt_tokens, (int, type(None)), "usage/cached_prompt_tokens")
    _exactly(returned.proposed_actions, tuple, "proposed_actions")
    for index, action in enumerate(returned.proposed_actions):
        where = f"proposed_actions/{index}"
        _exactly(action, ProposedAction, where)
        for name in ("connector", "connector_version", "effect_class", "resource"):
            _exactly(getattr(action, name), str, f"{where}/{name}")
        _exactly(
            action.expected_resource_version,
            (str, type(None)),
            f"{where}/expected_resource_version",
        )
    snapshot = json.loads(json.dumps(returned.record(), allow_nan=False))
    if type(snapshot) is not dict:
        raise _malformed("result: is not a record")
    return snapshot


def _dropped_usage(returned: object) -> str:
    """The usage figures a dropped result reported, as numbers only; empty when it has none that are plain integers."""
    usage = getattr(returned, "usage", None) if type(returned) is AgentStepResult else None
    if type(usage) is not StepUsage:
        return ""
    figures = (
        usage.prompt_tokens,
        usage.completion_tokens,
        usage.total_tokens,
        usage.turns,
        usage.tool_calls,
    )
    if any(type(figure) is not int for figure in figures):
        return ""
    prompt, completion, total, turns, calls = figures
    return (
        f" (reported usage: prompt={prompt} completion={completion} total={total} "
        f"turns={turns} tool_calls={calls})"
    )


def run_agent_step(
    adapter: object, request: AgentStepRequest, step_input: AgentStepInput
) -> AgentStepResult:
    """Run one governed step through ``adapter`` and return a validated result; never an exception from the adapter.

    ``request`` is validated first (``ContractValidationError`` and the adapter is not called when it is not a valid
    ``agent_step_request`` v1, or when ``step_input.agent`` is not the request's node: nothing has run). An ``adapter``
    without ``run_step``, or whose ``run_step`` returns a coroutine (it is closed, never awaited), is refused
    (:data:`ADAPTER_UNSUPPORTED`). Otherwise the adapter is called once, with the validated request, and what it returns is
    read only through one plain snapshot (:func:`_snapshot`), so the caller gets a schema-valid result that answers this very
    step, rebuilt by the platform, whose proposed actions are the model's unbound claims
    (:attr:`ProposedAction.platform_bound` is false). A raise, a timeout, a non-result or a result that fails that check is an
    ``error`` result with a fixed ``error_code`` (:data:`ADAPTER_ERROR`, :data:`ADAPTER_TIMEOUT`, :data:`MALFORMED_RESULT`)
    and no text from the failure. A ``BaseException`` (``KeyboardInterrupt``, ``SystemExit``, ``asyncio.CancelledError``)
    propagates, and the caller keeps the step's token reservation.
    """
    checked = parse_step_request(request.record())
    if step_input.agent.node_id != checked.node_id:
        raise ContractValidationError(
            REQUEST_KIND, ["node_id: the step input's agent is not the request's node"]
        )
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
    if inspect.iscoroutine(returned):
        returned.close()
        logger.warning(
            "agent step %s: adapter %s returned a coroutine; run_step must be a plain function",
            checked.step_request_id,
            type(adapter).__name__,
        )
        return refusal(checked, ADAPTER_UNSUPPORTED)
    try:
        return parse_step_result(_snapshot(returned), checked)
    except Exception:
        # The adapter's object is untrusted: any failure to read it is the same typed result, and its text, which could
        # quote the payload, goes nowhere. The usage numbers it reported are kept in the log for the reservation's sake.
        logger.warning(
            "agent step %s: adapter %s returned an invalid result%s",
            checked.step_request_id,
            type(adapter).__name__,
            _dropped_usage(returned),
        )
        return failure(checked, MALFORMED_RESULT)


@dataclass(frozen=True)
class ScriptedStep:
    """One scripted outcome: the answer a scripted adapter gives to the next request, with the request's identity.

    ``read_tool_calls`` are ``(tool name, arguments)`` pairs the scripted model calls first: each runs through
    :attr:`AgentStepInput.read_tools` (a name that is not there is not run and the step is refused with
    :data:`TOOL_NOT_READ_CLASS`). The ``usage`` is the script's, not recounted.
    """

    stop_reason: str = "completed"
    proposed_actions: tuple[ProposedAction, ...] = ()
    usage: StepUsage = field(default_factory=lambda: StepUsage(0, 0, 0, 0, 0))
    usage_source: str | None = None
    error_code: str | None = None
    read_tool_calls: tuple[tuple[str, Mapping[str, Any]], ...] = ()


#: A scripted outcome. A :class:`ScriptedStep` is answered with the request's identity. An exception stages a provider
#: failure: the adapter *returns* a typed ``error`` result for it (:data:`ADAPTER_TIMEOUT` for a ``TimeoutError``, else
#: :data:`PROVIDER_ERROR`), as a conformant adapter must. A callable gets the request and returns a raw ``agent_step_result``
#: record, which is read with :func:`~caliber.contracts.agent_step.parse_step_result` like real provider output (a bad record, a
#: claimed commit or a result for another step is :data:`MALFORMED_RESULT`).
ScriptEntry = ScriptedStep | Exception | Callable[[AgentStepRequest], Mapping[str, Any]]
ScriptEntries = Iterable[ScriptEntry]


class StepScript:
    """Deterministic scripted answers for :class:`AgentStepAdapter`: no clock, no network, no protected tool is ever called.

    Without entries it answers every request with the same ``completed`` result (usage estimated from the input's word
    count). With entries it answers in order, and when they run out it refuses with :data:`SCRIPT_EXHAUSTED`, so a test that
    asks for more steps than it scripted fails loudly instead of passing on a default. Concurrent callers each get a
    different entry.
    """

    def __init__(self, entries: ScriptEntries | None = None) -> None:
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
        try:
            entry = self._entries.popleft()
        except IndexError:
            return refusal(request, SCRIPT_EXHAUSTED)
        return self._play(entry, request, step_input)

    @staticmethod
    def _play(
        entry: ScriptEntry, request: AgentStepRequest, step_input: AgentStepInput
    ) -> AgentStepResult:
        if isinstance(entry, Exception):
            return failure(
                request, ADAPTER_TIMEOUT if isinstance(entry, _TIMEOUTS) else PROVIDER_ERROR
            )
        if isinstance(entry, ScriptedStep):
            for name, arguments in entry.read_tool_calls:
                tool = step_input.read_tools.get(name)
                if tool is None:
                    return refusal(request, TOOL_NOT_READ_CLASS)
                try:
                    tool(**arguments)
                except Exception:
                    return failure(request, TOOL_ERROR)
            return answer(
                request,
                entry.stop_reason,
                usage=entry.usage,
                proposed_actions=entry.proposed_actions,
                usage_source=entry.usage_source,
                error_code=entry.error_code,
            )
        try:
            return parse_step_result(entry(request), request)
        except (ContractValidationError, UnsupportedSchemaVersion, ProposalError):
            return failure(request, MALFORMED_RESULT)


__all__ = [
    "ADAPTER_ERROR",
    "ADAPTER_NOT_INTEGRATED",
    "ADAPTER_TIMEOUT",
    "ADAPTER_UNSUPPORTED",
    "MALFORMED_RESULT",
    "NOT_CALLED_CODES",
    "PROVIDER_ERROR",
    "SCRIPT_EXHAUSTED",
    "TOOL_ERROR",
    "TOOL_NOT_READ_CLASS",
    "AgentStepAdapter",
    "AgentStepInput",
    "AgentStepRequest",
    "AgentStepResult",
    "ProposedAction",
    "ScriptEntries",
    "ScriptEntry",
    "ScriptedStep",
    "StepHandoffAgents",
    "StepHandoffTarget",
    "StepScript",
    "StepUsage",
    "answer",
    "failure",
    "filter_read_tools",
    "refusal",
    "run_agent_step",
]
