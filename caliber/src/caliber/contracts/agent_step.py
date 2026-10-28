"""The agent-step contracts: what a step is asked and what it answers, strict and checked before any tool runs (B08.1a, #713).

An *agent step* is one bounded unit of model work inside a workflow node. Two control records describe it (ADR 0003,
kinds ``agent_step_request`` and ``agent_step_result``, schema ``v1``):

* :class:`AgentStepRequest` names the step (run, node, the durable **occurrence** and the attempt, and the
  ``step_request_id`` allocated for exactly that tuple), what it was given (the SHA-256 of the run's manifest and of the
  context manifest the provider sees), the one grant it runs under and its **limits** (turns, tool calls, tokens, deadline).
  The platform, not the adapter, enforces the limits.
* :class:`AgentStepResult` answers it with a **stop reason**, the **usage** the provider returned and the effect the step
  asks to perform as a :class:`ProposedAction`. A step *proposes*; it never invokes a protected tool and never says an
  effect happened: the schema has no ``state``, ``receipt`` or ``approval`` field and refuses an unknown one, so a result
  that claims a commit is invalid. A proposal becomes a ``proposal`` record through ``caliber.effects.records`` and moves
  through the proposal state machine there.

**What a model says is a claim, not a fact.** Everything in a result comes from model output, and two fields of a
proposed action decide how it is governed: ``effect_class`` (a model can call an irreversible write ``read``, and a
``read`` can be approved by policy alone) and ``connector_version`` (part of the intent). Neither is trusted here. The
platform's own class comes from the protected-effect inventory (``caliber.effects.inventory``) and
:meth:`ProposedAction.bound_to` returns the action carrying *that* class and refuses, with :class:`ClaimMismatch`, one
whose claim differs. B08.2 must call it, and must not call ``record_proposal`` with an action whose
:attr:`~ProposedAction.platform_bound` is false.

**At most one proposed action per step in v1.** A logical effect is keyed by ``(run, node, occurrence)``
(``effects.records.logical_key``), so two actions in one step would share a key and the second would be refused as a
conflict or, worse, swallowed as the first's retry. The schema keeps an array so a later version can carry several, but only
after it adds a per-action identity; ADR 0003 says so.

:class:`ProposedAction` is the single shape shared with the effect proposal (B05): its fields, names and limits are those of
the :class:`caliber.effects.records.Intent` behind a ``proposal`` v1 record (``target`` with ``connector`` and ``resource``,
``args``, ``effect_class``, ``connector_version``, ``expected_resource_version``). :attr:`ProposedAction.intent` is that
very ``Intent`` and :attr:`ProposedAction.intent_hash` is its digest, so what a step proposed, what an approver is shown and
what a receipt answers are one digest; nothing here computes a reduced one. An action is immutable: ``args`` is a read-only
deep view, ``intent`` is rebuilt on each read (so changing what one hands out cannot change what was approved or stored),
and equality and hashing follow the canonical intent bytes (``{"a": True}`` is not ``{"a": 1}``).

:func:`parse_step_request` and :func:`parse_step_result` are the gate: they validate against the schema (an unknown
version, an extra field, an invalid effect class or a missing target are refused with the registry's typed errors, whose
messages for these two kinds never carry a value from the record) and build every proposed action's intent (so a size,
depth or character violation is refused too). They run before anything acts on the record and raise
:class:`caliber.contracts.ContractValidationError` or :class:`caliber.contracts.UnsupportedSchemaVersion`.

**Usage and the token reservation.** ``usage.total_tokens`` is the authoritative figure a budget settles against; it may not
be less than ``prompt_tokens + completion_tokens`` and ``cached_prompt_tokens`` may not exceed ``prompt_tokens``. When the
provider gave no usage (a failed or ambiguous call) the adapter reports what it has with ``usage_source`` ``estimated`` or
``unknown`` and does not fabricate figures that look genuine. A refused or malformed result is **not** a release of the
step's token reservation: B08.4 keeps a conservative charge for it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Final

from caliber.contracts.registry import ContractValidationError, validate_record
from caliber.effects.records import Intent, ProposalError, intent_digest

REQUEST_KIND: Final = "agent_step_request"
RESULT_KIND: Final = "agent_step_result"
SCHEMA_VERSION: Final = "v1"
#: Why a step stopped. ``proposed_action`` is the only reason with a proposal, and every result with one carries it.
STOP_REASONS: Final = (
    "completed",
    "proposed_action",
    "max_turns",
    "max_tool_calls",
    "max_tokens",
    "deadline",
    "refused",
    "error",
)
#: Where usage figures come from; ``provider`` when a result does not say.
USAGE_SOURCES: Final = ("provider", "estimated", "unknown")
#: Most proposals one step may return in v1 (see the module docstring); the schema says the same.
MAX_PROPOSED_ACTIONS: Final = 1
CLAIM_MISMATCH: Final = "claim_mismatch"
#: The fields of a step's tuple: with ``step_request_id`` they say which step a request or a result is about.
_STEP_TUPLE: Final = ("workflow_run_id", "node_id", "occurrence_id", "attempt")
_DEADLINE: Final = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,9}))?"
    r"(?:Z|([+-])([0-9]{2}):([0-9]{2}))\Z"
)


class ClaimMismatch(ProposalError):  # noqa: N818 - a refusal, named for what it says
    """A model's claim about an action differs from what the platform knows; the action is not used."""

    def __init__(self, name: str, claimed: str | None, bound: str) -> None:
        shown = f"{claimed!r}" if name == "effect_class" else "another value"
        super().__init__(
            CLAIM_MISMATCH, f"the step claimed {name} {shown}; the platform's is {bound!r}"
        )
        self.name = name


