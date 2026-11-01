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
  malformed the usage *numbers* it reported are logged (the reservation ledger charges the whole reservation for it, not those numbers).
* :class:`StepScript` is the deterministic, offline scripted adapter :class:`~caliber.workflows.runtime.FakeWorkflowExecutor`
  uses (``step_script=``): it answers a request from a list of scripted outcomes, with no clock, no network and no tool call
  beyond the read tools a scripted step names.

**Refusals and failures are results, and they keep the reservation.** A ``refused`` or ``error`` result the boundary creates
carries zero usage with ``usage_source`` ``unknown``: that is "the provider may have been called and nothing is known", not
"nothing was used". The reservation ledger (:mod:`caliber.budget.step_reservation`, B08.4) charges the whole reservation for every
such result. :data:`NOT_CALLED_CODES` (``adapter_unsupported``, ``adapter_not_integrated``) name a body that never ran, but the
ledger does not infer "not called" from a result: the interpreter decides before it reserves (an executor that is not an adapter,
and a step :func:`before_work_stop` stops, reserve nothing). The reservation stays open on any ``BaseException`` that escapes
(``asyncio.CancelledError``, ``GeneratorExit``, ``KeyboardInterrupt``, ``SystemExit`` propagate out of :func:`run_agent_step`
because the provider call may have happened).

**Timeouts.** The boundary maps ``TimeoutError`` (and ``asyncio.TimeoutError``) to ``adapter_timeout`` and nothing else: it
does not know a provider's exception types. A provider adapter (#727-#730) maps its own SDK timeouts (openai, anthropic,
httpx) to ``failure(request, ADAPTER_TIMEOUT)`` itself.

**Limits are the platform's, not the adapter's (B08.3, #752).** :func:`run_agent_step` enforces the request's turns, tool
calls, tokens and deadline itself, for every adapter alike, and an adapter cannot raise them: the limits are read from the
request the boundary validated, never from the result. *Before work* (:func:`before_work_stop`) a step with no turns or tokens
to spend, or whose deadline has passed, stops without calling the adapter (``max_turns``, ``max_tokens`` or ``deadline``, zero
usage, ``usage_source`` ``estimated``: nothing was called, and no reservation is made for it). *During work* every read
tool is wrapped so the platform counts the calls itself and refuses the one past ``max_tool_calls``
(:class:`StepToolBudgetExceeded`) or one started at or after the deadline (:class:`StepDeadlineReached`); an adapter that
swallows a refusal still has it held against it. *After work* a ``completed`` or
``proposed_action`` result is replaced, and its proposal dropped, with the limit's own stop reason (the order is ``max_turns``,
``max_tool_calls``, ``max_tokens``, ``deadline``) when the usage it reported is over a limit, when the platform counted more
tool calls than the limit, or when the platform's own clock says the step finished after its deadline; the usage kept is the
reported one, with ``tool_calls`` raised to the platform's own count when that is higher. A limit is exceeded only when
*passed*: spending exactly ``max_tokens`` is within it. Turns and tokens are provider-reported and the platform cannot observe
them: they bound what is *recorded* and stop the proposal, while an adapter's own in-loop checks (defence in depth) bound what is
*spent*. The deadline is read from an injectable :class:`StepClock` (wall time for the absolute deadline, a monotonic clock for
how long the step took, so a clock that is stepped mid-step cannot extend it). Nothing here carries state between calls: a retry
is a new attempt with a fresh budget and a resumed step that finds its proposal is not asked again; the reservation ledger sums
attempts against the run (B08.4).

**Not here.** The token reservation (:mod:`caliber.budget.step_reservation`, B08.4; the interpreter takes it before this boundary
is called and settles it after) and the mapping of a model's tool call to a :class:`ProposedAction` bound to the
protected-effect inventory (B08.2) are the platform's, outside this boundary. The four provider adapters (B08.openai-chat, -responses, -agents, anthropic) replace their
:data:`ADAPTER_NOT_INTEGRATED` refusal in ``caliber.workflows.runtime`` one by one; until then they refuse governed execution.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Final, Protocol, runtime_checkable

