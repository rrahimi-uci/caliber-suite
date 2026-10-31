"""The platform side of a governed agent step's protected action: from a model's proposal to a recorded, approvable one (B08.2, #744).

An adapter (B08.1b, :mod:`caliber.workflows.agent_step`) only *proposes*: a step that stops with ``proposed_action`` carries one
:class:`~caliber.contracts.agent_step.ProposedAction` whose ``effect_class`` and ``connector_version`` are **the model's claims**.
This module is what the platform does with it, in the interpreter and never in an adapter:

1. **Classify the agent's tools, not the model's claim.** :func:`registry_tool_classifier` answers, for each tool bound to the
   agent, the protected-effect inventory's class for the tool's *registry identity* (a name alone is not an identity: only a row
   CALIBER seeded is looked up by name, every other tool, and every MCP tool, is the inventory's fail-closed class). Tools classified
   ``read`` are the only ones an adapter is handed (:func:`caliber.workflows.agent_step.filter_read_tools`); everything else is
   proposed, never run.
2. **Tie the proposal to the agent by a trusted map, then bind.** A proposal carries no tool name (the v1 contract has none) and the
   connector it names is the model's word. :attr:`GovernedStepContext.tool_backends` (the producer's, from trusted configuration) says
   which backend kind each tool sends through; the proposal's connector must be the connector of a mapped, non-read tool's backend
   (``connector_not_mapped`` otherwise), the backend kind is admitted (:func:`caliber.effects.dispatcher.admit_backend`) and the
   destination must fit the admitted connector's resource pattern. The class is the strictest among the tools that map to that
   backend and the version is the admitted one. :meth:`ProposedAction.bound_to` refuses a claim that differs
   (``claim_mismatch``): it is never repaired and never trusted.
3. **Record.** :func:`caliber.effects.dispatcher.submit_proposal` stores the bound action as a ``proposed`` effect for the node's
   durable occurrence (one logical effect per ``(run, node, occurrence)``; a retry finds the same row) and, in the same
   transaction, the approval request bound to that exact intent (:func:`caliber.workflows.approval_requests.create_approval_request`).
   Nothing is dispatched here: dispatch needs an approval decision, an owner lease and a backend (B04.1, B04.6).

Every refusal is :class:`~caliber.effects.dispatcher.EffectRefused` with a fixed code and **no** model-supplied text. The runtime
parks the run (:class:`StepParked`) behind the request; see :mod:`caliber.workflows.runtime`.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Final

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from caliber.contracts.agent_step import StepLimits
from caliber.db.models import (
    CaliberEffectProposal,
    CaliberRuntimeApprovalRequest,
    CaliberToolRegistry,
    CaliberWorkflowRun,
)
from caliber.effects.dispatcher import (
    BACKENDS,
    EffectRefused,
    UnsupportedEffectPath,
    admit_backend,
    submit_proposal,
)
from caliber.effects.inventory import (
    EFFECT_CLASSES,
    FAIL_CLOSED,
    InventoryError,
    effect_class,
    tool_disposition,
)
from caliber.effects.records import PROPOSED, ProposalError, logical_key
from caliber.run_manifest import canonical_bytes
from caliber.workflows.agent_step import (
    AgentStepRequest,
    AgentStepResult,
    StepCeilings,
    StepClock,
    StepUsage,
)
from caliber.workflows.approval_policy import ApprovalPolicy, approval_refusal
from caliber.workflows.approval_requests import ApprovalRequestError, create_approval_request
from caliber.workflows.builtin_tools import is_builtin_tool
from caliber.workflows.ir import IRAgent, IRToolBinding

#: Why a step's proposal was refused (``EffectRefused.code``); fixed tokens, never a value from the model.
NO_PROPOSED_ACTION: Final = "no_proposed_action"
NO_PROTECTED_TOOL: Final = "no_protected_tool"
CONNECTOR_NOT_MAPPED: Final = "connector_not_mapped"
CONNECTOR_AMBIGUOUS: Final = "connector_ambiguous"
BACKEND_NOT_FOR_AGENT_STEPS: Final = "backend_not_for_agent_steps"
#: The backend kinds a *model's* proposal may reach. Each must have a destination the platform checks at proposal time: the
#: admitted ``document_request`` connector declares a resource pattern (``BACKENDS[kind].configuration["resource"]``, a regular
#: expression the destination must fully match). ``http`` is not here (its destination is an arbitrary URL and the egress check
#: runs at dispatch, after an approval would be spent) and neither is a backend whose configuration holds a literal resource
#: template rather than a pattern. A producer that maps a tool to one of those gets ``backend_not_for_agent_steps``.
AGENT_STEP_BACKENDS: Final = frozenset({"document_request"})
RESOURCE_NOT_ADMITTED: Final = "resource_not_admitted"
STEP_MISMATCH: Final = "step_mismatch"
INTENT_MISMATCH: Final = "intent_mismatch"
APPROVAL_EVIDENCE_UNAVAILABLE: Final = "approval_evidence_unavailable"
APPROVAL_REJECTED: Final = "approval_rejected"
CONTEXT_MANIFEST_MISSING: Final = "context_manifest_missing"
#: The only code a refusal of an unsupported connector can have (the dispatcher's own, see :class:`UnsupportedEffectPath`).
UNSUPPORTED_EFFECT_PATH: Final = "unsupported_effect_path"
CLAIM_MISMATCH: Final = "claim_mismatch"
PROPOSED_ACTION: Final = "proposed_action"
_LIVE_STATUSES: Final = ("pending", "approved")


@dataclass(frozen=True)
class ToolTrust:
    """What the platform knows about one tool bound to an agent: never what the model says about it."""

    #: The name the inventory is asked about: the registry row's name for a CALIBER built-in, else the binding's local name.
    name: str
    #: The inventory's class for this registry identity (:data:`~caliber.effects.inventory.FAIL_CLOSED` when it is not proven).
    effect_class: str
    #: True only when the registry row proved a tool CALIBER ships (:func:`~caliber.workflows.builtin_tools.is_builtin_tool`).
    caliber_defined: bool
    #: ``dispatch`` or ``refuse`` (:func:`~caliber.effects.inventory.tool_disposition`).
    disposition: str
    #: ``(name, version)`` of the registry row the binding resolved to and whose module and callable match the binding, or
    #: ``None`` (an MCP tool, an unresolved or mismatched binding). **This, not the local name a manifest author chose, is what
    #: ``GovernedStepContext.tool_backends`` is keyed by**: a local name is an alias anyone can give any tool.
    registry: tuple[str, str] | None = None


#: ``binding -> trust``. It must fail closed: a tool it cannot place is :func:`untrusted_tool`, never ``read``.
ToolClassifier = Callable[[IRToolBinding], ToolTrust]


def untrusted_tool(name: str, registry: tuple[str, str] | None = None) -> ToolTrust:
    """A tool the platform cannot vouch for: the inventory's fail-closed class, routed through the dispatcher."""
    return ToolTrust(
        name=name,
        effect_class=FAIL_CLOSED,
        caliber_defined=False,
        disposition="dispatch",
        registry=registry,
    )


