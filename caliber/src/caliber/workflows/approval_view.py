"""The approver's view of one approval request: what is being decided and what the caller may do (B06.5, #743).

The console's approval panel (``docs/architecture/approval-ux-contract.md``) reads one record, built here from rows the
decision routes already use, so it shows the same facts the routes enforce:

* **The pins.** For a request bound to an effect proposal (B06.1, #706): the proposal id, the exact ``intent_hash``, the
  evidence and policy digests, the expected resource version, the expiry, the requester, and ``superseded_by`` and
  ``consumed_at`` when they are set.
* **The effect.** The proposal's connector, effect class, target resource and state, and the *shape* of its arguments: the
  top-level names (only when they are plain identifiers) and the kind of each value. **No argument value is ever returned**:
  arguments are the one place a secret or personal datum can sit, the platform's other operator views never load them
  (``caliber.effects.operator_view``), and the ``intent_hash`` already binds every value. Showing values needs a
  connector-owned preview that redacts, which does not exist yet; this module does not guess at one.
* **The state.** :func:`approval_state` applies the rules that make a request final or not for this run's step (consumed,
  superseded, expired, rejected, run not waiting, another node's request, already decided; see its docstring for why that
  order), so the panel can say *why* it is read-only before anyone presses a button. The decision route
  stays the authority: the view is advice that the route re-derives under its locks.
* **What the caller may do.** :func:`caller_abilities` repeats the decision route's authority checks for the authenticated
  principal (the deployment switches the routes check first, the project ``approve`` action, the node's ``required_role``
  scope, the requester and run-initiator rule, one approval per person) and names the first one that fails, so the console disables a button and says why without
  re-implementing any rule. It reads, never writes.

Nothing here locks, decides or consumes. The functions take the rows the route authorized.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.db.models import (
    CaliberEffectProposal,
    CaliberRuntimeApprovalRequest,
    CaliberWorkflowRun,
)
from caliber.workflows.approval_policy import (
    REFUSAL_ALREADY_CONSUMED,
    ApprovalPolicy,
    ApprovalPolicyError,
    approval_refusal,
    assert_distinct_actor,
)
from caliber.workflows.run_state import RUN_STATUS_WAITING_APPROVAL

#: The decisions the decision routes accept today. ``request_changes`` (ADR 0003 ``allowed_responses``) has no route yet, so it
#: is not offered: the panel renders exactly this list.
ALLOWED_RESPONSES: Final = ("approve", "reject")

STATE_OPEN: Final = "open"
STATE_ALREADY_DECIDED: Final = "already_decided"
STATE_REJECTED: Final = "rejected"
STATE_NOT_CURRENT: Final = "approval_not_current"
STATE_RUN_NOT_WAITING: Final = "run_not_waiting"

#: Why the caller may not make a decision, beyond the request's own state. The first three are the decision route's refusals
#: (a project role, the node's scope, the requester or run initiator, a second approval by the same person).
ABILITY_PROJECT_ROLE: Final = "project_role_required"
ABILITY_SCOPE: Final = "insufficient_scope"
ABILITY_SELF_APPROVAL: Final = "self_approval"
ABILITY_DUPLICATE: Final = "duplicate_approver"
#: The deployment switches the decision routes check before anything else (``_ensure_queue_enabled``,
#: ``_ensure_runtime_approvals_enabled``, and for an approval ``_ensure_checkpointing_enabled``): a ``409`` with no code.
ABILITY_QUEUE_DISABLED: Final = "queue_disabled"
ABILITY_APPROVALS_DISABLED: Final = "approvals_disabled"
ABILITY_CHECKPOINTING_DISABLED: Final = "checkpointing_disabled"

#: Most top-level arguments listed, and the shape a listed name must have. A name that is not a plain identifier (or is too
#: long) is listed without its name: a key can be attacker-supplied text, and a name is shown only when it is a field name.
MAX_ARG_FIELDS: Final = 50
_FIELD_NAME: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
#: Most approvers listed and the longest principal listed (the stored requester is capped at 256 too).
_MAX_APPROVERS: Final = 100
_MAX_PRINCIPAL: Final = 256


def _kind(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):  # before int: ``True`` is an int
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def args_shape(args: object) -> tuple[list[dict[str, Any]], int, bool]:
    """``(fields, total, truncated)`` for a proposal's arguments: top-level names and value kinds, never a value."""
    if not isinstance(args, dict):
        return [], 0, False
    keys = sorted(args, key=str)
    fields = [
        {
            "name": key if isinstance(key, str) and _FIELD_NAME.fullmatch(key) else None,
            "kind": _kind(args[key]),
        }
        for key in keys[:MAX_ARG_FIELDS]
    ]
    return fields, len(keys), len(keys) > MAX_ARG_FIELDS


