"""The producer of the governed contexts: what the workflow-run worker binds for a run it owns (#1051; ADR 0002, 0003).

The runtime (:mod:`caliber.workflows.runtime`) only *consumes* a :class:`~caliber.workflows.runtime.GovernedEffectContext`
and a :class:`~caliber.effects.step_proposal.GovernedStepContext`; it never builds one. This module is the one place that does,
from state the **server** holds, and it takes nothing from a request, a manifest, a tool call or a model:

* **The execution-owner lease** is read at the moment of use from ``lease_provider`` (the lifespan binds it to the
  :class:`~caliber.execution_role.ExecutionRoleController`, as it does for the reconciler). No lease, no dispatch: ``lease_unavailable``.
* **The approver's authority** is not taken from the request that decided. The decision route records *who* decided
  (``decided_by`` and the quorum in ``policy_snapshot['approvals']``); the scopes a dispatch attests are derived **now** from the
  configured user lists (:func:`caliber.auth.scopes_for_user`) for every one of those approvers and **intersected**, so a scope
  revoked since the decision is gone, and the quorum is only as strong as its weakest approver. Project role, membership, the
  requester and the initiator are re-derived by the commit recheck itself (:func:`~caliber.workflows.approval_policy.approval_valid_at_commit`).
* **The requester** the approval names is the run's initiator (``run.created_by``), read from the run row.
* **The downstream** an effect is sent to is the ``base_url`` of the connector's binding in ``CALIBER_EFFECT_CALLBACK_CONNECTORS``
  for the run's project: the same address the reconciler asks, so an effect is never sent where it cannot be looked up. No binding,
  no dispatch: ``connector_unbound``.
* **Which tools an agent step may propose through** is ``CALIBER_AGENT_STEP_TOOL_BACKENDS``, keyed by registry identity.
* **Step limits** are the operator's ceilings (``CALIBER_AGENT_STEP_MAX_*``) and the run's token budget is
  ``CALIBER_AGENT_RUN_MAX_TOKENS``.

What is **not** here, on purpose: an authoritative receipt verifier for the generic ``http``, ``registered_tool`` and
``openapi_http`` backends. They have no lookup by key the platform can call, so an outcome the platform cannot read back would end
``unknown`` and park the run with no way out (#1055). The effect context therefore names the backends it has a verifier for
(:attr:`GovernedEffectContext.dispatchable_backends`, none in production) and a workflow node that would send through any other is
refused **before** its proposal is recorded: ``no_authoritative_verifier``.
"""

from __future__ import annotations

import hashlib
import logging
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.auth import scopes_for_user
from caliber.config import CaliberConfig
from caliber.contracts.agent_step import StepLimits
from caliber.db.models import (
    CaliberEffectProposal,
    CaliberRuntimeApprovalRequest,
    CaliberWorkflowRun,
)
from caliber.effects.callbacks import parse_bindings
from caliber.effects.dispatcher import (
    BACKENDS,
    EffectOutcome,
    EffectRefused,
    EffectRequest,
    dispatch,
)
from caliber.effects.reconcile import targets_from_bindings
from caliber.effects.records import PROPOSED, RecordedProposal, proposal_view
from caliber.effects.step_proposal import (
    AGENT_STEP_BACKENDS,
    EffectApprovalPending,
    GovernedStepContext,
    PriorProposal,
    ToolClassifier,
    ensure_approval_request,
    registry_tool_classifier,
    stored_context_manifest_digest,
)
from caliber.effects.tool_backends import parse_tool_backends
from caliber.ownership import Lease
from caliber.run_manifest import canonical_bytes
from caliber.workflows.agent_step import StepCeilings
from caliber.workflows.approval_policy import ApprovalPolicy, approval_valid_at_commit
from caliber.workflows.approval_requests import configured_ttl_seconds
from caliber.workflows.ir import IRAgent
from caliber.workflows.runtime import EffectAuthorization, GovernedEffectContext

logger = logging.getLogger("caliber.effects.governed_context")