def registry_tool_classifier(session_factory: Callable[[], Session]) -> ToolClassifier:
    """The production classifier: a tool is trusted only when the registry row it resolved to is one CALIBER seeded.

    The binding names its registry row (``resolved_name``, ``resolved_version``) but carries no owner, so the row is read and
    the identity checked field by field: the row's module and callable must be the binding's, and
    :func:`~caliber.workflows.builtin_tools.is_builtin_tool` must accept the row (owner, version, module and callable as the seed
    wrote them). An MCP tool, an unresolved tool, a missing or different row and an unreadable registry are all
    :func:`untrusted_tool`: the answer can only be stricter, never looser, than the truth.
    """

    def classify(binding: IRToolBinding) -> ToolTrust:
        name = binding.local_name
        if (
            binding.binding_type != "registered_function"
            or not binding.resolved_name
            or not binding.resolved_version
        ):
            return untrusted_tool(name)
        try:
            with session_factory() as session:
                row = session.execute(
                    select(
                        CaliberToolRegistry.name,
                        CaliberToolRegistry.owner,
                        CaliberToolRegistry.module_path,
                        CaliberToolRegistry.callable_name,
                        CaliberToolRegistry.version,
                    ).where(
                        CaliberToolRegistry.name == binding.resolved_name,
                        CaliberToolRegistry.version == binding.resolved_version,
                    )
                ).first()
        except SQLAlchemyError:
            return untrusted_tool(name)
        if (
            row is None
            or row.module_path != binding.module_path
            or row.callable_name != binding.callable_name
        ):
            return untrusted_tool(name)
        identity = (row.name, row.version)
        trusted = is_builtin_tool(
            name=row.name,
            owner=row.owner,
            module_path=row.module_path,
            callable_name=row.callable_name,
            version=row.version,
        )
        try:
            return ToolTrust(
                name=row.name if trusted else name,
                effect_class=effect_class(row.name, caliber_defined=trusted),
                caliber_defined=trusted,
                disposition=tool_disposition(row.name, caliber_defined=trusted),
                registry=identity,
            )
        except (InventoryError, OSError):
            return untrusted_tool(name, identity)

    return classify