def _freeze(value: Any) -> Any:
    """A read-only deep view of plain JSON: objects become mapping proxies, arrays tuples."""
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    """A fresh mutable copy of a :func:`_freeze` view."""
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True, eq=False)
class ProposedAction:
    """One effect a step asks to perform: the :class:`~caliber.effects.records.Intent` fields, nothing more.

    Construction builds the canonical :class:`~caliber.effects.records.Intent` and raises
    :class:`~caliber.effects.records.ProposalError` when there is none (an identifier with a control character, ``args``
    that are not plain JSON, too deep or too large), so an action that exists has an intent. It is then immutable and
    shares nothing mutable with the caller or with what it hands out: ``args`` is a read-only deep view of the canonical
    arguments, :attr:`intent` and :meth:`record` are fresh on every call. Two actions are equal, and hash alike, when their
    canonical intents are the same bytes.

    ``effect_class`` and ``connector_version`` are the model's claims until :meth:`bound_to` has run.
    """

    connector: str
    connector_version: str
    effect_class: str
    resource: str
    args: Mapping[str, Any] = field(repr=False)
    expected_resource_version: str | None = None
    platform_bound: bool = field(default=False, init=False)
    """True only for an action :meth:`bound_to` returned: its class is the platform's, not the model's."""
    _canonical: bytes = field(init=False, repr=False)

    def __post_init__(self) -> None:
        intent = Intent(
            connector=self.connector,
            connector_version=self.connector_version,
            effect_class=self.effect_class,
            resource=self.resource,
            args=self.args,
            expected_resource_version=self.expected_resource_version,
        )
        object.__setattr__(self, "args", _freeze(intent.plain_args))
        object.__setattr__(self, "_canonical", intent.canonical)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ProposedAction) and self._canonical == other._canonical

    def __hash__(self) -> int:
        return hash(self._canonical)

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> ProposedAction:
        """Read a schema-valid ``proposed_actions`` element (see :func:`parse_step_result` for the checked path)."""
        target = record["target"]
        return cls(
            connector=target["connector"],
            connector_version=record["connector_version"],
            effect_class=record["effect_class"],
            resource=target["resource"],
            args=record["args"],
            expected_resource_version=record["expected_resource_version"],
        )

    @property
    def intent(self) -> Intent:
        """The canonical intent of this action, shared with the effect proposal; a new object on every read."""
        return Intent(
            connector=self.connector,
            connector_version=self.connector_version,
            effect_class=self.effect_class,
            resource=self.resource,
            args=_thaw(self.args),
            expected_resource_version=self.expected_resource_version,
        )

    @property
    def intent_hash(self) -> str:
        """The ``sha256:`` digest every approval and receipt of this effect reuses."""
        return intent_digest(self._canonical)

    def bound_to(
        self, inventory_effect_class: str, *, connector_version: str | None = None
    ) -> ProposedAction:
        """This action carrying the platform's effect class (and, when given, connector version).

        ``inventory_effect_class`` is what the protected-effect inventory says about the tool or connector the action
        targets, never what the model said. A claim that differs raises :class:`ClaimMismatch` and nothing is
        recorded; one that matches returns an action with :attr:`platform_bound` set.
        """
        if self.effect_class != inventory_effect_class:
            raise ClaimMismatch("effect_class", self.effect_class, inventory_effect_class)
        if connector_version is not None and self.connector_version != connector_version:
            raise ClaimMismatch("connector_version", self.connector_version, connector_version)
        bound = ProposedAction(
            connector=self.connector,
            connector_version=self.connector_version,
            effect_class=inventory_effect_class,
            resource=self.resource,
            args=_thaw(self.args),
            expected_resource_version=self.expected_resource_version,
        )
        object.__setattr__(bound, "platform_bound", True)
        return bound

    def record(self) -> dict[str, Any]:
        """The ``proposed_actions`` element: fresh plain JSON, ``args`` as the canonical intent holds them."""
        return {
            "effect_class": self.effect_class,
            "target": {"connector": self.connector, "resource": self.resource},
            "connector_version": self.connector_version,
            "args": _thaw(self.args),
            "expected_resource_version": self.expected_resource_version,
        }


