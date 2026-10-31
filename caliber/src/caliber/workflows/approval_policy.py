"""Human-approval policy — making the represented controls real (C6).

The review's finding: the manifest and Inspector expose ``required_role``,
``approval_count``, and ``timeout_behavior``, but at runtime "decision routes require
the global operator scope, not the node's configured role, quorum, timeout,
assignment, or separation of duties", and the worker created one request with a
hard-coded ``{"timeout_behavior": "block"}`` snapshot. Its recommendation was
explicit: either honour the controls or remove them.

This honours the three that can be honoured, and removes the one that cannot.

``required_role``
    Enforced. The decision endpoint requires the *node's* configured scope, not the
    global operator scope. A node asking for ``caliber.admin`` now means it.

``approval_count``
    Enforced as a quorum of **distinct** approvers. Distinctness is the whole
    property — a quorum one person can satisfy by clicking twice is not a quorum.

separation of duties
    Enforced by default: whoever triggered the run cannot approve it. This is the
    one SoD rule that needs no role hierarchy to be meaningful, and it is
    configurable because a single-operator install would otherwise deadlock.

``timeout_behavior``
    ``block`` is honoured (a pending approval blocks indefinitely, which is what it
    says). ``escalate`` is **rejected at manifest validation**: there is no
    escalation target in the product, so accepting it would leave a control that
    silently does nothing — the exact defect being fixed. ``auto_reject`` is also
    rejected for now, because nothing enforces a deadline; it is a missing feature,
    not a policy the runtime can pretend to apply.

Every rule is evaluated here rather than in the route so it is testable without a
request, and so the worker's snapshot and the route's enforcement read the same
definitions.

Whether a request can still be decided at all is :func:`approval_refusal` (B06.2, #712): superseded by a newer request, or expired.
It is pure (the caller reads the row and passes the clock), so the decision route and the recheck at commit apply one rule.

Separation of duties is :func:`assert_distinct_actor` (B06.1, #706), extracted from
:func:`record_approval` so every approval path applies one rule with one message. The request
that binds an approval to one effect proposal is ``caliber.workflows.approval_requests``. The
pure policy helpers remain usable without a request, while the commit validator below imports
the persistence and run-authority contracts needed for the actual dispatch boundary.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.db.models import (
    CaliberEffectProposal,
    CaliberProject,
    CaliberProjectMember,
    CaliberRuntimeApprovalRequest,
    CaliberWorkflowRun,
)
from caliber.effects.records import ProposalError, read_proposal
from caliber.observability.span_attributes import GovernedSpan, governed_span
from caliber.policy.grants import AUTHORITY_UNAVAILABLE
from caliber.resource_access import (
    ACTION_APPROVE,
    OWNER_ROLE_REQUIRED_SCOPES,
    PROJECT_ACTIONS,
    ROLE_EDITOR,
    ROLE_OWNER,
)
from caliber.run_manifest import canonical_bytes
from caliber.workflows.run_identity import (
    IdentityDenied,
    RunIdentity,
    identity_for_run,
)

#: The default role an approval node requires when the manifest says nothing.
DEFAULT_REQUIRED_ROLE = "caliber.approver"

#: ``timeout_behavior`` values the runtime can actually honour. ``escalate`` and
#: ``auto_reject`` are deliberately absent — see the module docstring.
SUPPORTED_TIMEOUT_BEHAVIORS: frozenset[str] = frozenset({"block"})

#: Recognised scope names, so a typo in a manifest is a validation error rather than
#: an approval that can never be granted.
VALID_ROLES: frozenset[str] = frozenset(
    {"caliber.viewer", "caliber.operator", "caliber.approver", "caliber.admin"}
)


#: Stable refusal codes for a request that can no longer be decided or consumed (B06.2, #712). The decision route returns them as
#: the ``reason_code`` of a 409 and the commit-time recheck (B06.3) reuses them. They are the vocabulary of
#: ``docs/architecture/approval-ux-contract.md`` (``expired``, ``superseded``), which replaces the ``approval_expired`` /
#: ``approval_superseded`` named in the plan.
REFUSAL_EXPIRED: Final = "expired"
REFUSAL_SUPERSEDED: Final = "superseded"
REFUSAL_INTENT_MISMATCH: Final = "intent_mismatch"
REFUSAL_ROLE_REVOKED: Final = "role_revoked"
REFUSAL_STALE_VERSION: Final = "stale_version"
REFUSAL_SELF_APPROVAL: Final = "self_approval"
REFUSAL_NOT_APPROVED: Final = "not_approved"
REFUSAL_ALREADY_CONSUMED: Final = "already_consumed"
REFUSAL_NOT_FOUND: Final = "not_found"
REFUSAL_AUTHORITY_UNAVAILABLE: Final = "authority_unavailable"
#: ``status`` of a request that a newer request for the same node replaced.
STATUS_SUPERSEDED: Final = "superseded"


class ApprovalPolicyError(ValueError):
    """The node's approval policy is not one the runtime can honour."""