@dataclass(frozen=True)
class GovernedStepContext:
    """What the interpreter needs to run an agent node as a governed step (``RuntimePlan.governed_agent_steps``).

    The runtime never builds it: the producer that admitted the run does (it holds the run's manifest, grant and session factory).
    A plan without one runs agent nodes exactly as before. With one, an agent node runs through
    :func:`caliber.workflows.agent_step.run_agent_step`: the adapter is given only the tools ``classify_tool`` calls ``read``, and
    a protected action it proposes is recorded here (:func:`record_step_proposal`) instead of executed.
    """

    session_factory: Callable[[], Session]
    workflow_run_id: str
    #: Lowercase SHA-256 hex of the run's stored ``run_manifest`` (what the request carries as ``run_manifest_digest``).
    run_manifest_digest: str
    #: The run's own grant (``run_manifest.grant_id``): the one grant the step runs under.
    grant_id: str
    classify_tool: ToolClassifier
    #: ``step_request_id -> sha256 hex`` of the context manifest recorded for that step before the provider sees anything.
    context_manifest_digest: Callable[[str], str]
    #: The limits the step runs under, from the producer's trusted configuration. The platform brings them down to
    #: :attr:`ceilings` (:func:`caliber.workflows.agent_step.cap_limits`) and enforces them in ``run_agent_step`` (B08.3): an
    #: adapter never sees a limit it could change, and what it reports is checked against them.
    limits: Callable[[IRAgent], StepLimits]
    #: Who the approval request names as its requester (a printable id, at most 256 characters).
    requested_by: str
    approval_ttl_seconds: int
    approval_policy: ApprovalPolicy = field(default_factory=ApprovalPolicy)
    #: Which backend kind (``caliber.effects.dispatcher.BACKENDS``, and one of :data:`AGENT_STEP_BACKENDS`) each registered tool
    #: sends through, keyed by the tool's registry identity ``(name, version)`` (``ToolTrust.registry``), never by a local name.
    #: This is what ties a proposal to a connector, and it is the producer's to build from trusted configuration: nothing a
    #: model, a manifest author or a tool call supplies may write it. A tool absent from it can be classified but never proposed
    #: through; an empty map (the default) records no proposal at all.
    tool_backends: Mapping[tuple[str, str], str] = field(default_factory=dict)
    #: The most any step may be given (``CALIBER_AGENT_STEP_MAX_*``; build with ``StepCeilings.from_config``). The default is the
    #: settings' own defaults, so a context that names none is capped, not uncapped.
    ceilings: StepCeilings = field(default_factory=StepCeilings)
    #: The clock a step's deadline is read from (wall time and a monotonic clock); ``None`` is the platform's own, a fake one in tests.
    clock: StepClock | None = None


def stored_context_manifest_digest(
    session_factory: Callable[[], Session], workflow_run_id: str
) -> Callable[[str], str]:
    """``context_manifest_digest`` for a context recorded in ``caliber_context_manifests`` (B07.3a): the latest revision's digest."""
    from caliber.context.manifest import load_context_manifest  # noqa: PLC0415

    def digest(step_request_id: str) -> str:
        with session_factory() as session:
            built = load_context_manifest(session, workflow_run_id, step_request_id)
        if built is None:
            raise EffectRefused(CONTEXT_MANIFEST_MISSING)
        return built.manifest_digest

    return digest


@dataclass(frozen=True)
class StepProposal:
    """A step's proposal as recorded: the effect, the approval request asking about it, and whether this call created the effect."""

    proposal_id: str
    approval_request_id: str
    intent_hash: str
    state: str
    created: bool


class StepParked(Exception):  # noqa: N818 - control flow, not an error: the step is waiting for a person
    """An agent step proposed a protected action and is waiting for its approval; the node must park, not finish.

    Carries identifiers and the usage the provider reported (so the budget is charged), never the action's arguments.
    """

    def __init__(self, proposal: StepProposal, *, step_request_id: str, usage: StepUsage) -> None:
        super().__init__("agent step is waiting for approval of its proposed action")
        self.proposal = proposal
        self.step_request_id = step_request_id
        self.usage = usage


