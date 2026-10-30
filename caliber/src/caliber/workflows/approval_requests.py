"""Approval requests bound to the exact effect proposal they approve (B06.1, #706; ADR 0003 ``approval_request``).

A protected effect is approved by a person, and the approval has to say *what* it approves. :func:`create_approval_request`
records a ``pending`` row in ``caliber_runtime_approval_requests`` that is bound to one effect proposal: its full
``intent_hash`` and expected resource version (read from the stored proposal, never from the object the caller holds), the
``evidence_hash`` of what the approver is shown, a ``policy_digest`` *computed* from the node's own policy (so the digest
cannot disagree with the policy the decision route enforces), the requester and an expiry. Migration ``0127`` freezes those
columns in the database and refuses a half-bound row, so neither a later UPDATE nor raw SQL can re-bind a request.

**Expiry and supersession (B06.2, #712).** ``expires_at`` is ``requested_at + ttl_seconds`` (the deployment's default is
``CALIBER_APPROVAL_TTL_SECONDS``, read with :func:`configured_ttl_seconds`). A request that is past it, or that a newer request
replaced, is refused with a stable code by :func:`caliber.workflows.approval_policy.approval_refusal` (``expired``,
``superseded``), which the decision route applies and the recheck at commit (B06.3) reuses. Expiry is computed, never
stored: nothing sweeps a request into an ``expired`` status, so refusing an expired one changes no state.
:func:`create_approval_request` supersedes in the same transaction that inserts the replacement: every *live* bound request for
the same ``(workflow_run_id, node_id)`` (``pending``, or ``approved`` and not yet consumed) becomes ``superseded`` with
``superseded_by`` set to the new request. A new request therefore replaces its predecessor whether it names the same proposal
or a later one of the same node (a changed intent); a request for another node or another run never touches it, and neither
does a legacy node-level request (no ``proposal_id``), which binds no intent a change could void, nor one that is rejected,
already consumed or already superseded.

**Not wired yet.** Nothing decides a *live* bound request on its own terms: the decision route refuses one that has a
``proposal_id`` and is neither expired nor superseded (``409 approval_bound_not_wired``) and its legacy selector ignores bound
rows, until the recheck at commit (B06.3) verifies the exact intent and the approver's authority. Creating a bound request
therefore records the question; it does not yet make an approval answerable.

**Locking.** Creators serialize on the *run* row, then the proposal row (always in that order), both ``FOR UPDATE`` (a no-op on
SQLite, where a write transaction is exclusive). The run lock is what makes supersession safe: two creators for different
proposals of one node lock different proposal rows, so without it each would read the other's request as absent and leave two
live ones. The proposal lock serializes creators for one proposal against ``caliber.effects.records.transition``'s UPDATE of that
row: a proposal that is moving to ``approved`` is either committed before the creator reads it (the creator sees ``approved``
and refuses) or waits for the creator's transaction. The requests being superseded are selected ``FOR UPDATE`` too, so a decider
or a consumer that holds one finishes first and the supersession then sees its outcome (a consumed request is left alone).
Anything that decides or consumes a request must lock *that request's row* ``FOR UPDATE`` and re-run ``approval_refusal`` on it
before writing; taking the run row first, if it takes both, keeps the order. The locks are held until the caller commits or rolls
back, so a caller keeps its transaction short.
"""

from __future__ import annotations

import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.contracts import validate_record
from caliber.db.models import (
    CaliberEffectProposal,
    CaliberRuntimeApprovalRequest,
    CaliberWorkflowRun,
)
from caliber.effects.records import PRINTABLE, PROPOSED
from caliber.ids import new_runtime_approval_id
from caliber.workflows.approval_policy import (
    STATUS_SUPERSEDED,
    SUPPORTED_TIMEOUT_BEHAVIORS,
    VALID_ROLES,
    ApprovalPolicy,
    policy_digest,
)

