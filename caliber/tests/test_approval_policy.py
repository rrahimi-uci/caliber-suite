"""Regression tests for human-approval policy (C6).

The review's finding: the manifest and Inspector expose ``required_role``,
``approval_count``, and ``timeout_behavior``, but "decision routes require the global
operator scope, not the node's configured role, quorum, timeout, assignment, or
separation of duties", and the worker stored a hard-coded
``{"timeout_behavior": "block"}`` snapshot. Its recommendation was to honour the
controls or remove them.

These tests pin both halves of that: the three controls that are now honoured, and
the one that is now *rejected at authoring time* rather than silently ignored.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from caliber.workflows.approval_policy import (
    SUPPORTED_TIMEOUT_BEHAVIORS,
    ApprovalPolicy,
    ApprovalPolicyError,
    record_approval,
    validate_timeout_behavior,
)
from caliber.workflows.manifest import HumanApprovalNode

APPROVER = frozenset({"caliber.approver", "caliber.viewer"})
OPERATOR = frozenset({"caliber.operator", "caliber.viewer"})
ADMIN = frozenset({"caliber.admin", "caliber.approver", "caliber.operator", "caliber.viewer"})


# ---------------------------------------------------------------------------
# Authoring-time validation: remove what cannot be honoured
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("behavior", ["escalate", "auto_reject", "nonsense"])
def test_unhonourable_timeout_behaviors_are_rejected_at_authoring_time(behavior: str) -> None:
    """The review's own recommendation: remove controls the server does not honor.
    ``escalate`` has no escalation target and ``auto_reject`` has no deadline
    enforcement, so accepting either leaves a control that does nothing."""
    with pytest.raises(ApprovalPolicyError, match="not implemented"):
        validate_timeout_behavior(behavior)
    with pytest.raises(ValidationError):
        HumanApprovalNode(id="gate", type="human_approval", timeout_behavior=behavior)


def test_block_is_supported_and_is_the_default() -> None:
    assert validate_timeout_behavior("block") == "block"
    assert validate_timeout_behavior("") == "block"
    assert frozenset({"block"}) == SUPPORTED_TIMEOUT_BEHAVIORS
    assert HumanApprovalNode(id="gate", type="human_approval").timeout_behavior == "block"


def test_an_unknown_required_role_is_rejected() -> None:
    """A typo'd scope would otherwise produce an approval nobody can ever grant."""
    with pytest.raises(ValidationError):
        HumanApprovalNode(id="gate", type="human_approval", required_role="admin")
    node = HumanApprovalNode(id="gate", type="human_approval", required_role="caliber.admin")
    assert node.required_role == "caliber.admin"


# ---------------------------------------------------------------------------
# The snapshot: what the worker records
# ---------------------------------------------------------------------------


def test_the_snapshot_round_trips_the_whole_policy() -> None:
    """It was a hard-coded ``{"timeout_behavior": "block"}`` literal, so the
    configured role and quorum never reached the decision path."""
    policy = ApprovalPolicy(
        required_role="caliber.admin", approval_count=3, allow_self_approval=True
    )
    restored = ApprovalPolicy.from_snapshot(policy.to_snapshot())
    assert restored == policy


def test_an_older_partial_snapshot_resolves_to_safe_defaults() -> None:
    """An approval already pending when this shipped must remain decidable."""
    restored = ApprovalPolicy.from_snapshot({"timeout_behavior": "block"})
    assert restored.required_role == "caliber.approver"
    assert restored.approval_count == 1
    assert restored.allow_self_approval is False
    # And a junk snapshot does not raise.
    assert ApprovalPolicy.from_snapshot(None).approval_count == 1
    assert ApprovalPolicy.from_snapshot({"approval_count": "many"}).approval_count == 1
    assert ApprovalPolicy.from_snapshot({"required_role": "root"}).required_role == (
        "caliber.approver"
    )


def test_the_policy_is_read_off_the_node() -> None:
    node = HumanApprovalNode(
        id="gate", type="human_approval", required_role="caliber.admin", approval_count=2
    )
    policy = ApprovalPolicy.from_node(node)
    assert policy.required_role == "caliber.admin"
    assert policy.approval_count == 2