def parse_deadline(value: str) -> datetime:
    """The aware instant of a schema-valid deadline; no clock is read.

    Fractional seconds beyond microseconds are truncated (the instant is at most 1 microsecond early). The schema allows
    ASCII digits only and no leap second, so the result is always a real ``datetime``.
    """
    match = _DEADLINE.fullmatch(value)
    if match is None:
        raise ValueError("not an RFC 3339 deadline")
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    micro = int((match.group(7) or "").ljust(6, "0")[:6])
    sign, off_h, off_m = match.group(8), match.group(9), match.group(10)
    zone = timezone.utc
    if sign is not None:
        delta = timedelta(hours=int(off_h), minutes=int(off_m))
        zone = timezone(-delta if sign == "-" else delta)
    return datetime(year, month, day, hour, minute, second, micro, tzinfo=zone)


@dataclass(frozen=True)
class StepLimits:
    """What the platform lets one step use. A limit of zero is a step that may not start that kind of work."""

    max_turns: int
    max_tool_calls: int
    max_tokens: int
    deadline: str  #: RFC 3339 ``date-time`` with an offset

    @property
    def deadline_at(self) -> datetime:
        """The deadline as an aware ``datetime`` (see :func:`parse_deadline`)."""
        return parse_deadline(self.deadline)

    def record(self) -> dict[str, Any]:
        return {
            "max_turns": self.max_turns,
            "max_tool_calls": self.max_tool_calls,
            "max_tokens": self.max_tokens,
            "deadline": self.deadline,
        }


@dataclass(frozen=True)
class StepUsage:
    """What the provider reported for the step. ``total_tokens`` is the figure a budget settles against.

    Raises :class:`~caliber.contracts.ContractValidationError` when the figures contradict each other: a total below
    ``prompt_tokens + completion_tokens`` understates the charge, and cached tokens are part of the prompt.
    """

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    turns: int
    tool_calls: int
    cached_prompt_tokens: int | None = None

    def __post_init__(self) -> None:
        errors = []
        if self.total_tokens < self.prompt_tokens + self.completion_tokens:
            errors.append("usage/total_tokens: is less than prompt_tokens + completion_tokens")
        if self.cached_prompt_tokens is not None and self.cached_prompt_tokens > self.prompt_tokens:
            errors.append("usage/cached_prompt_tokens: exceeds prompt_tokens")
        if errors:
            raise ContractValidationError(RESULT_KIND, errors)

    def record(self) -> dict[str, Any]:
        usage: dict[str, Any] = {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "turns": self.turns,
            "tool_calls": self.tool_calls,
        }
        if self.cached_prompt_tokens is not None:
            usage["cached_prompt_tokens"] = self.cached_prompt_tokens
        return usage


@dataclass(frozen=True)
class AgentStepRequest:
    """One bounded step, asked of an adapter. See the module docstring."""

    step_request_id: str
    workflow_run_id: str
    node_id: str
    occurrence_id: str
    attempt: int
    run_manifest_digest: str
    context_manifest_digest: str
    grant_id: str
    limits: StepLimits

    def record(self) -> dict[str, Any]:
        """The ``agent_step_request`` v1 record, validated before it is returned."""
        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "step_request_id": self.step_request_id,
            "workflow_run_id": self.workflow_run_id,
            "node_id": self.node_id,
            "occurrence_id": self.occurrence_id,
            "attempt": self.attempt,
            "run_manifest_digest": self.run_manifest_digest,
            "context_manifest_digest": self.context_manifest_digest,
            "grant_id": self.grant_id,
            "limits": self.limits.record(),
        }
        validate_record(REQUEST_KIND, record)
        return record