#: Why the worker could not build a context it needs (``GovernedContextUnavailable.code``); fixed tokens, never a value.
LEASE_PROVIDER_UNBOUND: Final = "lease_provider_unbound"
REQUESTER_UNAVAILABLE: Final = "requester_unavailable"
RUN_NOT_ADMITTED: Final = "run_not_admitted"
RUN_MANIFEST_UNAVAILABLE: Final = "run_manifest_unavailable"
CONTEXT_RECORDER_UNAVAILABLE: Final = "context_recorder_unavailable"
CONFIGURATION_INVALID: Final = "configuration_invalid"
#: Why an authorization or a dispatch was refused (``EffectRefused.code``).
LEASE_UNAVAILABLE: Final = "lease_unavailable"
CONNECTOR_UNBOUND: Final = "connector_unbound"
VERSION_UNVERIFIABLE: Final = "version_unverifiable"

LeaseProvider = Callable[[], Lease | None]
#: ``(request, response) -> receipt facts``: the authoritative read-back a backend needs to settle an outcome as ``committed``.
ReceiptVerifier = Callable[[EffectRequest, Mapping[str, Any]], Any]

_INVISIBLE_CATEGORIES: Final = frozenset({"Cc", "Cf", "Cs"})
_MAX_REQUESTER: Final = 256
_SHA256_HEX_LENGTH: Final = 64


class GovernedContextUnavailable(RuntimeError):  # noqa: N818 - named for the condition, like ``GrantDenied`` and ``LeaseLost``
    """A trusted input the governed path needs is not available, so the run must not proceed ungoverned.

    ``code`` is one of the fixed tokens above; the message is the code alone (nothing a run supplied reaches it).
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def effect_evidence_hash(
    *, run_manifest_digest: str, node_id: str, occurrence_id: str, intent_hash: str
) -> str:
    """The digest of what an approver of a workflow node's effect is shown: the run, the node occurrence and the exact intent.

    Deterministic, so a retry or a resume presents the same evidence; it changes if the run's admitted manifest, the node
    occurrence or the intent does, which voids an approval of the old one (the dispatcher compares it, B06.3).
    """
    material = {
        "kind": "workflow_effect_proposal",
        "run_manifest_digest": run_manifest_digest,
        "node_id": node_id,
        "occurrence_id": occurrence_id,
        "intent_hash": intent_hash,
    }
    return "sha256:" + hashlib.sha256(canonical_bytes(material)).hexdigest()


def _checked_requester(value: object) -> str:
    """The run's initiator as an approval request names it, or ``GovernedContextUnavailable`` (never a truncated or repaired id)."""
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > _MAX_REQUESTER
        or any(unicodedata.category(character) in _INVISIBLE_CATEGORIES for character in value)
    ):
        raise GovernedContextUnavailable(REQUESTER_UNAVAILABLE)
    return value