class ApprovalCommitRefusedError(PermissionError):
    """A bound approval cannot authorize a dispatch in the current transaction.

    ``code`` is a stable, non-sensitive refusal vocabulary for the dispatcher and
    its recovery/event paths.  The exception deliberately does not include ids in
    its default message: callers can log the surrounding transaction context under
    their normal access controls without turning an API refusal into an identifier
    oracle.
    """

    def __init__(
        self, code: str, message: str | None = None, *, reasons: tuple[str, ...] = ()
    ) -> None:
        super().__init__(message or code)
        self.code = code
        self.reasons = reasons


@dataclass(frozen=True)
class ApprovalCommit:
    """The exact, rechecked approval facts a dispatcher may carry into its claim.

    The rows remain owned by the caller's transaction and are not consumed here.
    A later dispatcher step must consume the same locked request and proposal in
    that transaction; this value only makes the verified identity and pins
    explicit, so it cannot accidentally substitute the approver for the run's
    original subject.
    """

    run_id: str
    approval_id: str
    proposal_id: str
    approver_id: str
    intent_hash: str
    evidence_hash: str
    policy_digest: str
    expected_resource_version: str | None
    grant_id: str
    decision_id: str
    run_identity: RunIdentity


# The shorter name reads naturally at call sites and remains the public contract;
# the concrete class uses the repository's Error suffix convention.
ApprovalCommitRefused = ApprovalCommitRefusedError


@dataclass(frozen=True)
class ApprovalPolicy:
    """The policy a specific approval node asks for."""

    required_role: str = DEFAULT_REQUIRED_ROLE
    approval_count: int = 1
    timeout_behavior: str = "block"
    #: Whether the run's initiator may approve their own run.
    allow_self_approval: bool = False

    def to_snapshot(self) -> dict[str, Any]:
        """The policy as stored on the approval request.

        Snapshotted at request time so a later manifest edit cannot retroactively
        change what a pending approval requires — the review flagged the equivalent
        race for MCP tool policy.
        """
        return {
            "required_role": self.required_role,
            "approval_count": self.approval_count,
            "timeout_behavior": self.timeout_behavior,
            "allow_self_approval": self.allow_self_approval,
        }

    @classmethod
    def from_snapshot(cls, snapshot: Any) -> ApprovalPolicy:
        """Rebuild from a stored snapshot, tolerating older/partial rows.

        A pre-existing row carries only ``{"timeout_behavior": "block"}``; it resolves
        to the defaults rather than failing, so an approval that was already pending
        when this shipped remains decidable.
        """
        if not isinstance(snapshot, dict):
            return cls()
        role = str(snapshot.get("required_role") or DEFAULT_REQUIRED_ROLE)
        raw_count = snapshot.get("approval_count", 1)
        try:
            count = max(1, int(raw_count))
        except (TypeError, ValueError):
            count = 1
        return cls(
            required_role=role if role in VALID_ROLES else DEFAULT_REQUIRED_ROLE,
            approval_count=count,
            timeout_behavior=str(snapshot.get("timeout_behavior") or "block"),
            allow_self_approval=bool(snapshot.get("allow_self_approval", False)),
        )

    @classmethod
    def from_node(cls, node: Any, *, allow_self_approval: bool = False) -> ApprovalPolicy:
        """Read the policy off an ``IRHumanApproval`` node."""
        role = str(getattr(node, "required_role", DEFAULT_REQUIRED_ROLE) or DEFAULT_REQUIRED_ROLE)
        try:
            count = max(1, int(getattr(node, "approval_count", 1) or 1))
        except (TypeError, ValueError):
            count = 1
        return cls(
            required_role=role if role in VALID_ROLES else DEFAULT_REQUIRED_ROLE,
            approval_count=count,
            timeout_behavior=str(getattr(node, "timeout_behavior", "block") or "block"),
            allow_self_approval=allow_self_approval,
        )