def load_proposal_view(
    session: Session, approval: CaliberRuntimeApprovalRequest
) -> dict[str, Any] | None:
    """The proposal block of a bound request, read for this request's own run only; ``None`` when unbound or not found.

    Named columns only, so the intent bytes are never loaded; the arguments are loaded to take their shape and dropped.
    """
    if approval.proposal_id is None:
        return None
    row = session.execute(
        select(
            CaliberEffectProposal.proposal_id,
            CaliberEffectProposal.connector,
            CaliberEffectProposal.connector_version,
            CaliberEffectProposal.effect_class,
            CaliberEffectProposal.resource,
            CaliberEffectProposal.state,
            CaliberEffectProposal.args,
        )
        .where(CaliberEffectProposal.proposal_id == approval.proposal_id)
        .where(CaliberEffectProposal.workflow_run_id == approval.workflow_run_id)
    ).one_or_none()
    if row is None:
        return None
    fields, total, truncated = args_shape(row.args)
    return {
        "proposal_id": row.proposal_id,
        "connector": row.connector,
        "connector_version": row.connector_version,
        "effect_class": row.effect_class,
        "resource": row.resource,
        "state": row.state,
        "args_fields": fields,
        "args_field_count": total,
        "args_truncated": truncated,
    }


def binding_view(
    approval: CaliberRuntimeApprovalRequest, *, show_requester: bool
) -> dict[str, Any] | None:
    """The exact-intent pins of a bound request, ``None`` for a legacy node-level request.

    ``requested_by`` names a person, so it is shown only when ``show_requester`` (the caller may decide the request).
    """
    if approval.proposal_id is None:
        return None
    return {
        "proposal_id": approval.proposal_id,
        "intent_hash": approval.intent_hash,
        "evidence_hash": approval.evidence_hash,
        "policy_digest": approval.policy_digest,
        "expected_resource_version": approval.expected_resource_version,
        "expires_at": approval.expires_at,
        "requested_by": approval.requested_by if show_requester else None,
        "superseded_by": approval.superseded_by,
        "consumed_at": approval.consumed_at,
    }


def approvers_of(approval: CaliberRuntimeApprovalRequest) -> list[str]:
    """The distinct approvers recorded so far, from the policy snapshot the decision route writes, in order."""
    raw = (approval.policy_snapshot or {}).get("approvals")
    if not isinstance(raw, list):
        return []
    seen: list[str] = []
    for item in raw:
        if isinstance(item, str) and item and item not in seen:
            seen.append(item[:_MAX_PRINCIPAL])
        if len(seen) >= _MAX_APPROVERS:
            break
    return seen


def approval_state(
    approval: CaliberRuntimeApprovalRequest, *, run: CaliberWorkflowRun, now: datetime
) -> dict[str, Any]:
    """Whether the request can be decided now.

    The order follows what is *true* of the request and run, most final first: ``already_consumed``; ``superseded`` and
    ``expired`` (``approval_refusal``); ``rejected`` (a rejection fails the run and clears its current node, so it has to be
    named before the run's state is judged); a run that is no longer waiting for approval (``run_not_waiting``: cancelled,
    failed, finished or resumed, which also clears its current node); then, for a bound request, a node other than the one the
    run waits at (``approval_not_current``); then a request that already holds a decision and waits for dispatch
    (``already_decided``). The decision route judges the run's status before it reads the request, so a run that is not
    waiting is never reported as a request for another node. A consumed request stays ``already_consumed`` after its expiry
    passes, as in the route.
    """
    code = STATE_OPEN
    if approval.consumed_at is not None:
        code = REFUSAL_ALREADY_CONSUMED
    else:
        refusal = approval_refusal(
            status=approval.status,
            superseded_by=approval.superseded_by,
            expires_at=approval.expires_at,
            now=now,
        )
        if refusal is not None:
            code = refusal
        elif approval.status not in ("pending", "approved"):
            code = STATE_REJECTED
        elif run.status != RUN_STATUS_WAITING_APPROVAL:
            code = STATE_RUN_NOT_WAITING
        elif approval.proposal_id is not None and approval.node_id != run.current_node_id:
            code = STATE_NOT_CURRENT
        elif approval.status == "approved":
            code = STATE_ALREADY_DECIDED
    return {
        "code": code,
        "superseded_by": approval.superseded_by if code == "superseded" else None,
    }