@dataclass(frozen=True)
class PriorProposal:
    """The effect already recorded for a node's occurrence (a resumed or retried step finds it instead of asking the model again)."""

    proposal_id: str
    state: str
    intent_hash: str


def prior_proposal(
    session_factory: Callable[[], Session], workflow_run_id: str, node_id: str, occurrence_id: str
) -> PriorProposal | None:
    """The proposal recorded for this ``(run, node, occurrence)``, by its logical key, or ``None``."""
    key = logical_key(workflow_run_id=workflow_run_id, node_id=node_id, occurrence_id=occurrence_id)
    with session_factory() as session:
        row = session.execute(
            select(
                CaliberEffectProposal.proposal_id,
                CaliberEffectProposal.state,
                CaliberEffectProposal.intent_hash,
            ).where(CaliberEffectProposal.logical_key == key)
        ).first()
    if row is None:
        return None
    return PriorProposal(str(row.proposal_id), str(row.state), str(row.intent_hash))


def step_evidence_hash(request: AgentStepRequest, intent_hash: str) -> str:
    """The digest of what an approver of this step's proposal is shown: the step, what it was given and the exact intent.

    Deterministic, so a retry of the same step presents the same evidence; it changes if the context the model saw or the
    intent does, which voids an approval of the old one (the dispatcher compares it, B06.3).
    """
    material = {
        "kind": "agent_step_proposal",
        "step_request_id": request.step_request_id,
        "run_manifest_digest": request.run_manifest_digest,
        "context_manifest_digest": request.context_manifest_digest,
        "intent_hash": intent_hash,
    }
    return "sha256:" + hashlib.sha256(canonical_bytes(material)).hexdigest()


def protected_tool_of_record(tools: dict[str, ToolTrust]) -> ToolTrust:
    """The tool a proposal is classified under: the strictest of the agent's non-read tools.

    A v1 proposal names no tool, so the platform cannot tell which protected tool the model meant and must not guess a looser one:
    the highest class wins, a tool the profile refuses before one it dispatches, then the name (deterministic). An agent with no
    protected tool cannot propose anything (:data:`NO_PROTECTED_TOOL`): the model invented the call.
    """
    protected = [trust for trust in tools.values() if trust.effect_class != "read"]
    if not protected:
        raise EffectRefused(NO_PROTECTED_TOOL)
    return max(
        protected,
        key=lambda trust: (
            EFFECT_CLASSES.index(trust.effect_class),
            trust.disposition == "refuse",
            trust.name,
        ),
    )


def _backend_of_record(
    context: GovernedStepContext, tools: Mapping[str, ToolTrust], connector: str
) -> tuple[str, ToolTrust]:
    """The backend kind and tool a proposal is held to, from the context's trusted map and never from the model.

    ``context.tool_backends`` says which backend kind each *registered tool* sends through, keyed by the registry identity
    ``(name, version)`` the classifier proved (never by the local name a manifest author chose, which anyone can give any tool).
    It is the producer's, built from trusted configuration; a model, a manifest or a tool call never writes it. Only the agent's
    non-read tools whose registry identity is in it count. The connector the model *named* is used for one thing, to choose among
    them: it must be the connector of a mapped tool's registered backend, or the proposal is refused (``connector_not_mapped``).
    The backend must be one an agent step may use (:data:`AGENT_STEP_BACKENDS`). The tool of record is then the strictest of the
    tools that map to that backend, so a class cannot come from a tool that has nothing to do with the connector.
    """
    if not any(trust.effect_class != "read" for trust in tools.values()):
        raise EffectRefused(NO_PROTECTED_TOOL)
    mapped = {
        name: context.tool_backends[trust.registry]
        for name, trust in tools.items()
        if trust.effect_class != "read"
        and trust.registry is not None
        and context.tool_backends.get(trust.registry) in BACKENDS
    }
    kinds = {kind for kind in mapped.values() if BACKENDS[kind].connector == connector}
    if not kinds:
        raise EffectRefused(CONNECTOR_NOT_MAPPED)
    if len(kinds) > 1:
        raise EffectRefused(CONNECTOR_AMBIGUOUS)
    (kind,) = kinds
    if kind not in AGENT_STEP_BACKENDS:
        raise EffectRefused(BACKEND_NOT_FOR_AGENT_STEPS)
    tool = protected_tool_of_record(
        {name: tools[name] for name, mapped_kind in mapped.items() if mapped_kind == kind}
    )
    if tool.disposition == "refuse":
        raise UnsupportedEffectPath(UNSUPPORTED_EFFECT_PATH)
    return kind, tool