def policy_digest(policy: ApprovalPolicy) -> str:
    """Digest the normalized policy snapshot stored with a bound request."""
    return "sha256:" + hashlib.sha256(canonical_bytes(policy.to_snapshot())).hexdigest()


def validate_timeout_behavior(value: str) -> str:
    """Reject a ``timeout_behavior`` the runtime cannot honour.

    Called from manifest validation so an unsupported value fails at authoring time.
    Accepting ``escalate`` would ship a control that silently does nothing, which is
    exactly what this fix removes.
    """
    resolved = (value or "block").strip() or "block"
    if resolved not in SUPPORTED_TIMEOUT_BEHAVIORS:
        raise ApprovalPolicyError(
            f"timeout_behavior {resolved!r} is not implemented; CALIBER honours "
            f"{sorted(SUPPORTED_TIMEOUT_BEHAVIORS)}. 'escalate' has no escalation "
            "target and 'auto_reject' has no deadline enforcement, so accepting either "
            "would leave a control that does nothing."
        )
    return resolved


@dataclass(frozen=True)
class QuorumState:
    """Progress toward a node's approval quorum."""

    approvals: tuple[str, ...] = ()
    required: int = 1

    @property
    def satisfied(self) -> bool:
        return len(self.approvals) >= self.required

    @property
    def remaining(self) -> int:
        return max(0, self.required - len(self.approvals))


def assert_distinct_actor(actor: str, initiated_by: str | None, *, allow_self: bool) -> None:
    """Refuse a decision by the account that initiated what is being decided (separation of duties).

    ``initiated_by`` is whoever triggered the run (or authored the change); an unknown initiator (``None`` or empty, a
    legacy run) cannot be compared and passes. With ``allow_self`` (a single-operator install) nothing is refused.
    Raises :class:`ApprovalPolicyError` with the message the decision route has always returned.

    Not shared with :func:`caliber.actor_provenance.require_distinct_actors` or the assistant service's reviewer and
    release identity checks (``assistant/service.py``): those take :class:`~caliber.actor_provenance.ActorProvenance`
    objects or return their own message, raise a different error type (``DistinctActorError``) or none, have no
    ``allow_self`` escape and compare against an author and a reviewer, so delegating would change behaviour their callers
    and tests pin.
    """
    if not allow_self and initiated_by and actor == initiated_by:
        raise ApprovalPolicyError(
            "the account that triggered this run cannot approve it; a second person "
            "must decide (set CALIBER_APPROVAL_ALLOW_SELF_APPROVAL=true for a "
            "single-operator install)"
        )