@dataclass(frozen=True)
class RunAuthority:
    """Everything the worker knows about one run it owns, from the server's own state, and what it may do with it.

    It is the ``ApprovalAsk`` of :func:`~caliber.effects.step_proposal.ensure_approval_request` (the policy, the requester and the
    window of the approval a proposal needs) and the one place an authorization is made: :meth:`authorize` for a workflow node's
    proposal and :meth:`dispatch_approved` for an agent step's.
    """

    session_factory: Callable[[], Session]
    workflow_run_id: str
    project_id: str | None
    config: CaliberConfig
    lease_provider: LeaseProvider
    run_manifest_digest: str
    requested_by: str
    approval_policy: ApprovalPolicy
    approval_ttl_seconds: int
    #: ``(connector, project) -> the downstream's base URL``, from ``CALIBER_EFFECT_CALLBACK_CONNECTORS``.
    targets: Mapping[tuple[str, str], str]

    # -- the authorization ---------------------------------------------------------------------

    def authorize(self, proposal_id: str, *, evidence_hash: str | None) -> EffectAuthorization:
        """The exact authorization for ``proposal_id``, or :class:`EffectApprovalPending` while no one has decided.

        The proposal's live approval request is found, or created in this transaction when there is none (a rejected request is
        final, ``approval_rejected``; a lapsed or superseded one is asked again) and committed before anything else, so a run
        that parks behind it parks behind a request that exists. With a decision recorded this returns what the dispatcher needs
        and nothing it could use alone: the request's id, the approvers' *current* scopes, the lease the execution owner holds now
        and the evidence the approver saw. The dispatcher still rechecks the exact intent, the policy, every approver's project
        role, the requester and the initiator, and the run's own grant, in the transaction that consumes the approval.
        """
        with self.session_factory() as session:
            approval = ensure_approval_request(
                session, self, proposal_id, evidence_hash=evidence_hash
            )
            approval_id = approval.runtime_approval_id
            intent_hash = str(approval.intent_hash)
            decided = approval.status == "approved"
            approvers = self._approvers(approval) if decided else ()
            session.commit()
        if not decided:
            raise EffectApprovalPending(
                proposal_id=proposal_id, approval_request_id=approval_id, intent_hash=intent_hash
            )
        lease = self.lease_provider()
        if lease is None:
            raise EffectRefused(LEASE_UNAVAILABLE)
        return EffectAuthorization(
            approval_id=approval_id,
            approver_scopes=self._approver_scopes(approvers),
            lease=lease,
            current_resource_version=self._current_version(proposal_id),
            evidence_hash=evidence_hash,
        )

    @staticmethod
    def _approvers(approval: CaliberRuntimeApprovalRequest) -> tuple[str, ...]:
        recorded = (approval.policy_snapshot or {}).get("approvals")
        earlier = recorded if isinstance(recorded, list) else []
        return tuple(
            dict.fromkeys(
                name for name in (approval.decided_by, *earlier) if isinstance(name, str) and name
            )
        )

    def _approver_scopes(self, approvers: Iterable[str]) -> frozenset[str]:
        """The scopes **every** approver holds now, from the configured user lists (empty when there is none to ask about)."""
        held = [scopes_for_user(self.config, approver) for approver in approvers]
        return frozenset.intersection(*held) if held else frozenset()

    def _current_version(self, proposal_id: str) -> str | None:
        """The downstream's current version of the target, for a proposal that names the version it expects.

        The platform has no read of its own for a connector's target, so a versioned proposal is sent only through a connector
        whose downstream enforces the precondition **atomically with the write** (a precondition header and a stale status in
        its capability record): a stale version is then a definitive rejection at the downstream (``failed``), not a write over
        newer data. Any other connector with a versioned proposal is refused (``version_unverifiable``).
        """
        with self.session_factory() as session:
            view = proposal_view(session, proposal_id)
        if view.expected_resource_version is None:
            return None
        for registration in BACKENDS.values():
            capability = registration.capability
            if (
                registration.connector == view.connector
                and capability is not None
                and capability.precondition_header is not None
                and capability.stale_precondition_status is not None
            ):
                return view.expected_resource_version
        raise EffectRefused(VERSION_UNVERIFIABLE)

    # -- a workflow node's effect --------------------------------------------------------------

    def authorize_recorded(self, recorded: RecordedProposal) -> EffectAuthorization:
        """``GovernedEffectContext.authorize``: the authorization of a node's freshly recorded proposal."""
        with self.session_factory() as session:
            row = session.execute(
                select(CaliberEffectProposal.node_id, CaliberEffectProposal.occurrence_id).where(
                    CaliberEffectProposal.proposal_id == recorded.proposal_id
                )
            ).one()
        evidence = effect_evidence_hash(
            run_manifest_digest=self.run_manifest_digest,
            node_id=str(row.node_id),
            occurrence_id=str(row.occurrence_id),
            intent_hash=recorded.intent_hash,
        )
        return self.authorize(recorded.proposal_id, evidence_hash=evidence)

    # -- an agent step's effect ----------------------------------------------------------------

    def dispatch_approved(self, prior: PriorProposal) -> EffectOutcome | None:
        """``GovernedStepContext.dispatch_approved``: send a step's proposal once it is approved, once, under the lease.

        ``None`` while the approval is pending. Otherwise the dispatcher consumes the approval and claims the dispatch in one
        transaction before the transport is called (a retry of a claimed proposal sends nothing), and the receipt is read back by
        the connector's own lookup by key. The backend is the one the *proposal's connector* is registered under, restricted to the
        backends an agent step may reach, and its downstream is the connector's binding for the run's project: a proposal whose
        connector has none is refused here, with the proposal still ``proposed`` and its approval unconsumed.
        """
        with self.session_factory() as session:
            view = proposal_view(session, prior.proposal_id)
        if view.state != PROPOSED:
            raise EffectRefused("proposal_not_proposed")
        kind = next(
            (
                name
                for name, registration in BACKENDS.items()
                if registration.connector == view.connector and name in AGENT_STEP_BACKENDS
            ),
            None,
        )
        if kind is None:
            raise EffectRefused("unsupported_backend")
        base_url = self.targets.get((view.connector, self.project_id or ""))
        if base_url is None:
            raise EffectRefused(CONNECTOR_UNBOUND)
        try:
            authorization = self.authorize(prior.proposal_id, evidence_hash=None)
        except EffectApprovalPending:
            return None
        return dispatch(
            self.session_factory,
            prior.proposal_id,
            approval_id=authorization.approval_id,
            approver_scopes=authorization.approver_scopes,
            lease=authorization.lease,
            backend_kind=kind,
            backend_config={"base_url": base_url, "max_attempts": 1},
            current_resource_version=authorization.current_resource_version,
            evidence_hash=authorization.evidence_hash,
            approval_validator=approval_valid_at_commit,
        )