def record_step_proposal(
    context: GovernedStepContext,
    request: AgentStepRequest,
    result: AgentStepResult,
    tools: dict[str, ToolTrust],
) -> StepProposal:
    """Record the ``proposed`` effect and its approval request for a step that stopped with ``proposed_action``.

    ``tools`` is the platform's classification of **every** tool bound to the agent (``local_name -> ToolTrust``). Raises
    :class:`~caliber.effects.dispatcher.EffectRefused` with a fixed code and nothing recorded when: the result holds no single
    action (``no_proposed_action``), the step is not the context's run (``step_mismatch``), the agent has no protected tool
    (``no_protected_tool``), no mapped tool sends through the named connector (``connector_not_mapped``, ``connector_ambiguous``),
    the tool's disposition refuses the path (``unsupported_effect_path``) or the backend is not admitted (the admission's code), the
    destination does not fit the connector (``resource_not_admitted``), the model's claimed class or connector version is not the
    platform's (``claim_mismatch``), or the stored effect differs from the bound action (``intent_mismatch``,
    ``intent_conflict`` for the same occurrence with another intent). The effect and the request are written in one
    transaction: both exist or neither does.
    """
    if result.stop_reason != PROPOSED_ACTION or len(result.proposed_actions) != 1:
        raise EffectRefused(NO_PROPOSED_ACTION)
    if (
        request.workflow_run_id != context.workflow_run_id
        or (result.step_request_id, result.workflow_run_id, result.node_id, result.occurrence_id)
        != (
            request.step_request_id,
            request.workflow_run_id,
            request.node_id,
            request.occurrence_id,
        )
        or result.attempt != request.attempt
    ):
        raise EffectRefused(STEP_MISMATCH)
    action = result.proposed_actions[0]
    kind, tool = _backend_of_record(context, tools, action.connector)
    admitted = admit_backend(kind)
    # The admitted configuration's ``resource`` is a regular expression the destination must fully match; an agent step may only
    # reach a backend that declares one (a literal template, or none, is refused rather than waved through).
    pattern = (BACKENDS[kind].configuration or {}).get("resource")
    if not isinstance(pattern, str) or re.fullmatch(pattern, action.resource) is None:
        raise EffectRefused(RESOURCE_NOT_ADMITTED)
    try:
        bound = action.bound_to(tool.effect_class, connector_version=admitted.connector_version)
    except ProposalError as exc:
        raise EffectRefused(exc.code) from None
    with context.session_factory() as session:
        # The run row first, the order every writer of a run's effects and approval requests uses (run, proposal, request):
        # inserting the proposal already takes a share lock on it (the foreign key), and two proposers that each hold that
        # share lock and then ask for the exclusive one would deadlock.
        _lock_run(session, request.workflow_run_id)
        proposal = submit_proposal(
            session,
            workflow_run_id=request.workflow_run_id,
            node_id=request.node_id,
            occurrence_id=request.occurrence_id,
            action=bound,
            tool_name=tool.name,
            caliber_defined=tool.caliber_defined,
            admission=admitted,
        )
        if proposal.intent_hash != bound.intent_hash:
            session.rollback()
            raise EffectRefused(INTENT_MISMATCH)
        approval = ensure_approval_request(
            session,
            context,
            proposal.proposal_id,
            evidence_hash=step_evidence_hash(request, proposal.intent_hash),
        )
        session.commit()
    return StepProposal(
        proposal_id=proposal.proposal_id,
        approval_request_id=approval.runtime_approval_id,
        intent_hash=proposal.intent_hash,
        state=proposal.state,
        created=proposal.created,
    )