from caliber.config import (
    AGENT_STEP_MAX_SECONDS_BOUNDS,
    AGENT_STEP_MAX_SECONDS_DEFAULT,
    AGENT_STEP_MAX_TOKENS_BOUNDS,
    AGENT_STEP_MAX_TOKENS_DEFAULT,
    AGENT_STEP_MAX_TOOL_CALLS_BOUNDS,
    AGENT_STEP_MAX_TOOL_CALLS_DEFAULT,
    AGENT_STEP_MAX_TURNS_BOUNDS,
    AGENT_STEP_MAX_TURNS_DEFAULT,
    CaliberConfig,
)
from caliber.contracts.agent_step import (
    REQUEST_KIND,
    RESULT_KIND,
    UNCLAIMED,
    UNCLAIMED_EFFECT_CLASS,
    AgentStepRequest,
    AgentStepResult,
    ProposedAction,
    StepLimits,
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
#: Codes of a step whose adapter body never ran. Informational: the reservation ledger (B08.4) never releases on a result (the interpreter reserves nothing for a step it knows will not call an adapter), so these keep their charge too.
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


#: The only fields of the arguments a model gives when it calls a protected tool (B04.6, #768). The model supplies the tool's own
#: arguments under ``args``; it may name the destination (``resource``, for a tool whose destination is the model's to choose within
#: a connector's pattern) and the version it expects. It supplies **no** connector, connector version or effect class: the platform
#: derives them from the called tool's trusted identity, and a model that volunteers one is unreadable (``malformed_result``).
PROPOSAL_REQUIRED: Final = ("args",)
PROPOSAL_OPTIONAL: Final = ("resource", "expected_resource_version")
PROPOSAL_FIELDS: Final = frozenset(PROPOSAL_REQUIRED + PROPOSAL_OPTIONAL)
#: The sentence every adapter adds to a protected tool's description, so the model knows what it is, and is not, asked for.
PROPOSAL_DESCRIPTION: Final = (
    "Calling this does not run the tool: it proposes the action for approval. Give only the tool's arguments in `args`; "
    "the platform decides the connector, its version and the effect class."
)


def proposal_parameters(args_schema: Mapping[str, Any]) -> dict[str, Any]:
    """The parameters schema a protected tool is offered with: its own arguments under ``args``, and nothing the model could claim."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(PROPOSAL_REQUIRED),
        "properties": {
            "args": dict(args_schema),
            "resource": {
                "type": ["string", "null"],
                "minLength": 1,
                "maxLength": 512,
                "description": "Only when the tool asks you to name the destination; otherwise null.",
            },
            "expected_resource_version": {
                "type": ["string", "null"],
                "minLength": 1,
                "maxLength": 256,
                "description": "The version of the resource you expect, or null.",
            },
        },
    }


def proposal_from_call(tool: str, envelope: object) -> ProposedAction:
    """The model's call of the protected tool ``tool`` as a proposal that names it; ``ValueError`` when the call is not one.

    The action carries the **called function's name** (:attr:`ProposedAction.tool`), so the platform binds it from that tool's
    trusted identity; its connector, version and class are the placeholders (:data:`~caliber.contracts.agent_step.UNCLAIMED`),
    never a model's claim. An unknown field (a model that volunteers ``effect_class``, ``connector``, ``state`` or ``receipt_id``),
    a missing ``args`` or a value that cannot be built into an intent is refused here, before the platform sees anything.
    """
    if (
        type(envelope) is not dict
        or not PROPOSAL_FIELDS.issuperset(envelope)
        or not set(PROPOSAL_REQUIRED).issubset(envelope)
    ):
        raise ValueError("a protected tool's call is not a proposal")
    return ProposedAction(
        connector=UNCLAIMED,
        connector_version=UNCLAIMED,
        effect_class=UNCLAIMED_EFFECT_CLASS,
        resource=UNCLAIMED if envelope.get("resource") is None else envelope["resource"],
        args=envelope["args"],
        expected_resource_version=envelope.get("expected_resource_version"),
        tool=tool,
    )


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
    """``refused``: the step was not run. Zero usage, source ``unknown``: the whole reservation is charged, never released (B08.4)."""
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


#: The limits a step can stop on, in the order the platform names one when several are passed.
LIMIT_STOPS: Final = ("max_turns", "max_tool_calls", "max_tokens", "deadline")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class StepClock:
    """The platform's clock for a step's deadline: wall time for the absolute deadline, a monotonic clock for how long it took.

    ``now`` must return an aware ``datetime`` and ``monotonic`` a non-decreasing float of seconds. Both are the platform's own
    (never read from an adapter or a provider); tests inject a fake one so a deadline is exercised without sleeping.
    """

    now: Callable[[], datetime] = _utc_now
    monotonic: Callable[[], float] = time.monotonic


SYSTEM_CLOCK: Final = StepClock()


def resolve_clock(clock: StepClock | None) -> StepClock:
    """``clock``, or the platform's own (:data:`SYSTEM_CLOCK`, looked up at call time) when there is none."""
    return SYSTEM_CLOCK if clock is None else clock


def read_instant(clock: StepClock) -> datetime:
    value = clock.now()
    if type(value) is not datetime or value.utcoffset() is None:
        raise ValueError("the step clock's now() must return an aware datetime")
    return value


def _seconds(clock: StepClock) -> float:
    value = clock.monotonic()
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("the step clock's monotonic() must return a finite number")
    return float(value)


def _rfc3339(instant: datetime) -> str:
    return instant.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class StepCeilings:
    """The most any one step may be given (``CALIBER_AGENT_STEP_MAX_*``): finite, whatever a producer's limits ask for.

    :func:`cap_limits` brings a step's limits down to these before the request is made. The defaults are the settings' own, so
    a governed context that names no ceilings is capped, never uncapped. Construction refuses a value outside the settings'
    bounds (a boolean is not a count), naming the field and nothing else.
    """

    max_turns: int = AGENT_STEP_MAX_TURNS_DEFAULT
    max_tool_calls: int = AGENT_STEP_MAX_TOOL_CALLS_DEFAULT
    max_tokens: int = AGENT_STEP_MAX_TOKENS_DEFAULT
    max_seconds: float = AGENT_STEP_MAX_SECONDS_DEFAULT

    def __post_init__(self) -> None:
        counts = (
            ("max_turns", self.max_turns, AGENT_STEP_MAX_TURNS_BOUNDS),
            ("max_tool_calls", self.max_tool_calls, AGENT_STEP_MAX_TOOL_CALLS_BOUNDS),
            ("max_tokens", self.max_tokens, AGENT_STEP_MAX_TOKENS_BOUNDS),
        )
        for name, value, (low, high) in counts:
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"step ceiling {name} must be an integer from {low} to {high}")
        low_s, high_s = AGENT_STEP_MAX_SECONDS_BOUNDS
        seconds = self.max_seconds
        if (
            type(seconds) not in (int, float)
            or not math.isfinite(seconds)
            or not low_s <= seconds <= high_s
        ):
            raise ValueError(
                f"step ceiling max_seconds must be a finite number from {low_s} to {high_s}"
            )

    @classmethod
    def from_config(cls, config: CaliberConfig) -> StepCeilings:
        """The ceilings the operator configured."""
        return cls(
            max_turns=config.agent_step_max_turns,
            max_tool_calls=config.agent_step_max_tool_calls,
            max_tokens=config.agent_step_max_tokens,
            max_seconds=config.agent_step_max_seconds,
        )