def caller_abilities(
    approval: CaliberRuntimeApprovalRequest,
    *,
    run: CaliberWorkflowRun,
    state_code: str,
    actor: str,
    scopes: frozenset[str] | set[str],
    project_allows_approve: bool,
    queue_enabled: bool = True,
    approvals_enabled: bool = True,
    checkpointing_enabled: bool = True,
) -> dict[str, dict[str, Any]]:
    """Whether ``actor`` may approve and may reject now, with the first reason when not.

    The rules are the decision routes': the project ``approve`` action, then the node's ``required_role`` scope (both
    decisions), then for an approval only the requester and run-initiator rule (:func:`assert_distinct_actor`) and one
    approval per person. A request that is not open is not decidable by anyone: the reason is its state. Before the caller's
    own rights, the routes check three deployment switches, so the view names a disabled one first (an approval needs all
    three, a rejection the first two).
    """
    if state_code != STATE_OPEN:
        closed: dict[str, Any] = {"allowed": False, "reason_code": state_code}
        return {"approve": dict(closed), "reject": dict(closed)}
    policy = ApprovalPolicy.from_snapshot(approval.policy_snapshot)
    base: str | None = None
    if not queue_enabled:
        base = ABILITY_QUEUE_DISABLED
    elif not approvals_enabled:
        base = ABILITY_APPROVALS_DISABLED
    elif not project_allows_approve:
        base = ABILITY_PROJECT_ROLE
    elif policy.required_role not in scopes:
        base = ABILITY_SCOPE
    reject = {"allowed": base is None, "reason_code": base}
    approve_reason = base
    if approve_reason is None and not checkpointing_enabled:
        approve_reason = ABILITY_CHECKPOINTING_DISABLED
    if approve_reason is None:
        for initiator in (approval.requested_by, run.created_by):
            try:
                assert_distinct_actor(actor, initiator, allow_self=policy.allow_self_approval)
            except ApprovalPolicyError:
                approve_reason = ABILITY_SELF_APPROVAL
                break
    if approve_reason is None and actor in approvers_of(approval):
        approve_reason = ABILITY_DUPLICATE
    return {
        "approve": {"allowed": approve_reason is None, "reason_code": approve_reason},
        "reject": reject,
    }


def build_approval_view(
    session: Session,
    *,
    run: CaliberWorkflowRun,
    approval: CaliberRuntimeApprovalRequest,
    actor: str,
    scopes: frozenset[str] | set[str],
    project_allows_approve: bool,
    now: datetime,
    queue_enabled: bool = True,
    approvals_enabled: bool = True,
    checkpointing_enabled: bool = True,
) -> dict[str, Any]:
    """The view record for ``approval`` of ``run`` as seen by ``actor`` (see the module docstring).

    ``proposal`` and the requester's name are read only when ``project_allows_approve``: what an effect would do, and who
    asked for it, are shown to the people who may decide it; everyone else with read access sees the pins, the state and the
    quorum.
    """
    policy = ApprovalPolicy.from_snapshot(approval.policy_snapshot)
    approvers = approvers_of(approval)
    state = approval_state(approval, run=run, now=now)
    return {
        "approval": approval,
        "binding": binding_view(approval, show_requester=project_allows_approve),
        "proposal": load_proposal_view(session, approval) if project_allows_approve else None,
        "policy": {
            "required_role": policy.required_role,
            "approval_count": policy.approval_count,
            "timeout_behavior": policy.timeout_behavior,
            "allow_self_approval": policy.allow_self_approval,
        },
        "quorum": {
            "approvers": approvers,
            "required": policy.approval_count,
            "remaining": max(0, policy.approval_count - len(approvers)),
        },
        "state": state,
        "allowed_responses": list(ALLOWED_RESPONSES),
        "caller": {
            "principal": actor,
            **caller_abilities(
                approval,
                run=run,
                state_code=state["code"],
                actor=actor,
                scopes=scopes,
                project_allows_approve=project_allows_approve,
                queue_enabled=queue_enabled,
                approvals_enabled=approvals_enabled,
                checkpointing_enabled=checkpointing_enabled,
            ),
        },
    }


__all__ = [
    "ALLOWED_RESPONSES",
    "MAX_ARG_FIELDS",
    "approval_state",
    "approvers_of",
    "args_shape",
    "binding_view",
    "build_approval_view",
    "caller_abilities",
    "load_proposal_view",
]
