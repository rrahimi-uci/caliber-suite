"""Approval requests bound to the exact effect proposal they approve (B06.1, #706; ADR 0003 ``approval_request``).

A protected effect is approved by a person, and the approval has to say *what* it approves. :func:`create_approval_request`
records a ``pending`` row in ``caliber_runtime_approval_requests`` that is bound to one effect proposal: its full
``intent_hash`` and expected resource version (read from the stored proposal, never from the object the caller holds), the
``evidence_hash`` of what the approver is shown, a ``policy_digest`` *computed* from the node's own policy (so the digest
cannot disagree with the policy the decision route enforces), the requester and an expiry. Migration ``0127`` freezes those
columns in the database and refuses a half-bound row, so neither a later UPDATE nor raw SQL can re-bind a request.

**Not wired yet.** Nothing decides a bound request on its own terms: the decision route refuses to decide a request that has a
``proposal_id`` (``409 approval_bound_not_wired``) and its legacy selector ignores bound rows, until the expiry/supersession
step (B06.2) and the recheck at commit (B06.3) exist. Creating a bound request therefore records the question; it does not yet
make an approval answerable.

**Locking.** The proposal row is read ``FOR UPDATE`` (a no-op on SQLite, where a write transaction is exclusive). Creators for
one proposal therefore serialize, and so do they against ``caliber.effects.records.transition``'s UPDATE of that row: a
proposal that is moving to ``approved`` is either committed before the creator reads it (the creator sees ``approved`` and
refuses) or waits for the creator's transaction. Two creators for one ``proposed`` proposal both succeed, one after the
other, and leave two ``pending`` rows: there is deliberately no unique index, and replacing the older request by the newer
(``superseded_by``) is B06.2's job. B06.2 should take the same lock (``SELECT ... FOR UPDATE`` on the proposal row) in the
transaction that supersedes. The lock is held until the caller commits or rolls back, so a caller keeps its transaction short.
"""

from __future__ import annotations

import hashlib
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
from caliber.run_manifest import canonical_bytes
from caliber.workflows.approval_policy import (
    SUPPORTED_TIMEOUT_BEHAVIORS,
    VALID_ROLES,
    ApprovalPolicy,
)

PROPOSAL_NOT_PROPOSED: Final = "proposal_not_proposed"
PROPOSAL_NOT_FOUND: Final = "proposal_not_found"
BAD_FIELD: Final = "bad_field"
#: What an approver may answer (ADR 0003 ``approval_request.allowed_responses``).
ALLOWED_RESPONSES: Final = ("approve", "reject", "request_changes")
#: Longest approval window: a year. This is a policy choice, not a limit of the storage: a request that must stay open longer
#: than a year is a mistake in the caller, and an unbounded one would also overflow a date. It is not a configuration value.
MAX_TTL_SECONDS: Final = 365 * 24 * 60 * 60
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


def policy_digest(policy: ApprovalPolicy) -> str:
    """``sha256:`` and the SHA-256 of the canonical JSON of ``policy.to_snapshot()``: the digest a request pins.

    It covers the policy as snapshotted at request time (required role, quorum, timeout behaviour, whether the initiator may
    approve), not the approvers a quorum accumulates in the same stored snapshot afterwards.
    """
    return "sha256:" + hashlib.sha256(canonical_bytes(policy.to_snapshot())).hexdigest()


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

    **Not wired yet:** the decision route refuses a request that has a ``proposal_id`` (``409 approval_bound_not_wired``) until
    expiry and supersession (B06.2) and the recheck at commit (B06.3) land; B06.3 must also compare the requester with the
    run's initiator (the route compares ``run.created_by``, which is not ``requested_by``) and check the proposal's run is
    still active.
    """
    checked_policy = _checked_policy(policy)
    evidence = _checked_digest("evidence_hash", evidence_hash)
    requester = _checked_requester(requested_by)
    moment, expires = _window(now, _checked_ttl(ttl_seconds))
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
    ).one_or_none()
    if row is None:
        raise ApprovalRequestError(PROPOSAL_NOT_FOUND)
    if row.state != PROPOSED or proposal.state != PROPOSED:
        raise ApprovalRequestError(PROPOSAL_NOT_PROPOSED)
    project_id = session.execute(
        select(CaliberWorkflowRun.project_id).where(
            CaliberWorkflowRun.workflow_run_id == row.workflow_run_id
        )
    ).scalar_one()
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
    return request


__all__ = [
    "ALLOWED_RESPONSES",
    "BAD_FIELD",
    "MAX_TTL_SECONDS",
    "PROPOSAL_NOT_FOUND",
    "PROPOSAL_NOT_PROPOSED",
    "ApprovalRequestError",
    "create_approval_request",
    "policy_digest",
]