def cap_limits(limits: StepLimits, ceilings: StepCeilings, *, now: datetime) -> StepLimits:
    """``limits`` brought down to ``ceilings``: a count above its ceiling becomes the ceiling, and a deadline later than
    ``now + max_seconds`` is brought forward to it (an earlier one is kept exactly as given). Never raises a limit.

    ``now`` is the platform's own aware instant. ``limits`` must be a :class:`StepLimits`; one whose deadline is not an
    RFC 3339 instant raises ``ValueError``. A producer that wants more than a ceiling cannot have it.
    """
    if type(limits) is not StepLimits:
        raise TypeError("limits must be a StepLimits")
    latest = now + timedelta(seconds=ceilings.max_seconds)
    deadline = _rfc3339(latest) if limits.deadline_at > latest else limits.deadline
    return StepLimits(
        max_turns=min(limits.max_turns, ceilings.max_turns),
        max_tool_calls=min(limits.max_tool_calls, ceilings.max_tool_calls),
        max_tokens=min(limits.max_tokens, ceilings.max_tokens),
        deadline=deadline,
    )


class StepToolBudgetExceeded(Exception):  # noqa: N818 - what an adapter's tool call raises, named for the limit
    """A read tool was called past the step's ``max_tool_calls``: it was not run. The text is fixed; nothing else is carried."""

    def __init__(self) -> None:
        super().__init__("the step's tool-call limit is reached")