PROPOSAL_NOT_PROPOSED: Final = "proposal_not_proposed"
PROPOSAL_NOT_FOUND: Final = "proposal_not_found"
BAD_FIELD: Final = "bad_field"
#: What an approver may answer (ADR 0003 ``approval_request.allowed_responses``).
ALLOWED_RESPONSES: Final = ("approve", "reject", "request_changes")
#: Longest approval window: a year. This is a policy choice, not a limit of the storage: a request that must stay open longer
#: than a year is a mistake in the caller, and an unbounded one would also overflow a date. It is also the ceiling of the
#: ``approval_ttl_seconds`` setting (a test pins the two together).
MAX_TTL_SECONDS: Final = 365 * 24 * 60 * 60
#: ``status`` values of a request that is still answerable or answered but not yet consumed: the ones a newer request replaces.
_LIVE_STATUSES: Final = ("pending", "approved")
_DIGEST_LENGTH: Final = len("sha256:") + 64
_MAX_REQUESTED_BY: Final = 256
#: Unicode categories refused in a requester: format characters (zero-width and bidirectional controls, the byte-order mark)
#: and the line and paragraph separators. They are invisible or reorder text, so two different ids can look the same.
_INVISIBLE_CATEGORIES: Final = frozenset({"Cf", "Zl", "Zp"})


