"""The agent-step contracts: what a step is asked and what it answers, strict and checked before any tool runs (B08.1a, #713).

An *agent step* is one bounded unit of model work inside a workflow node. Two control records describe it (ADR 0003,
kinds ``agent_step_request`` and ``agent_step_result``, schema ``v1``):

* :class:`AgentStepRequest` names the step (run, node, the durable **occurrence** and the attempt), what it was given (the
  SHA-256 of the run's manifest and of the context manifest the provider sees), the one grant it runs under and its
  **limits** (turns, tool calls, tokens, deadline). The platform, not the adapter, enforces the limits.
* :class:`AgentStepResult` answers it with a **stop reason**, the **usage** the provider returned and the effects the step
  asks to perform as :class:`ProposedAction` values. A step *proposes*; it never invokes a protected tool and never says an
  effect happened: the schema has no ``state``, ``receipt`` or ``approval`` field and refuses an unknown one, so a result
  that claims a commit is invalid. The proposals become ``proposal`` records through ``caliber.effects.records`` and move
  through the proposal state machine there.

:class:`ProposedAction` is the single shape shared with the effect proposal (B05): its fields, names and limits are those of
the :class:`caliber.effects.records.Intent` behind a ``proposal`` v1 record (``target`` with ``connector`` and ``resource``,
``args``, ``effect_class``, ``connector_version``, ``expected_resource_version``). :attr:`ProposedAction.intent` is that
very ``Intent`` and :attr:`ProposedAction.intent_hash` is its digest, so what a step proposed, what an approver is shown and
what a receipt answers are one digest; nothing here computes a reduced one.

:func:`parse_step_request` and :func:`parse_step_result` are the gate: they validate against the schema (an unknown
version, an extra field, an invalid effect class or a missing target are refused with the registry's typed errors) and
build every proposed action's intent (so a size, depth or character violation is refused too). They run before anything
acts on the record and raise :class:`caliber.contracts.ContractValidationError` or
:class:`caliber.contracts.UnsupportedSchemaVersion`; an intent error never carries an argument's value.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from caliber.contracts.registry import ContractValidationError, validate_record
from caliber.effects.records import Intent, ProposalError

REQUEST_KIND: Final = "agent_step_request"
RESULT_KIND: Final = "agent_step_result"
SCHEMA_VERSION: Final = "v1"
#: Why a step stopped. ``proposed_action`` is the only reason with proposals, and every result with one carries it.
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
#: Most proposals one step may return; the schema says the same.
MAX_PROPOSED_ACTIONS: Final = 16
#: The fields that say which step a request or a result is about; a result must repeat its request's.
_STEP_IDENTITY: Final = (
    "step_request_id",
    "workflow_run_id",
    "node_id",
    "occurrence_id",
    "attempt",
)


@dataclass(frozen=True)
class ProposedAction:
    """One effect a step asks to perform: the :class:`~caliber.effects.records.Intent` fields, nothing more.

    Construction builds the canonical :class:`~caliber.effects.records.Intent` and raises
    :class:`~caliber.effects.records.ProposalError` when there is none (an identifier with a control character, ``args``
    that are not plain JSON, too deep or too large), so an action that exists has an intent. ``args`` is then the
    canonical copy: a later change to the mapping the caller passed in changes neither the action nor its digest.
    """

    connector: str
    connector_version: str
    effect_class: str
    resource: str
    args: Mapping[str, Any]
    expected_resource_version: str | None = None
    intent: Intent = field(init=False, repr=False, compare=False)
    """The canonical intent of this action, shared with the effect proposal."""

    def __post_init__(self) -> None:
        intent = Intent(
            connector=self.connector,
            connector_version=self.connector_version,
            effect_class=self.effect_class,
            resource=self.resource,
            args=self.args,
            expected_resource_version=self.expected_resource_version,
        )
        object.__setattr__(self, "args", intent.plain_args)
        object.__setattr__(self, "intent", intent)

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
    def intent_hash(self) -> str:
        """The ``sha256:`` digest every approval and receipt of this effect reuses."""
        return self.intent.digest

    def record(self) -> dict[str, Any]:
        """The ``proposed_actions`` element: plain JSON, ``args`` as the canonical intent holds them."""
        return {
            "effect_class": self.effect_class,
            "target": {"connector": self.connector, "resource": self.resource},
            "connector_version": self.connector_version,
            "args": copy.deepcopy(self.intent.plain_args),
            "expected_resource_version": self.expected_resource_version,
        }


@dataclass(frozen=True)
class StepLimits:
    """What the platform lets one step use. A limit of zero is a step that may not start that kind of work."""

    max_turns: int
    max_tool_calls: int
    max_tokens: int
    deadline: str  #: RFC 3339 ``date-time`` with an offset

    def record(self) -> dict[str, Any]:
        return {
            "max_turns": self.max_turns,
            "max_tool_calls": self.max_tool_calls,
            "max_tokens": self.max_tokens,
            "deadline": self.deadline,
        }


@dataclass(frozen=True)
class StepUsage:
    """What the provider reported for the step. ``cached_prompt_tokens`` is absent when it did not say."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    turns: int
    tool_calls: int
    cached_prompt_tokens: int | None = None

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
            **{name: getattr(self, name) for name in _STEP_IDENTITY},
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

    def record(self) -> dict[str, Any]:
        """The ``agent_step_result`` v1 record, validated before it is returned."""
        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            **{name: getattr(self, name) for name in _STEP_IDENTITY},
            "stop_reason": self.stop_reason,
            "proposed_actions": [action.record() for action in self.proposed_actions],
            "usage": self.usage.record(),
        }
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
        attempt=int(payload["attempt"]),
        run_manifest_digest=payload["run_manifest_digest"],
        context_manifest_digest=payload["context_manifest_digest"],
        grant_id=payload["grant_id"],
        limits=StepLimits(
            max_turns=int(limits["max_turns"]),
            max_tool_calls=int(limits["max_tool_calls"]),
            max_tokens=int(limits["max_tokens"]),
            deadline=limits["deadline"],
        ),
    )