def run_authority(
    *,
    session_factory: Callable[[], Session],
    run: CaliberWorkflowRun,
    config: CaliberConfig,
    lease_provider: LeaseProvider | None,
    run_manifest_digest: str,
) -> RunAuthority:
    """The authority of the run the worker is executing, from the run row and the configuration; refuses rather than guesses.

    Raises :class:`GovernedContextUnavailable` when there is no lease provider (the process is not wired to the execution-owner
    lease), the run has no usable initiator or its manifest digest is not the SHA-256 hex digest of a stored manifest, or the
    configuration cannot be read.
    """
    if lease_provider is None:
        raise GovernedContextUnavailable(LEASE_PROVIDER_UNBOUND)
    digest = run_manifest_digest
    if len(digest) != _SHA256_HEX_LENGTH or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise GovernedContextUnavailable(RUN_MANIFEST_UNAVAILABLE)
    try:
        targets = targets_from_bindings(
            parse_bindings(getattr(config, "effect_callback_connectors", "[]"))
        )
        ttl = configured_ttl_seconds(config)
    except ValueError:
        raise GovernedContextUnavailable(CONFIGURATION_INVALID) from None
    return RunAuthority(
        session_factory=session_factory,
        workflow_run_id=run.workflow_run_id,
        project_id=run.project_id,
        config=config,
        lease_provider=lease_provider,
        run_manifest_digest=digest,
        requested_by=_checked_requester(run.created_by),
        approval_policy=ApprovalPolicy(
            allow_self_approval=bool(getattr(config, "approval_allow_self_approval", False))
        ),
        approval_ttl_seconds=ttl,
        targets=targets,
    )


def effect_context(
    authority: RunAuthority,
    *,
    verifiers: Mapping[str, ReceiptVerifier] | None = None,
) -> GovernedEffectContext:
    """The ``GovernedEffectContext`` of a run: an exact-approval authorization and the backends it can verify.

    ``verifiers`` maps a backend kind to its authoritative receipt verifier; the context dispatches only through those kinds
    (``dispatchable_backends``). The worker passes none today (no generic connector has a lookup by key, #1055), so every
    workflow HTTP, registered-tool or OpenAPI node is refused before it is recorded; the mechanism (a proposal, a bound request,
    a park behind it, a dispatch under the lease) is the one a verifier plugs into.
    """
    by_kind = dict(verifiers or {})
    by_connector = {
        BACKENDS[kind].connector: verifier for kind, verifier in by_kind.items() if kind in BACKENDS
    }

    def receipt_verifier(request: EffectRequest, response: Mapping[str, Any]) -> Any:
        verifier = by_connector.get(request.view.connector)
        if (
            verifier is None
        ):  # unreachable while dispatchable_backends is what was verified; never an implicit allow
            raise EffectRefused("no_authoritative_verifier")
        return verifier(request, response)

    return GovernedEffectContext(
        session_factory=authority.session_factory,
        workflow_run_id=authority.workflow_run_id,
        authorize=authority.authorize_recorded,
        receipt_verifier=receipt_verifier,
        dispatchable_backends=frozenset(by_kind),
    )