@dataclass(frozen=True)
class AgentStepResult:
    """What one step proposed and used. See the module docstring."""

    step_request_id: str
    workflow_run_id: str
    node_id: str
    occurrence_id: str
    attempt: int
    stop_reason: str
    usage: StepUsage
    proposed_actions: tuple[ProposedAction, ...] = ()
    usage_source: str | None = None
    error_code: str | None = None

    def record(self) -> dict[str, Any]:
        """The ``agent_step_result`` v1 record, validated before it is returned."""
        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "step_request_id": self.step_request_id,
            "workflow_run_id": self.workflow_run_id,
            "node_id": self.node_id,
            "occurrence_id": self.occurrence_id,
            "attempt": self.attempt,
            "stop_reason": self.stop_reason,
            "proposed_actions": [action.record() for action in self.proposed_actions],
            "usage": self.usage.record(),
        }
        if self.usage_source is not None:
            record["usage_source"] = self.usage_source
        if self.error_code is not None:
            record["error_code"] = self.error_code
        validate_record(RESULT_KIND, record)
        return record


def parse_step_request(payload: Mapping[str, Any]) -> AgentStepRequest:
    """Validate ``payload`` as an ``agent_step_request`` v1 record and read it; nothing has run when this raises."""
    validate_record(REQUEST_KIND, payload)
    limits = payload["limits"]
    return AgentStepRequest(
        step_request_id=payload["step_request_id"],
        workflow_run_id=payload["workflow_run_id"],
        node_id=payload["node_id"],
        occurrence_id=payload["occurrence_id"],
        attempt=payload["attempt"],
        run_manifest_digest=payload["run_manifest_digest"],
        context_manifest_digest=payload["context_manifest_digest"],
        grant_id=payload["grant_id"],
        limits=StepLimits(
            max_turns=limits["max_turns"],
            max_tool_calls=limits["max_tool_calls"],
            max_tokens=limits["max_tokens"],
            deadline=limits["deadline"],
        ),
    )


def parse_step_result(payload: Mapping[str, Any], request: AgentStepRequest) -> AgentStepResult:
    """Validate ``payload`` as an ``agent_step_result`` v1 record that answers ``request`` and read it.

    Nothing has run when this raises. The request is required: a result that is not bound to the step that was asked
    is never read. Every proposed action's intent is built, so one that cannot be canonicalised (too large, too deep, not
    plain JSON, a control character in an identifier) is refused here and never reaches the proposal store; the usage
    must be consistent (:class:`StepUsage`); and the result must answer ``request`` (:func:`check_result_answers_request`).
    The proposed action's ``effect_class`` and ``connector_version`` are still the model's claims: see
    :meth:`ProposedAction.bound_to`.
    """
    validate_record(RESULT_KIND, payload)
    actions: list[ProposedAction] = []
    for index, item in enumerate(payload["proposed_actions"]):
        try:
            actions.append(ProposedAction.from_record(item))
        except ProposalError as exc:
            raise ContractValidationError(
                RESULT_KIND, [f"proposed_actions/{index}: {exc}"]
            ) from exc
    usage = payload["usage"]
    result = AgentStepResult(
        step_request_id=payload["step_request_id"],
        workflow_run_id=payload["workflow_run_id"],
        node_id=payload["node_id"],
        occurrence_id=payload["occurrence_id"],
        attempt=payload["attempt"],
        stop_reason=payload["stop_reason"],
        usage=StepUsage(
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            total_tokens=usage["total_tokens"],
            turns=usage["turns"],
            tool_calls=usage["tool_calls"],
            cached_prompt_tokens=usage.get("cached_prompt_tokens"),
        ),
        proposed_actions=tuple(actions),
        usage_source=payload.get("usage_source"),
        error_code=payload.get("error_code"),
    )
    check_result_answers_request(request, result)
    return result


def check_result_answers_request(request: AgentStepRequest, result: AgentStepResult) -> None:
    """Refuse a result about another step than ``request``: a stale or crossed answer is never applied.

    A step is one ``step_request_id`` and one ``(run, node, occurrence, attempt)`` tuple, bound to each other when the
    request is made. A result must carry both exactly; one whose tuple matches but whose id differs, or the reverse, is
    refused with a message that says which side matched. Values are not quoted.
    """
    differing = [name for name in _STEP_TUPLE if getattr(result, name) != getattr(request, name)]
    same_id = result.step_request_id == request.step_request_id
    if same_id and not differing:
        return
    if not same_id and not differing:
        errors = [
            "step_request_id: names another step than the request's, "
            "although run, node, occurrence and attempt are the same"
        ]
    elif same_id:
        errors = [
            f"{name}: differs from the request's, although step_request_id is the same"
            for name in differing
        ]
    else:
        errors = [
            f"{name}: does not match the request's" for name in ("step_request_id", *differing)
        ]
    raise ContractValidationError(RESULT_KIND, errors)