def _lock_run(session: Session, workflow_run_id: str) -> None:
    """Lock the run's row (``FOR UPDATE``; a no-op on SQLite, where a write transaction is exclusive) until the caller ends."""
    row = session.execute(
        select(CaliberWorkflowRun.workflow_run_id)
        .where(CaliberWorkflowRun.workflow_run_id == workflow_run_id)
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise EffectRefused("run_not_found")


def ensure_approval_request(
    session: Session,
    context: GovernedStepContext,
    proposal_id: str,
    *,
    evidence_hash: str | None,
) -> CaliberRuntimeApprovalRequest:
    """The live approval request of ``proposal_id``, created in the caller's transaction when there is none (never commits).

    Serialized on the run row (``FOR UPDATE``, the first lock of the run, proposal, request order every writer uses), so two
    callers for one proposal cannot each create a request and leave one parked behind a request the other superseded: the second
    finds the first's. A request is live while it is ``pending`` or ``approved`` and not consumed, superseded or expired
    (:func:`~caliber.workflows.approval_policy.approval_refusal`); otherwise a new one is created, which supersedes any other live
    request of the node. ``evidence_hash=None`` (a resumed step has no request to hash) reuses the evidence of the proposal's latest
    request, and refuses (:data:`APPROVAL_EVIDENCE_UNAVAILABLE`) when there is none.

    A **rejected** request is final: ``approval_rejected``, nothing is asked again. Raises
    :class:`~caliber.effects.dispatcher.EffectRefused` (``proposal_not_proposed`` for a proposal that has moved on,
    ``bad_field`` for a request the runtime cannot honour).
    """
    run_id = session.execute(
        select(CaliberEffectProposal.workflow_run_id).where(
            CaliberEffectProposal.proposal_id == proposal_id
        )
    ).scalar_one_or_none()
    if run_id is None:
        raise EffectRefused("proposal_not_found")
    _lock_run(session, run_id)
    proposal = session.execute(
        select(CaliberEffectProposal)
        .where(CaliberEffectProposal.proposal_id == proposal_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()
    if proposal.state != PROPOSED:
        raise EffectRefused("proposal_not_proposed")
    requests = (
        session.execute(
            select(CaliberRuntimeApprovalRequest)
            .where(CaliberRuntimeApprovalRequest.proposal_id == proposal_id)
            .order_by(CaliberRuntimeApprovalRequest.requested_at.desc())
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        .scalars()
        .all()
    )
    if any(existing.status == "rejected" for existing in requests):
        # A person said no to this exact intent. Asking again would put the same action in front of another approver until
        # one agrees, so a rejection is final for the proposal.
        raise EffectRefused(APPROVAL_REJECTED)
    now = datetime.now(timezone.utc)
    for existing in requests:
        if (
            existing.status in _LIVE_STATUSES
            and existing.consumed_at is None
            and approval_refusal(
                status=existing.status,
                superseded_by=existing.superseded_by,
                expires_at=existing.expires_at,
                now=now,
            )
            is None
        ):
            return existing
    if evidence_hash is None:
        if not requests or not requests[0].evidence_hash:
            raise EffectRefused(APPROVAL_EVIDENCE_UNAVAILABLE)
        evidence_hash = requests[0].evidence_hash
    try:
        return create_approval_request(
            session,
            proposal=proposal,
            policy=context.approval_policy,
            evidence_hash=evidence_hash,
            requested_by=context.requested_by,
            ttl_seconds=context.approval_ttl_seconds,
        )
    except ApprovalRequestError as exc:
        raise EffectRefused(exc.code) from None


def reask_approval(context: GovernedStepContext, prior: PriorProposal) -> StepProposal:
    """For a resumed step whose proposal is still ``proposed``: its live approval request, re-asked when the last one lapsed."""
    with context.session_factory() as session:
        approval = ensure_approval_request(session, context, prior.proposal_id, evidence_hash=None)
        session.commit()
    return StepProposal(
        proposal_id=prior.proposal_id,
        approval_request_id=approval.runtime_approval_id,
        intent_hash=prior.intent_hash,
        state=prior.state,
        created=False,
    )


__all__ = [
    "AGENT_STEP_BACKENDS",
    "APPROVAL_EVIDENCE_UNAVAILABLE",
    "APPROVAL_REJECTED",
    "BACKEND_NOT_FOR_AGENT_STEPS",
    "CLAIM_MISMATCH",
    "CONNECTOR_AMBIGUOUS",
    "CONNECTOR_NOT_MAPPED",
    "CONTEXT_MANIFEST_MISSING",
    "INTENT_MISMATCH",
    "NO_PROPOSED_ACTION",
    "NO_PROTECTED_TOOL",
    "PROPOSED_ACTION",
    "RESOURCE_NOT_ADMITTED",
    "STEP_MISMATCH",
    "UNSUPPORTED_EFFECT_PATH",
    "GovernedStepContext",
    "PriorProposal",
    "StepParked",
    "StepProposal",
    "ToolClassifier",
    "ToolTrust",
    "ensure_approval_request",
    "prior_proposal",
    "protected_tool_of_record",
    "reask_approval",
    "record_step_proposal",
    "registry_tool_classifier",
    "step_evidence_hash",
    "stored_context_manifest_digest",
    "untrusted_tool",
]