class ApprovalRequestError(ValueError):
    """An approval request cannot be created; ``code`` is stable and the message is the code or names the field only."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


def _checked_policy(policy: object) -> ApprovalPolicy:
    """``policy`` if it is one the runtime can honour and store; the request would otherwise pin a policy nobody enforces."""
    if not isinstance(policy, ApprovalPolicy):
        raise ApprovalRequestError(BAD_FIELD, "policy must be an ApprovalPolicy")
    if policy.required_role not in VALID_ROLES:
        raise ApprovalRequestError(BAD_FIELD, "policy.required_role is not a known role")
    if type(policy.approval_count) is not int or policy.approval_count < 1:
        raise ApprovalRequestError(
            BAD_FIELD, "policy.approval_count must be an integer of at least 1"
        )
    if policy.timeout_behavior not in SUPPORTED_TIMEOUT_BEHAVIORS:
        raise ApprovalRequestError(
            BAD_FIELD, "policy.timeout_behavior is not one the runtime honours"
        )
    if type(policy.allow_self_approval) is not bool:
        raise ApprovalRequestError(BAD_FIELD, "policy.allow_self_approval must be a boolean")
    return policy


def _checked_digest(name: str, value: object) -> str:
    if not isinstance(value, str) or len(value) != _DIGEST_LENGTH or not _is_sha256(value):
        raise ApprovalRequestError(
            BAD_FIELD, f"{name} must be 'sha256:' and 64 lowercase hex digits"
        )
    return value


def _is_sha256(value: str) -> bool:
    return value.startswith("sha256:") and all(c in "0123456789abcdef" for c in value[7:])


def _checked_requester(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > _MAX_REQUESTED_BY
        or not PRINTABLE.fullmatch(value)
        or any(unicodedata.category(c) in _INVISIBLE_CATEGORIES for c in value)
    ):
        raise ApprovalRequestError(
            BAD_FIELD,
            f"requested_by must be 1..{_MAX_REQUESTED_BY} printable characters with no leading or trailing "
            "whitespace, control, zero-width or bidirectional characters",
        )
    return value


def _checked_ttl(value: object) -> int:
    if type(value) is not int or not 0 < value <= MAX_TTL_SECONDS:
        raise ApprovalRequestError(
            BAD_FIELD, f"ttl_seconds must be an integer from 1 to {MAX_TTL_SECONDS}"
        )
    return value


def _window(now: datetime | None, ttl_seconds: int) -> tuple[datetime, datetime]:
    """``(start, end)`` in UTC. A clock the calendar cannot add the ttl to is ``bad_field``, never an ``OverflowError``."""
    if now is not None and not isinstance(now, datetime):
        raise ApprovalRequestError(BAD_FIELD, "now must be a datetime")
    try:
        moment = datetime.now(timezone.utc) if now is None else now
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        moment = moment.astimezone(timezone.utc)
        return moment, moment + timedelta(seconds=ttl_seconds)
    except (OverflowError, ValueError) as exc:
        raise ApprovalRequestError(
            BAD_FIELD, "now is outside the range of dates a request can expire in"
        ) from exc


def configured_ttl_seconds(config: object) -> int:
    """The deployment's approval window, ``config.approval_ttl_seconds`` (``CALIBER_APPROVAL_TTL_SECONDS``, default 86400).

    For the caller that creates a request to pass as ``ttl_seconds``. Validated here as well as by the configuration model
    (which refuses a value outside ``1..MAX_TTL_SECONDS``), so a hand-built or mocked config cannot store a window the
    writer would refuse: anything but an integer in that range is ``bad_field`` naming the setting.
    """
    value = getattr(config, "approval_ttl_seconds", None)
    if type(value) is not int or not 0 < value <= MAX_TTL_SECONDS:
        raise ApprovalRequestError(
            BAD_FIELD, f"approval_ttl_seconds must be an integer from 1 to {MAX_TTL_SECONDS}"
        )
    return value


def _rfc3339(moment: datetime) -> str:
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def create_approval_request(
    session: Session,
    *,
    proposal: CaliberEffectProposal,
    policy: ApprovalPolicy,
    evidence_hash: str,
    requested_by: str,
    ttl_seconds: int,
    now: datetime | None = None,
) -> CaliberRuntimeApprovalRequest:
    """Ask for approval of exactly one proposal's intent, in the caller's transaction (flushes, never commits).

    The request is bound to what the proposal says *now*, read from the database ``FOR UPDATE`` (see the module docstring)
    rather than from the object the caller holds: its ``intent_hash`` (the one full digest, ``caliber.effects.records.Intent``)
    and expected resource version. ``policy`` is the node's own :class:`ApprovalPolicy` and is stored as the request's
    ``policy_snapshot`` exactly as the decision route reads it back (``ApprovalPolicy.from_snapshot``); ``policy_digest`` is
    :func:`policy_digest` of it, computed here and never taken from the caller. ``evidence_hash`` is the digest of what the
    approver is shown, ``expires_at`` is ``now + ttl_seconds`` (``now`` is injectable for a fixed clock) and the record is
    valid against the ``approval_request`` v1 contract before it is stored (``risk_class`` is the proposal's effect class).

    Only a ``proposed`` proposal can be asked about: any other state raises :class:`ApprovalRequestError` with code and
    message ``proposal_not_proposed``, an unknown one ``proposal_not_found``. A policy the runtime cannot honour, a malformed
    ``evidence_hash`` (``sha256:`` and 64 lowercase hex digits), a ``requested_by`` that is blank, has leading or trailing
    whitespace, is over 256 characters or holds a control, zero-width or bidirectional character, a ``ttl_seconds`` that is not
    an integer from 1 to :data:`MAX_TTL_SECONDS`, or a ``now`` the expiry cannot be computed from, is ``bad_field``. Nothing is
    stored on any of them.

    **Supersession.** In the same transaction every live bound request for the same ``(workflow_run_id, node_id)`` (``pending``,
    or ``approved`` with ``consumed_at`` unset) is set to ``superseded`` with ``superseded_by`` the new request's id; see the
    module docstring for what is left alone and for the lock order (run, proposal, then the superseded requests). Nothing else
    on those rows moves. The requests a call replaced are those whose ``superseded_by`` is the returned request's id.

    **Not wired yet:** the decision route refuses a live bound request (``409 approval_bound_not_wired``) until the recheck at
    commit (B06.3) lands; B06.3 must also compare the requester with the run's initiator (the route compares
    ``run.created_by``, which is not ``requested_by``) and check the proposal's run is still active.
    """
    checked_policy = _checked_policy(policy)
    evidence = _checked_digest("evidence_hash", evidence_hash)
    requester = _checked_requester(requested_by)
    moment, expires = _window(now, _checked_ttl(ttl_seconds))
    # Lock order is run, then proposal, then the requests being superseded. The proposal's run and node never change, so the first
    # read needs no lock; the run lock is what serializes creators for different proposals of one node (module docstring).
    run_id = session.execute(
        select(CaliberEffectProposal.workflow_run_id).where(
            CaliberEffectProposal.proposal_id == proposal.proposal_id
        )
    ).scalar_one_or_none()
    if run_id is None:
        raise ApprovalRequestError(PROPOSAL_NOT_FOUND)
    project_id = session.execute(
        select(CaliberWorkflowRun.project_id)
        .where(CaliberWorkflowRun.workflow_run_id == run_id)
        .with_for_update()
    ).scalar_one()
    row = session.execute(
        select(
            CaliberEffectProposal.proposal_id,
            CaliberEffectProposal.state,
            CaliberEffectProposal.workflow_run_id,
            CaliberEffectProposal.node_id,
            CaliberEffectProposal.intent_hash,
            CaliberEffectProposal.effect_class,
            CaliberEffectProposal.expected_resource_version,
        )
        .where(CaliberEffectProposal.proposal_id == proposal.proposal_id)
        .with_for_update()
    ).one()  # it exists: proposals are immutable and never deleted (migration 0123)
    if row.state != PROPOSED or proposal.state != PROPOSED:
        raise ApprovalRequestError(PROPOSAL_NOT_PROPOSED)
    request_id = new_runtime_approval_id()
    digest = policy_digest(checked_policy)
    validate_record(
        "approval_request",
        {
            "schema_version": "v1",
            "approval_request_id": request_id,
            "proposal_id": row.proposal_id,
            "intent_hash": row.intent_hash,
            "evidence_hash": evidence,
            "policy_digest": digest,
            "expected_resource_version": row.expected_resource_version,
            "risk_class": row.effect_class,
            "requested_by": requester,
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
        policy_snapshot=checked_policy.to_snapshot(),
        proposal_id=row.proposal_id,
        intent_hash=row.intent_hash,
        evidence_hash=evidence,
        policy_digest=digest,
        expected_resource_version=row.expected_resource_version,
        expires_at=expires.replace(tzinfo=None),
        requested_by=requester,
    )
    session.add(request)
    session.flush()
    _supersede_live_requests(session, replacement=request)
    return request


def _supersede_live_requests(
    session: Session, *, replacement: CaliberRuntimeApprovalRequest
) -> None:
    """Mark every other live bound request of ``replacement``'s run and node superseded by it (the caller holds the run lock).

    The rows are read ``FOR UPDATE`` and the predicate is part of that read, so a request that a decider or consumer
    finished with first (``consumed_at`` now set, status ``rejected``) is no longer selected, rather than overwritten.
    Only ``status`` and ``superseded_by`` change: the binding columns are frozen by the database.
    """
    older = (
        session.execute(
            select(CaliberRuntimeApprovalRequest)
            .where(
                CaliberRuntimeApprovalRequest.workflow_run_id == replacement.workflow_run_id,
                CaliberRuntimeApprovalRequest.node_id == replacement.node_id,
                CaliberRuntimeApprovalRequest.runtime_approval_id
                != replacement.runtime_approval_id,
                CaliberRuntimeApprovalRequest.proposal_id.is_not(None),
                CaliberRuntimeApprovalRequest.status.in_(_LIVE_STATUSES),
                CaliberRuntimeApprovalRequest.superseded_by.is_(None),
                CaliberRuntimeApprovalRequest.consumed_at.is_(None),
            )
            .order_by(CaliberRuntimeApprovalRequest.runtime_approval_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        .scalars()
        .all()
    )
    for request in older:
        request.status = STATUS_SUPERSEDED
        request.superseded_by = replacement.runtime_approval_id
    session.flush()


__all__ = [
    "ALLOWED_RESPONSES",
    "BAD_FIELD",
    "MAX_TTL_SECONDS",
    "PROPOSAL_NOT_FOUND",
    "PROPOSAL_NOT_PROPOSED",
    "ApprovalRequestError",
    "configured_ttl_seconds",
    "create_approval_request",
    "policy_digest",
]