def parse_step_result(
    payload: Mapping[str, Any], *, request: AgentStepRequest | None = None
) -> AgentStepResult:
    """Validate ``payload`` as an ``agent_step_result`` v1 record and read it; nothing has run when this raises.

    Every proposed action's intent is built, so one that cannot be canonicalised (too large, too deep, not plain JSON,
    a control character in an identifier) is refused here and never reaches the proposal store. With ``request`` the
    result must also answer that step: the same step, run, node, occurrence and attempt.
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
    cached = usage.get("cached_prompt_tokens")
    result = AgentStepResult(
        step_request_id=payload["step_request_id"],
        workflow_run_id=payload["workflow_run_id"],
        node_id=payload["node_id"],
        occurrence_id=payload["occurrence_id"],
        attempt=int(payload["attempt"]),
        stop_reason=payload["stop_reason"],
        usage=StepUsage(
            prompt_tokens=int(usage["prompt_tokens"]),
            completion_tokens=int(usage["completion_tokens"]),
            total_tokens=int(usage["total_tokens"]),
            turns=int(usage["turns"]),
            tool_calls=int(usage["tool_calls"]),
            cached_prompt_tokens=None if cached is None else int(cached),
        ),
        proposed_actions=tuple(actions),
    )
    if request is not None:
        check_result_answers_request(request, result)
    return result


def check_result_answers_request(request: AgentStepRequest, result: AgentStepResult) -> None:
    """Refuse a result that is about another step than ``request``: a stale or crossed answer is never applied."""
    errors = [
        f"{name}: {getattr(result, name)!r} is not the request's {getattr(request, name)!r}"
        for name in _STEP_IDENTITY
        if getattr(result, name) != getattr(request, name)
    ]
    if errors:
        raise ContractValidationError(RESULT_KIND, errors)
