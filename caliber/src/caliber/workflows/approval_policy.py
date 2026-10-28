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

Exact-intent binding (B06.1, #706). :func:`create_approval_request` records a request that is
bound to one effect proposal: its full intent hash, expected resource version, the evidence the
approver is shown, the policy digest in force and an expiry. :func:`assert_distinct_actor` is the
separation-of-duties rule :func:`record_approval` applies, extracted so other approval paths
(Beta's editor/reviewer/approver checks) apply the same rule with the same message.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.contracts import validate_record
from caliber.db.models import (
    CaliberEffectProposal,
    CaliberRuntimeApprovalRequest,
    CaliberWorkflowRun,
)
from caliber.effects.records import PROPOSED
from caliber.ids import new_runtime_approval_id

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


class ApprovalPolicyError(ValueError):
    """The node's approval policy is not one the runtime can honour."""


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
    """
    if not allow_self and initiated_by and actor == initiated_by:
        raise ApprovalPolicyError(
            "the account that triggered this run cannot approve it; a second person "
            "must decide (set CALIBER_APPROVAL_ALLOW_SELF_APPROVAL=true for a "
            "single-operator install)"
        )


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


#: The one failure :func:`create_approval_request` names with a fixed code rather than a message.
PROPOSAL_NOT_PROPOSED: Final = "proposal_not_proposed"
#: What an approver may answer (ADR 0003 ``approval_request.allowed_responses``).
ALLOWED_RESPONSES: Final = ("approve", "reject", "request_changes")
#: Longest approval window: a year. A larger ``ttl_seconds`` is a mistake (and would overflow a date), not a policy.
MAX_TTL_SECONDS: Final = 365 * 24 * 60 * 60
_DIGEST: Final = re.compile(r"sha256:[0-9a-f]{64}")
#: No control characters, NUL or unpaired surrogates: the requester reaches the database as it is.
_PRINTABLE: Final = re.compile(r"[^\x00-\x1f\x7f-\x9f\ud800-\udfff]+")
_MAX_REQUESTED_BY: Final = 256


class ApprovalRequestError(ValueError):
    """An approval request cannot be created; ``code`` is stable and the message is the code or names the field only."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


def _rfc3339(moment: datetime) -> str:
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def create_approval_request(
    session: Session,
    *,
    proposal: CaliberEffectProposal,
    evidence_hash: str,
    policy_digest: str,
    requested_by: str,
    ttl_seconds: int,
    now: datetime | None = None,
) -> CaliberRuntimeApprovalRequest:
    """Ask for approval of exactly one proposal's intent, in the caller's transaction (flushes, never commits).

    The request is bound to what the proposal says *now*, read from the database rather than from the object the
    caller holds: its ``intent_hash`` (the one full digest, ``caliber.effects.records.Intent``), its expected resource
    version, the ``evidence_hash`` of what the approver is shown, the ``policy_digest`` in force and an ``expires_at`` of
    ``now + ttl_seconds`` (``now`` is injectable for a fixed clock). It is stored ``pending`` against the proposal's own
    run and node, and is valid against the ``approval_request`` v1 contract before it is stored.

    Only a ``proposed`` proposal can be asked about: any other state (already approved, dispatched, settled) raises
    :class:`ApprovalRequestError` with code and message ``proposal_not_proposed``, and a request for an unknown proposal
    ``proposal_not_found``. A malformed ``evidence_hash``/``policy_digest`` (not ``sha256:`` and 64 lowercase hex digits),
    ``requested_by`` or ``ttl_seconds`` (a positive ``int`` of at most :data:`MAX_TTL_SECONDS`) is ``bad_field``.
    Supersession of an earlier request for the same node and consumption belong to B06.2 and B06.4.
    """
    for name, value in (("evidence_hash", evidence_hash), ("policy_digest", policy_digest)):
        if not isinstance(value, str) or not _DIGEST.fullmatch(value):
            raise ApprovalRequestError(
                "bad_field", f"{name} must be 'sha256:' and 64 lowercase hex digits"
            )
    if (
        not isinstance(requested_by, str)
        or not requested_by.strip()
        or len(requested_by) > _MAX_REQUESTED_BY
        or not _PRINTABLE.fullmatch(requested_by)
    ):
        raise ApprovalRequestError(
            "bad_field",
            f"requested_by must be 1..{_MAX_REQUESTED_BY} printable characters, not blank",
        )
    if type(ttl_seconds) is not int or not 0 < ttl_seconds <= MAX_TTL_SECONDS:
        raise ApprovalRequestError(
            "bad_field", f"ttl_seconds must be an integer from 1 to {MAX_TTL_SECONDS}"
        )
    row = session.execute(
        select(
            CaliberEffectProposal.proposal_id,
            CaliberEffectProposal.state,
            CaliberEffectProposal.workflow_run_id,
            CaliberEffectProposal.node_id,
            CaliberEffectProposal.intent_hash,
            CaliberEffectProposal.effect_class,
            CaliberEffectProposal.expected_resource_version,
        ).where(CaliberEffectProposal.proposal_id == proposal.proposal_id)
    ).one_or_none()
    if row is None:
        raise ApprovalRequestError("proposal_not_found")
    if row.state != PROPOSED or proposal.state != PROPOSED:
        raise ApprovalRequestError(PROPOSAL_NOT_PROPOSED)
    project_id = session.execute(
        select(CaliberWorkflowRun.project_id).where(
            CaliberWorkflowRun.workflow_run_id == row.workflow_run_id
        )
    ).scalar_one()
    moment = datetime.now(timezone.utc) if now is None else now
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    expires = moment + timedelta(seconds=ttl_seconds)
    request_id = new_runtime_approval_id()
    validate_record(
        "approval_request",
        {
            "schema_version": "v1",
            "approval_request_id": request_id,
            "proposal_id": row.proposal_id,
            "intent_hash": row.intent_hash,
            "evidence_hash": evidence_hash,
            "policy_digest": policy_digest,
            "expected_resource_version": row.expected_resource_version,
            "risk_class": row.effect_class,
            "requested_by": requested_by,
            "expires_at": _rfc3339(expires),
            "allowed_responses": list(ALLOWED_RESPONSES),
        },
    )
    request = CaliberRuntimeApprovalRequest(
        runtime_approval_id=request_id,
        workflow_run_id=row.workflow_run_id,
        project_id=project_id,
        node_id=row.node_id,
        status="pending",
        requested_at=moment.replace(tzinfo=None),
        policy_snapshot=ApprovalPolicy().to_snapshot(),
        proposal_id=row.proposal_id,
        intent_hash=row.intent_hash,
        evidence_hash=evidence_hash,
        policy_digest=policy_digest,
        expected_resource_version=row.expected_resource_version,
        expires_at=expires.replace(tzinfo=None),
        requested_by=requested_by,
    )
    session.add(request)
    session.flush()
    return request


__all__ = [
    "ALLOWED_RESPONSES",
    "DEFAULT_REQUIRED_ROLE",
    "MAX_TTL_SECONDS",
    "PROPOSAL_NOT_PROPOSED",
    "SUPPORTED_TIMEOUT_BEHAVIORS",
    "VALID_ROLES",
    "ApprovalPolicy",
    "ApprovalPolicyError",
    "ApprovalRequestError",
    "QuorumState",
    "assert_distinct_actor",
    "create_approval_request",
    "record_approval",
    "validate_timeout_behavior",
]