def _utc(moment: datetime) -> datetime:
    """``moment`` as an aware UTC datetime; a naive one (how every timestamp is stored here) is already UTC."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def approval_refusal(
    *,
    status: str | None,
    superseded_by: str | None,
    expires_at: datetime | None,
    now: datetime,
) -> str | None:
    """Why a request can no longer be decided or consumed: :data:`REFUSAL_SUPERSEDED`, :data:`REFUSAL_EXPIRED` or ``None``.

    Pure: the caller reads the row and passes the clock, so every path that acts on a request (the decision route today, the
    recheck at commit in B06.3) applies one rule with one set of codes, and a test can fix the clock.

    * **Superseded** (checked first, because a replaced request is gone whatever its clock says): ``status`` is
      ``superseded`` or ``superseded_by`` is set.
    * **Expired**: ``now`` is at or after ``expires_at``. The window is half-open, ``[requested_at, expires_at)``: at the
      very instant it ends the request is already refused, so a request is never usable one tick longer than its ttl.
      ``expires_at`` is stored naive and read as UTC; an aware ``now`` is converted. A request with no ``expires_at`` (a
      legacy, node-level one) never expires.

    The clock is the application's (``datetime.now(timezone.utc)``): the same one that wrote ``expires_at``, so a request is
    never judged by two clocks. Nodes that share a database need synchronized clocks, as for every other deadline here.
    """
    if status == STATUS_SUPERSEDED or superseded_by:
        return REFUSAL_SUPERSEDED
    if expires_at is not None and _utc(now) >= _utc(expires_at):
        return REFUSAL_EXPIRED
    return None


_CURRENT_RESOURCE_VERSION_MISSING: Final = object()
_DIGEST_LENGTH: Final = len("sha256:") + 64


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _DIGEST_LENGTH
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def approval_valid_at_commit(
    session: Session,
    *,
    approval_id: str,
    proposal_id: str,
    approver_scopes: frozenset[str] | set[str],
    current_resource_version: str | object | None = _CURRENT_RESOURCE_VERSION_MISSING,
    evidence_hash: str | None = None,
    now: datetime | None = None,
) -> ApprovalCommit:
    """Recheck a bound approval immediately before an effect claim.

    This is the producer-owned B06.3 contract consumed by B04.1's dispatcher.
    It does not decide or consume the request and it does not call a connector.
    The caller must keep the returned rows in the same transaction while it
    atomically consumes the approval and claims dispatch.

    Lock order is ``run -> proposal -> approval``.  The run lock serializes
    approval/request changes for one run; the proposal lock makes the canonical
    intent and state read stable; the approval lock makes expiry, supersession and
    consumption checks apply to the row that will be consumed.  PostgreSQL uses
    these locks for the claim transaction; SQLite remains development-only and
    serializes its write transaction.

    ``current_resource_version`` is required for a versioned target.  Omitting it
    is therefore a fail-closed ``stale_version`` refusal, not permission to dispatch
    without checking the downstream.  An unversioned proposal has a ``None``
    expected version and may omit it.  When ``evidence_hash`` is supplied it is the
    current evidence digest produced by the dispatcher and must equal the frozen
    digest shown to the approver; when omitted, the stored digest is still checked
    for shape and remains the evidence pin carried into the claim.

    The run authority check deliberately delegates to :func:`identity_for_run`.
    That function loads the manifest's exact original subject, workload, purpose,
    run id and grant id before calling :func:`decide`; the approving principal is
    checked separately from the request row and is never substituted into that
    grant.

    The check runs inside an ``effect.approval.commit_check`` span (B11.3a) that names the rows the
    check has actually bound: ``caliber.proposal_id`` once the locked proposal is read,
    ``caliber.approval_id`` once the request is bound to that proposal's exact intent, and
    ``caliber.decision_id`` from the run's current policy decision on success.  A refusal adds its
    stable ``caliber.refusal_code`` (never a message, scope or id the caller supplied).
    """
    with governed_span("effect.approval.commit_check", span_type="CHAIN") as span:
        return _recheck_approval(
            session,
            approval_id=approval_id,
            proposal_id=proposal_id,
            approver_scopes=approver_scopes,
            current_resource_version=current_resource_version,
            evidence_hash=evidence_hash,
            now=now,
            span=span,
        )


def _recheck_approval(  # noqa: PLR0912, PLR0915 - one fail-closed commit gate owns all pins
    session: Session,
    *,
    approval_id: str,
    proposal_id: str,
    approver_scopes: frozenset[str] | set[str],
    current_resource_version: str | object | None = _CURRENT_RESOURCE_VERSION_MISSING,
    evidence_hash: str | None = None,
    now: datetime | None = None,
    span: GovernedSpan,
) -> ApprovalCommit:
    """The checks of :func:`approval_valid_at_commit`; ``span`` receives only ids read from rows."""
    moment = now if now is not None else datetime.now(timezone.utc)
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise ApprovalCommitRefused(REFUSAL_AUTHORITY_UNAVAILABLE)
    moment = moment.astimezone(timezone.utc)

    # These are deliberately separate row reads so the lock order is visible in
    # code and can be kept aligned with the future dispatcher claim.
    run = session.execute(
        select(CaliberWorkflowRun)
        .where(CaliberWorkflowRun.workflow_run_id == _proposal_run_id(session, proposal_id))
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if run is None:
        raise ApprovalCommitRefused(REFUSAL_NOT_FOUND)

    proposal = session.execute(
        select(CaliberEffectProposal)
        .where(
            CaliberEffectProposal.proposal_id == proposal_id,
            CaliberEffectProposal.workflow_run_id == run.workflow_run_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if proposal is None:
        raise ApprovalCommitRefused(REFUSAL_NOT_FOUND)
    span.bind(proposal_id=proposal.proposal_id)

    approval = session.execute(
        select(CaliberRuntimeApprovalRequest)
        .where(
            CaliberRuntimeApprovalRequest.runtime_approval_id == approval_id,
            CaliberRuntimeApprovalRequest.workflow_run_id == run.workflow_run_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if approval is None:
        raise ApprovalCommitRefused(REFUSAL_NOT_FOUND)

    # A consumed row is terminal even when its old expiry has since elapsed.  The
    # caller can safely retry only by reading the already-claimed proposal state.
    if approval.consumed_at is not None:
        raise ApprovalCommitRefused(REFUSAL_ALREADY_CONSUMED)
    refusal = approval_refusal(
        status=approval.status,
        superseded_by=approval.superseded_by,
        expires_at=approval.expires_at,
        now=moment,
    )
    if refusal is not None:
        raise ApprovalCommitRefused(refusal)
    if approval.status != "approved" or not approval.decided_by:
        raise ApprovalCommitRefused(REFUSAL_NOT_APPROVED)

    # The binding is checked before any authority decision is recorded.  The
    # canonical reader rebuilds intent-v1 from all producer-owned fields,
    # including connector version and effect class, so a changed downstream
    # identity cannot reuse an old approval digest.
    if (
        approval.proposal_id != proposal.proposal_id
        or approval.workflow_run_id != proposal.workflow_run_id
        or approval.project_id != run.project_id
        or not _is_digest(approval.intent_hash)
        or not _is_digest(approval.evidence_hash)
        or not _is_digest(approval.policy_digest)
        or approval.intent_hash != proposal.intent_hash
        or approval.expected_resource_version != proposal.expected_resource_version
        or not isinstance(approval.policy_snapshot, dict)
    ):
        raise ApprovalCommitRefused(REFUSAL_INTENT_MISMATCH)
    try:
        stored_proposal = read_proposal(session, proposal.proposal_id)
    except (KeyError, ProposalError):
        raise ApprovalCommitRefused(REFUSAL_INTENT_MISMATCH) from None
    if stored_proposal["intent_hash"] != approval.intent_hash:
        raise ApprovalCommitRefused(REFUSAL_INTENT_MISMATCH)

    stored_policy = ApprovalPolicy.from_snapshot(approval.policy_snapshot)
    if policy_digest(stored_policy) != approval.policy_digest:
        raise ApprovalCommitRefused(REFUSAL_INTENT_MISMATCH)
    if evidence_hash is not None and evidence_hash != approval.evidence_hash:
        raise ApprovalCommitRefused(REFUSAL_INTENT_MISMATCH)
    # SQLAlchemy keeps these columns nullable for legacy rows; the binding checks
    # above establish the stronger type for this path.
    assert isinstance(approval.intent_hash, str)
    assert isinstance(approval.evidence_hash, str)
    assert isinstance(approval.policy_digest, str)
    # Every pin that binds this request to this proposal's exact intent, policy and evidence has held.
    span.bind(approval_id=approval.runtime_approval_id)

    expected_version = proposal.expected_resource_version
    if current_resource_version is _CURRENT_RESOURCE_VERSION_MISSING:
        if expected_version is not None:
            raise ApprovalCommitRefused(REFUSAL_STALE_VERSION)
    elif current_resource_version != expected_version:
        raise ApprovalCommitRefused(REFUSAL_STALE_VERSION)

    # The exact approving principal is authorized independently.  In particular,
    # an approver's membership/scopes never flow into identity_for_run's grant
    # recheck, which must continue to use the original run subject.
    scopes = frozenset(approver_scopes)
    if stored_policy.required_role not in scopes:
        raise ApprovalCommitRefused(REFUSAL_ROLE_REVOKED)
    try:
        # Neither the run's initiator nor whoever asked for this approval may be the approver; the decision route
        # enforced both when it recorded the decision, and the row is checked again here because the claim must not
        # rely on a writer that ran earlier.
        for initiator in (run.created_by, approval.requested_by):
            assert_distinct_actor(
                approval.decided_by,
                initiator,
                allow_self=stored_policy.allow_self_approval,
            )
    except ApprovalPolicyError:
        raise ApprovalCommitRefused(REFUSAL_SELF_APPROVAL) from None

    if run.project_id is not None:
        project = session.execute(
            select(CaliberProject)
            .where(CaliberProject.project_id == run.project_id)
            .with_for_update(read=True)
        ).scalar_one_or_none()
        member = session.execute(
            select(CaliberProjectMember)
            .where(
                CaliberProjectMember.project_id == run.project_id,
                CaliberProjectMember.user_id == approval.decided_by,
            )
            .with_for_update(read=True)
        ).scalar_one_or_none()
        role = None
        if project is not None and project.owner == approval.decided_by:
            role = ROLE_OWNER
            if not scopes >= OWNER_ROLE_REQUIRED_SCOPES:
                role = ROLE_EDITOR
        elif member is not None and member.status == "active" and member.deactivated_at is None:
            role = member.role
        if role not in PROJECT_ACTIONS[ACTION_APPROVE]:
            raise ApprovalCommitRefused(REFUSAL_ROLE_REVOKED)

    try:
        run_identity = identity_for_run(session, run, now=moment)
    except IdentityDenied as denied:
        if denied.code == AUTHORITY_UNAVAILABLE:
            raise ApprovalCommitRefused(
                REFUSAL_AUTHORITY_UNAVAILABLE, reasons=denied.reasons
            ) from None
        raise ApprovalCommitRefused(REFUSAL_ROLE_REVOKED, reasons=denied.reasons) from None
    if run_identity is None:
        raise ApprovalCommitRefused(REFUSAL_ROLE_REVOKED)

    span.bind(decision_id=run_identity.decision.decision_id)
    return ApprovalCommit(
        run_id=run.workflow_run_id,
        approval_id=approval.runtime_approval_id,
        proposal_id=proposal.proposal_id,
        approver_id=approval.decided_by,
        intent_hash=approval.intent_hash,
        evidence_hash=approval.evidence_hash,
        policy_digest=approval.policy_digest,
        expected_resource_version=approval.expected_resource_version,
        grant_id=run_identity.grant_id,
        decision_id=run_identity.decision.decision_id,
        run_identity=run_identity,
    )


def _proposal_run_id(session: Session, proposal_id: str) -> str:
    """Resolve a proposal's run before taking the run lock.

    Proposal identity is immutable.  Keeping this tiny lookup separate makes the
    lock order in :func:`approval_valid_at_commit` explicit while still refusing
    an unknown proposal without inventing a run id.
    """
    run_id = session.execute(
        select(CaliberEffectProposal.workflow_run_id).where(
            CaliberEffectProposal.proposal_id == proposal_id
        )
    ).scalar_one_or_none()
    return str(run_id or "")


def record_approval(
    *,
    policy: ApprovalPolicy,
    existing_approvals: list[str] | tuple[str, ...] | None,
    actor: str,
    actor_scopes: frozenset[str] | set[str],
    initiated_by: str | None,
) -> QuorumState:
    """Validate one approval decision and return the resulting quorum state.

    Raises :class:`ApprovalPolicyError` when the decision is not permitted. Every
    rule is a separate check with its own message, because "you may not approve this"
    without a reason is unusable.
    """
    if policy.required_role not in set(actor_scopes):
        raise ApprovalPolicyError(
            f"this approval requires the {policy.required_role!r} scope; "
            f"you hold {sorted(actor_scopes)}"
        )
    assert_distinct_actor(actor, initiated_by, allow_self=policy.allow_self_approval)
    approvals = list(existing_approvals or [])
    if actor in approvals:
        raise ApprovalPolicyError(
            f"{actor} has already approved this step; a quorum of "
            f"{policy.approval_count} requires distinct approvers"
        )
    approvals.append(actor)
    return QuorumState(approvals=tuple(approvals), required=policy.approval_count)


__all__ = [
    "DEFAULT_REQUIRED_ROLE",
    "REFUSAL_ALREADY_CONSUMED",
    "REFUSAL_AUTHORITY_UNAVAILABLE",
    "REFUSAL_EXPIRED",
    "REFUSAL_INTENT_MISMATCH",
    "REFUSAL_NOT_APPROVED",
    "REFUSAL_NOT_FOUND",
    "REFUSAL_ROLE_REVOKED",
    "REFUSAL_SELF_APPROVAL",
    "REFUSAL_STALE_VERSION",
    "REFUSAL_SUPERSEDED",
    "STATUS_SUPERSEDED",
    "SUPPORTED_TIMEOUT_BEHAVIORS",
    "VALID_ROLES",
    "ApprovalCommit",
    "ApprovalCommitRefused",
    "ApprovalCommitRefusedError",
    "ApprovalPolicy",
    "ApprovalPolicyError",
    "QuorumState",
    "approval_refusal",
    "approval_valid_at_commit",
    "assert_distinct_actor",
    "policy_digest",
    "record_approval",
    "validate_timeout_behavior",
]