class StepDeadlineReached(Exception):  # noqa: N818 - what an adapter's tool call raises, named for the limit
    """A read tool was called at or after the step's deadline: it was not run. The text is fixed; nothing else is carried."""

    def __init__(self) -> None:
        super().__init__("the step's deadline is reached")


class _ToolMeter:
    """Counts the read-tool calls an adapter attempts, and refuses the one past the limit or at the deadline.

    The platform's own count and clock. A call is counted when it is attempted, before the tool runs, under a lock (an adapter
    may call tools from several threads), so two calls racing for the last slot cannot both run. A call made when ``expired()``
    (the deadline is now or past) is refused before it is counted and the refusal is remembered (:attr:`deadline_hit`), whether
    or not the adapter goes on to ignore it. A call already running is not interrupted. Nothing here is shared between steps.
    """

    def __init__(self, limit: int, expired: Callable[[], bool] = lambda: False) -> None:
        self._limit = limit
        self._expired = expired
        self._lock = threading.Lock()
        self._attempted = 0
        self.deadline_hit = False

    @property
    def executed(self) -> int:
        """Calls the platform let run."""
        return min(self._attempted, self._limit)

    @property
    def overrun(self) -> bool:
        """True when a call past the limit was attempted, whether or not the adapter went on to ignore the refusal."""
        return self._attempted > self._limit

    def wrap(self, tool: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(tool)
        def metered(*args: Any, **kwargs: Any) -> Any:
            if self._expired():
                self.deadline_hit = True
                raise StepDeadlineReached
            with self._lock:
                self._attempted += 1
                allowed = self._attempted <= self._limit
            if not allowed:
                raise StepToolBudgetExceeded
            return tool(*args, **kwargs)

        return metered

    def wrap_all(self, tools: Mapping[str, Callable[..., Any]]) -> dict[str, Callable[..., Any]]:
        return {name: self.wrap(tool) for name, tool in tools.items()}


def _metered_input(step_input: AgentStepInput, meter: _ToolMeter) -> AgentStepInput:
    """``step_input`` with every read tool (the agent's and its handoff targets') going through ``meter``."""
    handoffs: dict[str, StepHandoffTarget] | None = None
    if step_input.handoff_agents is not None:
        handoffs = {
            node_id: StepHandoffTarget(target.agent, meter.wrap_all(target.read_tools))
            for node_id, target in step_input.handoff_agents.items()
        }
    return AgentStepInput(
        agent=step_input.agent,
        input_text=step_input.input_text,
        history=step_input.history,
        read_tools=meter.wrap_all(step_input.read_tools),
        handoff_agents=handoffs,
    )


def before_work_stop(
    request: AgentStepRequest, *, clock: StepClock | None = None
) -> AgentStepResult | None:
    """The stop for a step that may not start, or ``None``: no turn or no token to spend, or a deadline that is now or past.

    The adapter has not been called, so the result carries zero usage with ``usage_source`` ``estimated``. The interpreter asks
    this before it reserves tokens, so a step stopped here has no reservation (B08.4). ``max_tool_calls`` of zero does not stop a step: it may still
    answer without a tool. A limit is checked against the platform's clock only.
    """
    limits = request.limits
    clock = resolve_clock(clock)
    if limits.max_turns <= 0:
        reason = "max_turns"
    elif limits.max_tokens <= 0:
        reason = "max_tokens"
    elif limits.deadline_at <= read_instant(clock):
        reason = "deadline"
    else:
        return None
    return answer(request, reason, usage_source="estimated")


def _exceeded(
    limits: StepLimits, usage: StepUsage, meter: _ToolMeter, finished_at: datetime
) -> str | None:
    """The first limit (in :data:`LIMIT_STOPS` order) the step passed: exactly the limit is within it, one over is not."""
    if usage.turns > limits.max_turns:
        return "max_turns"
    if meter.overrun or usage.tool_calls > limits.max_tool_calls:
        return "max_tool_calls"
    if usage.total_tokens > limits.max_tokens:
        return "max_tokens"
    if meter.deadline_hit or finished_at > limits.deadline_at:
        return "deadline"
    return None


def _enforce_limits(
    request: AgentStepRequest,
    result: AgentStepResult,
    meter: _ToolMeter,
    started_at: datetime,
    elapsed_seconds: float,
) -> AgentStepResult:
    """Hold a validated result to the request's limits: the platform's own measurements and the reported usage.

    ``tool_calls`` is raised to the platform's count when the adapter reported fewer. A ``completed`` or ``proposed_action``
    result that passed a limit becomes that limit's stop with the proposal dropped (a step that overran never gets to propose);
    any other result (a stop, a refusal, a failure) already ends the step and is returned as it is.
    """
    usage = result.usage
    if meter.executed > usage.tool_calls:
        usage = replace(usage, tool_calls=meter.executed)
        result = replace(result, usage=usage)
    if result.stop_reason not in ("completed", "proposed_action"):
        return result
    finished_at = started_at + timedelta(seconds=max(0.0, elapsed_seconds))
    reason = _exceeded(request.limits, usage, meter, finished_at)
    if reason is None:
        return result
    logger.warning(
        "agent step %s: stopped at %s (turns=%d tool_calls=%d tokens=%d)",
        request.step_request_id,
        reason,
        usage.turns,
        usage.tool_calls,
        usage.total_tokens,
    )
    return answer(request, reason, usage=usage, usage_source=result.usage_source)


def run_agent_step(
    adapter: object,
    request: AgentStepRequest,
    step_input: AgentStepInput,
    *,
    clock: StepClock | None = None,
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

    The request's limits are enforced here, for every adapter (see the module docstring): a step that may not start stops
    before the adapter is called (:func:`before_work_stop`), the adapter is handed read tools that the platform counts and
    refuses past ``max_tool_calls``, and a result that passed a limit is replaced by that limit's stop with no proposal.
    ``clock`` is the platform's (:class:`StepClock`; ``None`` is :data:`SYSTEM_CLOCK`).
    """
    checked = parse_step_request(request.record())
    clock = resolve_clock(clock)
    if step_input.agent.node_id != checked.node_id:
        raise ContractValidationError(
            REQUEST_KIND, ["node_id: the step input's agent is not the request's node"]
        )
    if not isinstance(adapter, AgentStepAdapter):
        return refusal(checked, ADAPTER_UNSUPPORTED)
    if (stopped := before_work_stop(checked, clock=clock)) is not None:
        return stopped
    started_at, started = read_instant(clock), _seconds(clock)
    deadline = checked.limits.deadline_at
    meter = _ToolMeter(
        checked.limits.max_tool_calls,
        lambda: started_at + timedelta(seconds=max(0.0, _seconds(clock) - started)) >= deadline,
    )
    # The adapter gets its own copy of the request: ``checked`` (whose limits every check below reads) is never handed out, so
    # an adapter that rewrites the limits of the request it holds (a frozen dataclass can be forced) changes nothing here.
    offered = parse_step_request(checked.record())
    try:
        returned = adapter.run_step(offered, _metered_input(step_input, meter))
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
    elapsed = _seconds(clock) - started
    try:
        result = parse_step_result(_snapshot(returned), checked)
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
    return _enforce_limits(checked, result, meter, started_at, elapsed)


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
    "LIMIT_STOPS",
    "MALFORMED_RESULT",
    "NOT_CALLED_CODES",
    "PROPOSAL_DESCRIPTION",
    "PROPOSAL_FIELDS",
    "PROPOSAL_OPTIONAL",
    "PROPOSAL_REQUIRED",
    "PROVIDER_ERROR",
    "SCRIPT_EXHAUSTED",
    "SYSTEM_CLOCK",
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
    "StepCeilings",
    "StepClock",
    "StepDeadlineReached",
    "StepHandoffAgents",
    "StepHandoffTarget",
    "StepLimits",
    "StepScript",
    "StepToolBudgetExceeded",
    "StepUsage",
    "answer",
    "before_work_stop",
    "cap_limits",
    "failure",
    "filter_read_tools",
    "proposal_from_call",
    "proposal_parameters",
    "read_instant",
    "refusal",
    "resolve_clock",
    "run_agent_step",
]