# ---------------------------------------------------------------------------
# required_role
# ---------------------------------------------------------------------------


def test_the_nodes_role_is_required_not_the_global_operator_scope() -> None:
    """The concrete defect: a node configured for ``caliber.admin`` was approvable by
    any operator."""
    policy = ApprovalPolicy(required_role="caliber.admin")
    with pytest.raises(ApprovalPolicyError, match="caliber.admin"):
        record_approval(
            policy=policy,
            existing_approvals=[],
            actor="@operator",
            actor_scopes=OPERATOR,
            initiated_by="@someone-else",
        )
    # An admin can.
    state = record_approval(
        policy=policy,
        existing_approvals=[],
        actor="@admin",
        actor_scopes=ADMIN,
        initiated_by="@someone-else",
    )
    assert state.satisfied is True


def test_an_approver_cannot_satisfy_an_admin_gate() -> None:
    with pytest.raises(ApprovalPolicyError):
        record_approval(
            policy=ApprovalPolicy(required_role="caliber.admin"),
            existing_approvals=[],
            actor="@approver",
            actor_scopes=APPROVER,
            initiated_by="@other",
        )


# ---------------------------------------------------------------------------
# approval_count: a quorum of DISTINCT approvers
# ---------------------------------------------------------------------------


def test_a_quorum_needs_distinct_approvers() -> None:
    """A quorum one person can satisfy by clicking twice is not a quorum."""
    policy = ApprovalPolicy(approval_count=2)
    first = record_approval(
        policy=policy,
        existing_approvals=[],
        actor="@alice",
        actor_scopes=APPROVER,
        initiated_by="@bob",
    )
    assert first.satisfied is False
    assert first.remaining == 1

    with pytest.raises(ApprovalPolicyError, match="already approved"):
        record_approval(
            policy=policy,
            existing_approvals=list(first.approvals),
            actor="@alice",
            actor_scopes=APPROVER,
            initiated_by="@bob",
        )

    second = record_approval(
        policy=policy,
        existing_approvals=list(first.approvals),
        actor="@carol",
        actor_scopes=APPROVER,
        initiated_by="@bob",
    )
    assert second.satisfied is True
    assert second.approvals == ("@alice", "@carol")


def test_a_quorum_of_one_is_satisfied_immediately() -> None:
    state = record_approval(
        policy=ApprovalPolicy(),
        existing_approvals=[],
        actor="@alice",
        actor_scopes=APPROVER,
        initiated_by="@bob",
    )
    assert state.satisfied is True
    assert state.remaining == 0


# ---------------------------------------------------------------------------
# Separation of duties
# ---------------------------------------------------------------------------


def test_the_run_initiator_cannot_approve_their_own_run() -> None:
    """The one SoD rule that is meaningful without a role hierarchy."""
    with pytest.raises(ApprovalPolicyError, match="cannot approve"):
        record_approval(
            policy=ApprovalPolicy(),
            existing_approvals=[],
            actor="@alice",
            actor_scopes=APPROVER,
            initiated_by="@alice",
        )


def test_self_approval_can_be_enabled_for_a_single_operator_install() -> None:
    """Without the escape hatch, a one-person deployment deadlocks on its own gates."""
    state = record_approval(
        policy=ApprovalPolicy(allow_self_approval=True),
        existing_approvals=[],
        actor="@alice",
        actor_scopes=APPROVER,
        initiated_by="@alice",
    )
    assert state.satisfied is True


def test_an_unknown_initiator_does_not_block_approval() -> None:
    """A run with no recorded initiator must not become undecidable."""
    state = record_approval(
        policy=ApprovalPolicy(),
        existing_approvals=[],
        actor="@alice",
        actor_scopes=APPROVER,
        initiated_by=None,
    )
    assert state.satisfied is True


def test_the_role_check_precedes_the_self_approval_check() -> None:
    """The first message an operator sees should be the most fundamental problem."""
    with pytest.raises(ApprovalPolicyError, match="scope"):
        record_approval(
            policy=ApprovalPolicy(required_role="caliber.admin"),
            existing_approvals=[],
            actor="@alice",
            actor_scopes=OPERATOR,
            initiated_by="@alice",
        )