def governed_agent_nodes(
    agents: Iterable[IRAgent],
    *,
    classify: ToolClassifier,
    tool_backends: Mapping[tuple[str, str], str],
) -> frozenset[str]:
    """The agent nodes that can reach a tool the operator mapped to a backend: by registry identity, never by local name."""
    governed: set[str] = set()
    for agent in agents:
        for binding in agent.tools:
            trust = classify(binding)
            if trust.registry is not None and trust.registry in tool_backends:
                governed.add(agent.node_id)
                break
    return frozenset(governed)


def _deadline(ceilings: StepCeilings) -> str:
    moment = datetime.now(timezone.utc) + timedelta(seconds=ceilings.max_seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def step_context(
    authority: RunAuthority,
    *,
    grant_id: str,
    governed_nodes: frozenset[str],
    tool_backends: Mapping[tuple[str, str], str],
) -> GovernedStepContext:
    """The ``GovernedStepContext`` of a run: the tools the operator mapped, the operator's ceilings and the dispatch of an approval.

    ``governed_nodes`` are the agent nodes this governs (the others run as before); ``tool_backends`` is the operator's map. The
    limits a step runs under are the operator's ceilings themselves (the platform caps whatever a producer asks for at them), and
    the run's token budget is ``CALIBER_AGENT_RUN_MAX_TOKENS``.
    """
    config = authority.config
    ceilings = StepCeilings.from_config(config)

    def limits(_agent: IRAgent) -> StepLimits:
        return StepLimits(
            max_turns=ceilings.max_turns,
            max_tool_calls=ceilings.max_tool_calls,
            max_tokens=ceilings.max_tokens,
            deadline=_deadline(ceilings),
        )

    return GovernedStepContext(
        session_factory=authority.session_factory,
        workflow_run_id=authority.workflow_run_id,
        run_manifest_digest=authority.run_manifest_digest,
        grant_id=grant_id,
        classify_tool=registry_tool_classifier(authority.session_factory),
        context_manifest_digest=stored_context_manifest_digest(
            authority.session_factory, authority.workflow_run_id
        ),
        limits=limits,
        requested_by=authority.requested_by,
        approval_ttl_seconds=authority.approval_ttl_seconds,
        approval_policy=authority.approval_policy,
        tool_backends=dict(tool_backends),
        ceilings=ceilings,
        run_token_budget=config.agent_run_max_tokens,
        governed_nodes=governed_nodes,
        dispatch_approved=authority.dispatch_approved,
    )


def configured_tool_backends(config: CaliberConfig) -> Mapping[tuple[str, str], str]:
    """The operator's ``(registry name, version) -> backend`` map, or ``GovernedContextUnavailable`` when it cannot be read."""
    try:
        return parse_tool_backends(getattr(config, "agent_step_tool_backends", "[]"))
    except ValueError:
        raise GovernedContextUnavailable(CONFIGURATION_INVALID) from None


__all__ = [
    "CONFIGURATION_INVALID",
    "CONNECTOR_UNBOUND",
    "CONTEXT_RECORDER_UNAVAILABLE",
    "LEASE_PROVIDER_UNBOUND",
    "LEASE_UNAVAILABLE",
    "REQUESTER_UNAVAILABLE",
    "RUN_MANIFEST_UNAVAILABLE",
    "RUN_NOT_ADMITTED",
    "VERSION_UNVERIFIABLE",
    "GovernedContextUnavailable",
    "LeaseProvider",
    "ReceiptVerifier",
    "RunAuthority",
    "configured_tool_backends",
    "effect_context",
    "effect_evidence_hash",
    "governed_agent_nodes",
    "run_authority",
    "step_context",
]
