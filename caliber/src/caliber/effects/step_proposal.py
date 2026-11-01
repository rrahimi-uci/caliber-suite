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
   Nothing is dispatched here: dispatch needs an approval decision, an owner lease and a backend (B04.1; the producer that holds the
   lease dispatches, #1051).
4. **A model's call of a registered, OpenAPI or MCP tool** (B04.6, #768) is bound from the tool's trusted identity by the runtime's
   :class:`~caliber.effects.model_tool_calls.ToolProposalBinder`, not from the model's connector and resource (see
   :func:`record_step_proposal`).

Every refusal is :class:`~caliber.effects.dispatcher.EffectRefused` with a fixed code and **no** model-supplied text. The runtime
parks the run (:class:`StepParked`) behind the request; see :mod:`caliber.workflows.runtime`.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Final, Protocol

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from caliber.config import AGENT_RUN_MAX_TOKENS_BOUNDS, AGENT_RUN_MAX_TOKENS_DEFAULT
from caliber.contracts.agent_step import (
    UNCLAIMED,
    UNCLAIMED_EFFECT_CLASS,
    ProposedAction,
    StepLimits,
)
from caliber.db.models import (
    CaliberEffectProposal,
    CaliberRuntimeApprovalRequest,
    CaliberToolRegistry,
    CaliberWorkflowRun,
)
from caliber.effects.admission import AdmittedConnector
from caliber.effects.dispatcher import (
    BACKENDS,
    EffectOutcome,
    EffectRefused,
    UnsupportedEffectPath,
    admit_backend,
    proposal_effect_class,
    submit_proposal,
)
from caliber.effects.inventory import (
    EFFECT_CLASSES,
    FAIL_CLOSED,
    InventoryError,
    effect_class,
    tool_disposition,
)
from caliber.effects.model_tool_calls import (
    REGISTRY_ROW_MISMATCH,
    TOOL_NOT_PROTECTED,
    ToolProposal,
    ToolProposalBinder,
)
from caliber.effects.records import PROPOSED, ProposalError, logical_key
from caliber.effects.registered_tools import declared_effect_class
from caliber.effects.tool_backends import AGENT_STEP_BACKENDS
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
# ``AGENT_STEP_BACKENDS`` (imported above) are the backend kinds a *model's* proposal may reach. Each must have a destination the
# platform checks at proposal time: the admitted ``document_request`` connector declares a resource pattern
# (``BACKENDS[kind].configuration["resource"]``, a regular expression the destination must fully match). ``http`` is not there (its
# destination is an arbitrary URL and the egress check runs at dispatch, after an approval would be spent) and neither is a backend
# whose configuration holds a literal resource template rather than a pattern. A producer that maps a tool to one of those gets
# ``backend_not_for_agent_steps``.
RESOURCE_NOT_ADMITTED: Final = "resource_not_admitted"
STEP_MISMATCH: Final = "step_mismatch"
INTENT_MISMATCH: Final = "intent_mismatch"
#: A proposal named a function the agent does not have (the adapter refuses that itself; this is the platform's own check).
UNKNOWN_TOOL: Final = "unknown_tool"
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
                        CaliberToolRegistry.side_effect_level,
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
            klass = effect_class(row.name, caliber_defined=trusted)
            if trusted:
                # The same rule a workflow tool node applies (``route_registered_tool``): the class of a CALIBER built-in is
                # the stricter of the inventory's and the one its own registry row declares, so a row declared stricter than
                # the inventory is not read here while a node would dispatch it.
                klass = max(
                    klass, declared_effect_class(row.side_effect_level), key=EFFECT_CLASSES.index
                )
            return ToolTrust(
                name=row.name if trusted else name,
                effect_class=klass,
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
    #: The MCP tools, as ``(server_id, tool_name)``, a model may propose a protected call of (B04.6). An MCP tool has no registry
    #: row, so ``tool_backends`` cannot name it; this is the same kind of allow-list, the producer's, from trusted configuration:
    #: nothing a model, a manifest or a tool call writes. A tool absent from it can be refused or classified but never proposed.
    mcp_tools: frozenset[tuple[str, str]] = field(default_factory=frozenset)
    #: The most any step may be given (``CALIBER_AGENT_STEP_MAX_*``; build with ``StepCeilings.from_config``). The default is the
    #: settings' own defaults, so a context that names none is capped, not uncapped.
    ceilings: StepCeilings = field(default_factory=StepCeilings)
    #: The clock a step's deadline is read from (wall time and a monotonic clock); ``None`` is the platform's own, a fake one in tests.
    clock: StepClock | None = None
    #: The most tokens the run's governed steps may reserve and be charged in total (``CALIBER_AGENT_RUN_MAX_TOKENS``; B08.4). A step's
    #: ``max_tokens`` ceiling is reserved against it before the adapter is called. The default is the setting's own, so a context
    #: that names none is bounded, not unbounded.
    run_token_budget: int = AGENT_RUN_MAX_TOKENS_DEFAULT
    #: The agent nodes (by node id) this context governs. ``None`` (the default, and every context built before the worker's) governs
    #: **every** agent node of the plan. A producer that governs only the agents that can reach a mapped protected tool names them
    #: here, so an agent that answers in text keeps running as before: a governed step has no text channel in the v1 result contract.
    governed_nodes: frozenset[str] | None = None
    #: Sends an approved proposal of this run (#1051). Called with the proposal a resumed step finds still ``proposed``: it returns
    #: ``None`` while the exact approval is pending (the node parks again behind its live request), the :class:`EffectOutcome` of
    #: the dispatch once it was approved and claimed, and raises :class:`~caliber.effects.dispatcher.EffectRefused` with a fixed
    #: code when the dispatcher refuses (no lease, the approver's authority gone, the connector not bound). ``None`` here (the
    #: default): nothing dispatches, an approved proposal stays ``proposed`` and the run keeps parking, as before the producer.
    dispatch_approved: Callable[[PriorProposal], EffectOutcome | None] | None = None

    def __post_init__(self) -> None:
        low, high = AGENT_RUN_MAX_TOKENS_BOUNDS
        if type(self.run_token_budget) is not int or not low <= self.run_token_budget <= high:
            raise ValueError(f"run_token_budget must be an integer from {low} to {high}")

    def governs(self, node_id: str) -> bool:
        """Whether the agent node ``node_id`` runs as a governed step under this context."""
        return self.governed_nodes is None or node_id in self.governed_nodes


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


class EffectApprovalPending(Exception):  # noqa: N818 - control flow, not an error: the node is waiting for a person
    """A governed workflow effect (a node's own proposal, not an agent step's) is waiting for its exact approval: the node must park.

    Raised by the producer's ``authorize`` while the approval request bound to the recorded proposal has no decision, and turned
    by the interpreter into a ``waiting_approval`` park behind that request. Carries identifiers only, never the effect's arguments.
    """

    def __init__(self, *, proposal_id: str, approval_request_id: str, intent_hash: str) -> None:
        super().__init__("a governed effect is waiting for its approval")
        self.proposal_id = proposal_id
        self.approval_request_id = approval_request_id
        self.intent_hash = intent_hash
        self.blocked_node_id: str | None = None
        self.blocked_inputs: dict[str, object] | None = None


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


#: The backend kinds a *model's tool proposal* may reach through a :class:`~caliber.effects.model_tool_calls.ToolProposalBinder`:
#: the registered-tool, OpenAPI and MCP transports (B04.6). Unlike :data:`AGENT_STEP_BACKENDS` their destination is not the
#: model's to choose within a pattern: it is derived from the tool's trusted identity, and the model's claim must equal it.
TOOL_PROPOSAL_BACKENDS: Final = frozenset({"registered_tool", "openapi_http", "mcp"})


def _tool_backend_connectors() -> frozenset[str]:
    return frozenset(BACKENDS[kind].connector for kind in TOOL_PROPOSAL_BACKENDS)


def _bind_tool_proposal(
    context: GovernedStepContext,
    tools: Mapping[str, ToolTrust],
    bindings: Sequence[IRToolBinding],
    binder: ToolProposalBinder,
    action: ProposedAction,
) -> ToolProposal:
    """What the agent's one protected tool that ``action`` proposes would record, from the tool's trusted identity (B04.6).

    A v1 proposal names no tool, so the platform finds it: a candidate is an agent tool the platform did **not** classify
    ``read`` whose connector (a function of its kind) is the one the model named. Each candidate is asked, by the runtime's
    binder, what its proposal for the model's arguments is (the same classification helpers a workflow tool node uses: the
    registry row or the MCP server record and the administrator's tool policy, the trusted input schema). The proposal is the
    candidate's only if the resource it derives is the one the model named, so the model chooses *among* the agent's protected
    tools and nothing else. Two candidates that derive the same proposal are the same tool; two that derive different ones are
    ``connector_ambiguous``. The producer's allow-list then decides whether this tool may be proposed at all
    (``tool_backends`` for a registry tool, ``mcp_tools`` for an MCP tool), and the registry row the binder read must be the one
    the classifier did (a row that changed in between is ``registry_row_mismatch``).
    """
    candidates = [
        binding
        for binding in bindings
        if binding.local_name in tools
        and tools[binding.local_name].effect_class != "read"
        and binder.connector(binding) == action.connector
    ]
    if not candidates:
        raise EffectRefused(CONNECTOR_NOT_MAPPED)
    refusals: list[str] = []
    matches: list[tuple[IRToolBinding, ToolProposal]] = []
    for binding in candidates:
        hinted = binder.static_resource(binding)
        if hinted is not None and hinted != action.resource:
            continue
        try:
            proposal = binder.bind(binding, action.intent.plain_args)
        except EffectRefused as exc:
            refusals.append(exc.code)
            continue
        if proposal.resource == action.resource and proposal.connector == action.connector:
            matches.append((binding, proposal))
    if not matches:
        raise EffectRefused(refusals[0] if refusals else RESOURCE_NOT_ADMITTED)
    first_binding, first = matches[0]
    if any(proposal != first for _, proposal in matches[1:]):
        raise EffectRefused(CONNECTOR_AMBIGUOUS)
    _check_tool_allowed(context, tools[first_binding.local_name], first)
    return first


def _check_tool_allowed(
    context: GovernedStepContext, trust: ToolTrust, proposal: ToolProposal
) -> None:
    """The producer's allow-list for a tool proposal: the registry row read must be the classified one, and the tool mapped."""
    if proposal.registry is not None:
        if trust.registry != proposal.registry:
            raise EffectRefused(REGISTRY_ROW_MISMATCH)
        allowed = context.tool_backends.get(proposal.registry) == proposal.backend_kind
    else:
        allowed = proposal.mcp is not None and proposal.mcp in context.mcp_tools
    if proposal.backend_kind not in TOOL_PROPOSAL_BACKENDS or not allowed:
        raise EffectRefused(CONNECTOR_NOT_MAPPED)


@dataclass(frozen=True)
class _Resolved:
    """A proposal as the platform will record it: the bound action, its admission and what ``submit_proposal`` classifies by."""

    bound: ProposedAction
    admitted: AdmittedConnector
    tool_name: str
    caliber_defined: bool
    floor: str | None


def _claims_hold(
    action: ProposedAction,
    *,
    connector: str,
    version: str,
    effect_class: str,
    expected_resource_version: str | None = None,
) -> None:
    """A proposal that names its tool claims nothing: each claim field is the placeholder or exactly the platform's value."""
    if (
        action.connector not in (UNCLAIMED, connector)
        or action.connector_version not in (UNCLAIMED, version)
        or action.effect_class not in (UNCLAIMED_EFFECT_CLASS, effect_class)
        or action.expected_resource_version not in (None, expected_resource_version)
    ):
        raise EffectRefused(CLAIM_MISMATCH)


def _rebuilt(action: ProposedAction, **fields: Any) -> ProposedAction:
    """The action the platform records: its own connector, version, class and (for a tool) resource, the model's arguments."""
    values: dict[str, Any] = {
        "args": action.intent.plain_args,
        "expected_resource_version": action.expected_resource_version,
        "tool": action.tool,
    }
    values.update(fields)
    try:
        return ProposedAction(**values).bound_to(
            values["effect_class"], connector_version=values["connector_version"]
        )
    except ProposalError as exc:
        raise EffectRefused(exc.code) from None


def _bind_named_tool(
    context: GovernedStepContext,
    tools: Mapping[str, ToolTrust],
    bindings: Sequence[IRToolBinding],
    binder: ToolProposalBinder | None,
    action: ProposedAction,
) -> _Resolved:
    """A proposal that names the function the model called (:attr:`ProposedAction.tool`), bound from that tool's trusted identity.

    The tool is found by the **name the model called** among the agent's bindings, not by any connector or resource the model
    claimed; the model supplied arguments only. A name that is not an agent tool is ``unknown_tool`` and a tool the platform
    classifies ``read`` is ``tool_not_protected`` (a read runs inline, it is never proposed). A registered, OpenAPI or MCP tool is
    described by the runtime's binder (connector, resource and intent arguments from the registry row or the server record, the
    arguments checked against the trusted schema) and held to the producer's allow-list. Any other tool must be mapped by
    ``tool_backends`` to a backend an agent step may reach, whose destination the model names within the connector's pattern.
    Every claim field is the placeholder or the platform's own value (:func:`_claims_hold`).
    """
    binding = next((item for item in bindings if item.local_name == action.tool), None)
    trust = tools.get(action.tool or "")
    if binding is None or trust is None:
        raise EffectRefused(UNKNOWN_TOOL)
    if trust.effect_class == "read":
        raise EffectRefused(TOOL_NOT_PROTECTED)
    mapped = None if trust.registry is None else context.tool_backends.get(trust.registry)
    if (
        mapped not in AGENT_STEP_BACKENDS
        and binder is not None
        and binder.connector(binding) is not None
    ):
        proposal = binder.bind(binding, action.intent.plain_args)
        _check_tool_allowed(context, trust, proposal)
        admitted = admit_backend(proposal.backend_kind)
        platform_class = proposal_effect_class(
            proposal.tool_name,
            caliber_defined=proposal.caliber_defined,
            floor=proposal.effect_floor,
        )
        _claims_hold(
            action,
            connector=proposal.connector,
            version=admitted.connector_version,
            effect_class=platform_class,
            expected_resource_version=proposal.expected_resource_version,
        )
        if action.resource not in (UNCLAIMED, proposal.resource):
            raise EffectRefused(RESOURCE_NOT_ADMITTED)
        bound = _rebuilt(
            action,
            connector=proposal.connector,
            connector_version=admitted.connector_version,
            effect_class=platform_class,
            resource=proposal.resource,
            args=proposal.args,
            expected_resource_version=proposal.expected_resource_version,
        )
        return _Resolved(
            bound, admitted, proposal.tool_name, proposal.caliber_defined, proposal.effect_floor
        )
    kind = mapped
    if kind is None or kind not in BACKENDS:
        raise EffectRefused(CONNECTOR_NOT_MAPPED)
    if kind not in AGENT_STEP_BACKENDS:
        raise EffectRefused(BACKEND_NOT_FOR_AGENT_STEPS)
    if trust.disposition == "refuse":
        raise UnsupportedEffectPath(UNSUPPORTED_EFFECT_PATH)
    admitted = admit_backend(kind)
    connector = BACKENDS[kind].connector
    _claims_hold(
        action,
        connector=connector,
        version=admitted.connector_version,
        effect_class=trust.effect_class,
    )
    # The destination is the model's to name within the admitted connector's pattern; the placeholder never fits it.
    pattern = (BACKENDS[kind].configuration or {}).get("resource")
    if not isinstance(pattern, str) or re.fullmatch(pattern, action.resource) is None:
        raise EffectRefused(RESOURCE_NOT_ADMITTED)
    bound = _rebuilt(
        action,
        connector=connector,
        connector_version=admitted.connector_version,
        effect_class=trust.effect_class,
        resource=action.resource,
    )
    return _Resolved(bound, admitted, trust.name, trust.caliber_defined, None)


def _resolve_action(
    context: GovernedStepContext,
    tools: Mapping[str, ToolTrust],
    bindings: Sequence[IRToolBinding],
    binder: ToolProposalBinder | None,
    action: ProposedAction,
) -> _Resolved:
    """What the platform records for ``action``: bound from the tool the model called, or from the claim it made."""
    if action.tool is not None:
        return _bind_named_tool(context, tools, bindings, binder, action)
    if binder is not None and action.connector in _tool_backend_connectors():
        tool_proposal = _bind_tool_proposal(context, tools, bindings, binder, action)
        admitted = admit_backend(tool_proposal.backend_kind)
        # What ``submit_proposal`` will record for this tool: the inventory's class, or the stricter floor of the route.
        platform_class = proposal_effect_class(
            tool_proposal.tool_name,
            caliber_defined=tool_proposal.caliber_defined,
            floor=tool_proposal.effect_floor,
        )
        if action.expected_resource_version not in (None, tool_proposal.expected_resource_version):
            raise EffectRefused(CLAIM_MISMATCH)
        try:
            # The model's claims (class, version) are checked, then the action the platform records is rebuilt from the
            # trusted identity: nothing of the model's survives but the arguments that passed the trusted schema.
            action.bound_to(platform_class, connector_version=admitted.connector_version)
        except ProposalError as exc:
            raise EffectRefused(exc.code) from None
        bound = _rebuilt(
            action,
            connector=tool_proposal.connector,
            connector_version=admitted.connector_version,
            effect_class=platform_class,
            resource=tool_proposal.resource,
            args=tool_proposal.args,
            expected_resource_version=tool_proposal.expected_resource_version,
        )
        return _Resolved(
            bound,
            admitted,
            tool_proposal.tool_name,
            tool_proposal.caliber_defined,
            tool_proposal.effect_floor,
        )
    kind, tool = _backend_of_record(context, dict(tools), action.connector)
    admitted = admit_backend(kind)
    # The admitted configuration's ``resource`` is a regular expression the destination must fully match; an agent step may
    # only reach a backend that declares one (a literal template, or none, is refused rather than waved through).
    pattern = (BACKENDS[kind].configuration or {}).get("resource")
    if not isinstance(pattern, str) or re.fullmatch(pattern, action.resource) is None:
        raise EffectRefused(RESOURCE_NOT_ADMITTED)
    try:
        bound = action.bound_to(tool.effect_class, connector_version=admitted.connector_version)
    except ProposalError as exc:
        raise EffectRefused(exc.code) from None
    return _Resolved(bound, admitted, tool.name, tool.caliber_defined, None)


def record_step_proposal(
    context: GovernedStepContext,
    request: AgentStepRequest,
    result: AgentStepResult,
    tools: dict[str, ToolTrust],
    *,
    binder: ToolProposalBinder | None = None,
    bindings: Sequence[IRToolBinding] = (),
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

    With a ``binder`` (the runtime's, B04.6) a proposal that names a registered-tool, OpenAPI or MCP connector is a proposal of
    one of the agent's ``bindings``: the platform finds the tool (:func:`_bind_tool_proposal`), takes the connector, resource and
    intent arguments **from the tool's trusted identity** (the model's arguments are embedded only after the trusted input
    schema accepted them) and refuses the model's claim if its class, version or precondition differs. Any other connector is
    held to the ``tool_backends`` / :data:`AGENT_STEP_BACKENDS` rules above.
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
    resolved = _resolve_action(context, tools, bindings, binder, action)
    bound, admitted = resolved.bound, resolved.admitted
    tool_name, caliber_defined, floor = resolved.tool_name, resolved.caliber_defined, resolved.floor
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
            tool_name=tool_name,
            caliber_defined=caliber_defined,
            min_effect_class=floor,
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


class ApprovalAsk(Protocol):
    """What :func:`ensure_approval_request` needs to ask for an approval: the policy, who asks and for how long.

    A :class:`GovernedStepContext` has all three; the producer's effect authority (:mod:`caliber.effects.governed_context`) has
    them too, so a workflow node's proposal and an agent step's are asked about the same way.
    """

    @property
    def approval_policy(self) -> ApprovalPolicy: ...

    @property
    def requested_by(self) -> str: ...

    @property
    def approval_ttl_seconds(self) -> int: ...


def ensure_approval_request(
    session: Session,
    context: ApprovalAsk,
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
    "TOOL_PROPOSAL_BACKENDS",
    "UNKNOWN_TOOL",
    "UNSUPPORTED_EFFECT_PATH",
    "ApprovalAsk",
    "EffectApprovalPending",
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
