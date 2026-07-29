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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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
    if not policy.allow_self_approval and initiated_by and actor == initiated_by:
        raise ApprovalPolicyError(
            "the account that triggered this run cannot approve it; a second person "
            "must decide (set CALIBER_APPROVAL_ALLOW_SELF_APPROVAL=true for a "
            "single-operator install)"
        )
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
    "SUPPORTED_TIMEOUT_BEHAVIORS",
    "VALID_ROLES",
    "ApprovalPolicy",
    "ApprovalPolicyError",
    "QuorumState",
    "record_approval",
    "validate_timeout_behavior",
]
